from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import resources

from cursor_orchestrator.branch_manager import BranchManager, NullBranchManager
from cursor_orchestrator.clients.base import (
    CursorClientBase,
    PlanCriticClientBase,
    ReviewerClientBase,
    TestClientBase,
)
from cursor_orchestrator.config import OrchestratorConfig
from cursor_orchestrator.models import (
    FeaturePlan,
    Plan,
    PlanCritique,
    ReviewResult,
    SubTaskResult,
    Verdict,
)


class HumanGate:
    """Blocking CLI prompts for the two points a human must weigh in:
    scope approval, and the iteration-cap check-in. Inject a different
    object (e.g. a scripted fake) to drive the orchestrator without a
    terminal, such as in tests.
    """

    def approve_scope(self, plan: Plan, critique: PlanCritique | None = None) -> tuple[str, str]:
        """Returns (decision, feedback); decision in {"approve","edit","abort"}."""
        print("\n=== PLAN ===")
        print(plan.summary)
        for t in plan.subtasks:
            print(f"  - [{t.id}] {t.description} (depends_on={t.depends_on})")
        if critique is not None:
            print("\n=== PLAN CRITIQUE (second opinion -- verify, not fact) ===")
            if not critique.findings:
                print("  (no findings)")
            for f in critique.findings:
                print(f"  [{f.severity}] {f.message}")
        while True:
            choice = input("\nApprove this plan? [a]pprove / [e]dit / [x] abort: ").strip().lower()
            if choice in ("a", "approve"):
                return "approve", ""
            if choice in ("e", "edit"):
                return "edit", input("Describe the changes you want: ").strip()
            if choice in ("x", "abort"):
                return "abort", ""
            print("Please enter a, e, or x.")

    def approve_feature_plan(self, feature_plan: FeaturePlan) -> tuple[str, str]:
        """Returns (decision, feedback); decision in {"approve","edit","abort"}.
        Same shape as approve_scope, one tier up -- gates campaign.py's
        feature/task breakdown before any branch is created, exactly
        once per whole architecture (not per task)."""
        print("\n=== FEATURE PLAN ===")
        print(feature_plan.summary)
        for feature in feature_plan.features:
            print(f"\n[{feature.id}] {feature.description}")
            for task in feature.tasks:
                print(f"  - [{task.id}] {task.description} (depends_on={task.depends_on})")
        while True:
            choice = input(
                "\nApprove this feature/task breakdown? [a]pprove / [e]dit / [x] abort: "
            ).strip().lower()
            if choice in ("a", "approve"):
                return "approve", ""
            if choice in ("e", "edit"):
                return "edit", input("Describe the changes you want: ").strip()
            if choice in ("x", "abort"):
                return "abort", ""
            print("Please enter a, e, or x.")

    def iteration_cap_reached(self, findings_summary: str) -> str:
        """Returns one of "continue", "take_over", "accept_as_is"."""
        print("\n=== ITERATION CAP REACHED -- still not ready_for_pr ===")
        print(findings_summary)
        while True:
            choice = input(
                "[c]ontinue more iterations / [t]ake over manually / "
                "[p]roceed to PR as-is (flagged): "
            ).strip().lower()
            if choice in ("c", "continue"):
                return "continue"
            if choice in ("t", "take_over"):
                return "take_over"
            if choice in ("p", "proceed"):
                return "accept_as_is"
            print("Please enter c, t, or p.")


class AbortedByHuman(Exception):
    pass


class TookOverManually(Exception):
    pass


@dataclass
class OrchestratorResult:
    pr_url: str | None
    aborted: bool = False
    took_over: bool = False
    babysit_outcome: str | None = None  # "clean" | "escalated", set once a PR is opened


