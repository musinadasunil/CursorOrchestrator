from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from cursor_orchestrator.branch_manager import sync_base_branch
from cursor_orchestrator.clients.base import (
    CursorClientBase,
    FeaturePlannerClientBase,
    PlanCriticClientBase,
    PullRequest,
    ReviewerClientBase,
    TestClientBase,
)
from cursor_orchestrator.config import OrchestratorConfig
from cursor_orchestrator.models import Feature, FeaturePlan, Task
from cursor_orchestrator.orchestrator import HumanGate, Orchestrator

"""Sequential, PR-gated execution of a whole architecture: feature-plan ->
human approval -> for each task (in dependency order), sync the base
branch, run the normal plan/build/review/PR flow (orchestrator.py)
scoped to that one task, then actually wait for a human to merge its PR
(not just for CI to go clean, which is as far as a normal single-prompt
run waits) before moving to the next task.

Progress is persisted to a JSON state file so a long wait for a human
merge -- which could take hours or days -- survives the process being
killed; re-running the same command resumes instead of re-planning or
rebuilding already-merged tasks.
"""


@dataclass
class TaskState:
    status: str = "pending"  # "pending" | "pr_open" | "merged"
    pr_url: str | None = None
    branch: str | None = None


@dataclass
class CampaignState:
    architecture_prompt: str
    feature_plan: FeaturePlan
    tasks: dict[str, TaskState] = field(default_factory=dict)

    @staticmethod
    def new(architecture_prompt: str, feature_plan: FeaturePlan) -> "CampaignState":
        tasks = {t.id: TaskState() for t in feature_plan.ordered_tasks()}
        return CampaignState(
            architecture_prompt=architecture_prompt, feature_plan=feature_plan, tasks=tasks
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "architecture_prompt": self.architecture_prompt,
            "feature_plan": {
                "summary": self.feature_plan.summary,
                "features": [
                    {
                        "id": f.id,
                        "description": f.description,
                        "tasks": [
                            {"id": t.id, "description": t.description, "depends_on": t.depends_on}
                            for t in f.tasks
                        ],
                    }
                    for f in self.feature_plan.features
                ],
            },
            "tasks": {
                tid: {"status": ts.status, "pr_url": ts.pr_url, "branch": ts.branch}
                for tid, ts in self.tasks.items()
            },
        }
        path.write_text(json.dumps(payload, indent=2))

    @staticmethod
    def load(path: Path) -> "CampaignState":
        payload = json.loads(path.read_text())
        feature_plan = FeaturePlan(
            summary=payload["feature_plan"]["summary"],
            features=[
                Feature(
                    id=f["id"],
                    description=f["description"],
                    tasks=[
                        Task(id=t["id"], description=t["description"], depends_on=t.get("depends_on", []))
                        for t in f["tasks"]
                    ],
                )
                for f in payload["feature_plan"]["features"]
            ],
        )
        tasks = {tid: TaskState(**ts) for tid, ts in payload["tasks"].items()}
        return CampaignState(
            architecture_prompt=payload["architecture_prompt"],
            feature_plan=feature_plan,
            tasks=tasks,
        )


@dataclass
class CampaignResult:
    completed: bool = False
    aborted: bool = False
    halted_reason: str | None = None


def default_state_file_path(repo_path: str, architecture_prompt: str) -> Path:
    repo_slug = _slugify(Path(repo_path).resolve().name)
    prompt_slug = _slugify(architecture_prompt)
    return Path.home() / ".cursor-orchestrator" / "campaigns" / f"{repo_slug}-{prompt_slug}.json"