class Orchestrator:
    """The state machine: plan -> scope gate -> build -> review loop -> PR
    -> babysit. Reviewer is the sole verdict source -- a subtask with a
    failed test result is auto-needs_revision before the reviewer is even
    asked. Hitting the iteration cap is always a deterministic, blocking
    human gate, never a model judgment call.
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        cursor_client: CursorClientBase,
        test_client: TestClientBase,
        reviewer_client: ReviewerClientBase,
        plan_critic_client: PlanCriticClientBase,
        planner_client: CursorClientBase | None = None,
        human_gate: HumanGate | None = None,
        dry_run: bool = False,
        repo_path: str = ".",
    ) -> None:
        self.config = config
        self.cursor_client = cursor_client
        self.planner_client = planner_client or cursor_client
        self.test_client = test_client
        self.reviewer_client = reviewer_client
        self.plan_critic_client = plan_critic_client
        self.human_gate = human_gate or HumanGate()
        self.dry_run = dry_run
        self.repo_path = repo_path

    def run(self, prompt: str, branch_name: str | None = None) -> OrchestratorResult:
        plan = self._plan_with_approval(prompt)
        if plan is None:
            return OrchestratorResult(pr_url=None, aborted=True)

        feature_branch = branch_name or f"orchestrator/{_slugify(prompt)}"
        branch_manager = (
            NullBranchManager(self.config.git.base_branch, feature_branch)
            if self.dry_run
            else BranchManager(self.repo_path, self.config.git.base_branch, feature_branch)
        )
        branch_manager.create_feature_branch()

        try:
            results = self._execute_plan(plan, prompt, branch_manager)
            outcome = self._review_loop(prompt, plan, results, branch_manager)
            if outcome == "aborted":
                return OrchestratorResult(pr_url=None, took_over=True)

            pr = self.cursor_client.create_pr(
                branch=feature_branch,
                base_branch=self.config.git.base_branch,
                title=f"[orchestrator] {plan.summary}",
                body=_render_pr_body(prompt, plan, results),
            )
            print(f"\n=== PR OPENED: {pr.url} ===")

            from cursor_orchestrator.babysitter import Babysitter

            babysit_poll_interval = 0.1 if self.dry_run else None
            print("=== BABYSITTING PR (polling CI + review comments) ===")
            outcome = Babysitter(
                self.config,
                self.cursor_client,
                test_client=self.test_client,
                branch_manager=branch_manager,
                human_gate=self.human_gate,
                poll_interval_seconds=babysit_poll_interval,
            ).babysit(pr, prompt)
            print(f"=== BABYSITTING DONE: {outcome} ===")

            return OrchestratorResult(pr_url=pr.url, babysit_outcome=outcome)
        finally:
            branch_manager.cleanup()

    def _plan_with_approval(self, prompt: str) -> Plan | None:
        plan = self.planner_client.plan(prompt)
        while True:
            critique = self.plan_critic_client.critique(prompt, plan)
            decision, feedback = self.human_gate.approve_scope(plan, critique)
            if decision == "approve":
                return plan
            if decision == "abort":
                return None
            plan = self.planner_client.plan(
                f"{prompt}\n\nHuman feedback on prior plan: {feedback}"
            )

    def _execute_plan(
        self, plan: Plan, prompt: str, branch_manager: BranchManager | NullBranchManager
    ) -> dict[str, SubTaskResult]:
        results: dict[str, SubTaskResult] = {}
        for group_num, group in enumerate(
            plan.chunked_parallel_groups(self.config.limits.max_parallel_agents), start=1
        ):
            print(f"=== GROUP {group_num}: {[t.id for t in group]} ===")
            for subtask in group:
                results[subtask.id] = self._build_subtask(subtask, prompt, branch_manager)
                tr = results[subtask.id].test_result
                print(f"  [{subtask.id}] tests: {'PASS' if tr and tr.passed else 'FAIL'}")
            for subtask in group:
                try:
                    branch_manager.merge_worktree(subtask.id)
                finally:
                    branch_manager.remove_worktree(subtask.id)
        return results

    def _build_subtask(self, subtask, prompt, branch_manager) -> SubTaskResult:
        worktree = branch_manager.create_worktree(subtask.id)
        diff, rationale = self.cursor_client.implement_subtask(subtask, worktree.path, prompt)
        branch_manager.commit_worktree(subtask.id, f"implement {subtask.id}")
        test_result = self.test_client.write_and_run_tests(subtask, worktree.path)
        return SubTaskResult(
            subtask=subtask,
            diff=diff,
            rationale=rationale,
            branch_or_worktree=worktree.branch,
            test_result=test_result,
        )

    def _review_loop(
        self,
        prompt: str,
        plan: Plan,
        results: dict[str, SubTaskResult],
        branch_manager: BranchManager | NullBranchManager,
    ) -> str:
        iteration = 0
        while True:
            review = self.reviewer_client.review(prompt, plan.summary, list(results.values()))
            print(f"=== REVIEW: {review.verdict.value} ===")
            if review.verdict == Verdict.READY_FOR_PR:
                return "ready"

            iteration += 1
            print(f"  needs_revision -> {review.revision_requests}")
            if iteration >= self.config.limits.max_review_iterations:
                decision = self.human_gate.iteration_cap_reached(_findings_summary(review))
                if decision == "accept_as_is":
                    return "ready"
                if decision == "take_over":
                    return "aborted"
                iteration = 0  # human granted more iterations

            for subtask_id, instructions in review.revision_requests.items():
                subtask = results[subtask_id].subtask
                worktree = branch_manager.create_worktree(subtask_id)
                diff, rationale = self.cursor_client.implement_subtask(
                    subtask, worktree.path, f"{prompt}\n\nRevision requested: {instructions}"
                )
                branch_manager.commit_worktree(subtask_id, f"revise {subtask_id}")
                test_result = self.test_client.write_and_run_tests(subtask, worktree.path)
                results[subtask_id] = SubTaskResult(
                    subtask=subtask,
                    diff=diff,
                    rationale=rationale,
                    branch_or_worktree=worktree.branch,
                    test_result=test_result,
                )
                try:
                    branch_manager.merge_worktree(subtask_id)
                finally:
                    branch_manager.remove_worktree(subtask_id)


def _slugify(prompt: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", prompt.lower()).strip("-")
    return slug[:40] or "task"


def _findings_summary(review: ReviewResult) -> str:
    lines = [f"[{f.severity}] subtask={f.subtask_id}: {f.message}" for f in review.findings]
    return "\n".join(lines) if lines else "(no findings recorded)"


def _render_pr_body(prompt: str, plan: Plan, results: dict[str, SubTaskResult]) -> str:
    template = resources.files("cursor_orchestrator").joinpath("pr_template.md").read_text()
    rationale = "\n".join(f"- **{r.subtask.id}**: {r.rationale}" for r in results.values())
    test_results = "\n".join(
        f"- **{r.subtask.id}**: "
        f"{'PASS' if r.test_result and r.test_result.passed else 'FAIL'} -- "
        f"{r.test_result.details if r.test_result else 'no test result'}"
        for r in results.values()
    )
    return template.format(
        original_prompt=prompt,
        plan_summary=plan.summary,
        subtask_rationale=rationale,
        reviewer_findings="(attached at PR-open time)",
        test_results=test_results,
    )