class CampaignRunner:
    """Drives a whole architecture through campaign.py's feature/task tier,
    one task at a time. Each task is handed to a fresh orchestrator.py
    Orchestrator (same role clients every time) scoped to just that
    task's description -- campaign.py adds nothing to the actual
    plan/build/review/PR logic, only the sequencing and merge-gating
    around it.
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        cursor_client: CursorClientBase,
        test_client: TestClientBase,
        reviewer_client: ReviewerClientBase,
        plan_critic_client: PlanCriticClientBase,
        feature_planner_client: FeaturePlannerClientBase,
        planner_client: CursorClientBase | None = None,
        human_gate: HumanGate | None = None,
        dry_run: bool = False,
        repo_path: str = ".",
    ) -> None:
        self.config = config
        self.cursor_client = cursor_client
        self.test_client = test_client
        self.reviewer_client = reviewer_client
        self.plan_critic_client = plan_critic_client
        self.feature_planner_client = feature_planner_client
        self.planner_client = planner_client
        self.human_gate = human_gate or HumanGate()
        self.dry_run = dry_run
        self.repo_path = repo_path

    def run(self, architecture_prompt: str, state_file: Path) -> CampaignResult:
        state = self._load_or_plan(architecture_prompt, state_file)
        if state is None:
            return CampaignResult(aborted=True)

        tasks = state.feature_plan.ordered_tasks()
        for task in tasks:
            task_state = state.tasks[task.id]
            if task_state.status == "merged":
                continue

            if task_state.status == "pending":
                if not self.dry_run:
                    sync_base_branch(self.repo_path, self.config.git.base_branch)

                branch_name = f"orchestrator/{task.id}"
                result = Orchestrator(
                    config=self.config,
                    cursor_client=self.cursor_client,
                    test_client=self.test_client,
                    reviewer_client=self.reviewer_client,
                    plan_critic_client=self.plan_critic_client,
                    planner_client=self.planner_client,
                    human_gate=self.human_gate,
                    dry_run=self.dry_run,
                    repo_path=self.repo_path,
                ).run(task.description, branch_name=branch_name)

                if result.aborted or result.took_over:
                    print(
                        f"[campaign] halted at task {task.id!r} -- build did not reach a PR. "
                        f"State file: {state_file}"
                    )
                    return CampaignResult(halted_reason=f"task {task.id!r} aborted or taken over")

                task_state.status = "pr_open"
                task_state.pr_url = result.pr_url
                task_state.branch = branch_name
                state.save(state_file)

                if result.babysit_outcome == "escalated":
                    print(
                        f"[campaign] halted at task {task.id!r} -- PR needs human attention: "
                        f"{result.pr_url}. Once resolved, re-run to resume waiting for the merge."
                    )
                    return CampaignResult(halted_reason=f"task {task.id!r} escalated")

            pr = PullRequest(url=task_state.pr_url, branch=task_state.branch, head_sha="")
            try:
                merge_state = self._wait_for_merge(pr)
            except KeyboardInterrupt:
                print(
                    f"\n[campaign] paused waiting for task {task.id!r}'s PR to merge -- "
                    f"re-run the same command to resume. State file: {state_file}"
                )
                return CampaignResult(halted_reason="interrupted")

            if merge_state == "closed":
                print(
                    f"[campaign] halted -- task {task.id!r}'s PR was closed without merging: "
                    f"{task_state.pr_url}"
                )
                return CampaignResult(halted_reason=f"task {task.id!r} PR closed unmerged")

            task_state.status = "merged"
            state.save(state_file)

        print(f"[campaign] all {len(tasks)} tasks merged. State file: {state_file}")
        return CampaignResult(completed=True)

    def _load_or_plan(self, architecture_prompt: str, state_file: Path) -> CampaignState | None:
        if state_file.exists():
            state = CampaignState.load(state_file)
            remaining = sum(1 for t in state.tasks.values() if t.status != "merged")
            print(f"[campaign] resuming from {state_file} -- {remaining} task(s) remaining")
            return state

        feature_plan = self.feature_planner_client.plan_features(architecture_prompt)
        while True:
            decision, feedback = self.human_gate.approve_feature_plan(feature_plan)
            if decision == "approve":
                break
            if decision == "abort":
                return None
            feature_plan = self.feature_planner_client.plan_features(
                f"{architecture_prompt}\n\nHuman feedback on prior feature plan: {feedback}"
            )

        state = CampaignState.new(architecture_prompt, feature_plan)
        state.save(state_file)
        print(f"[campaign] state file: {state_file}")
        return state

    def _wait_for_merge(self, pr: PullRequest) -> str:
        poll_interval = 0.01 if self.dry_run else self.config.limits.merge_poll_interval_seconds
        polls = 0
        while True:
            state = self.cursor_client.get_pr_merge_state(pr)
            if state != "open":
                return state
            polls += 1
            if polls == 1 or polls % 10 == 0:
                print(f"[campaign] waiting for {pr.url} to be merged...")
            time.sleep(poll_interval)


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:40] or "task"
