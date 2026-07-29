import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

from cursor_orchestrator import campaign as campaign_module
from cursor_orchestrator.campaign import CampaignRunner, CampaignState, TaskState
from cursor_orchestrator.clients.base import CIStatus, PullRequest
from cursor_orchestrator.clients.mock_clients import (
    MockCursorClient,
    MockPlanCriticClient,
    MockReviewerClient,
    MockTestClient,
)
from cursor_orchestrator.config import (
    GitConfig,
    LimitsConfig,
    ModelsConfig,
    OrchestratorConfig,
    TestingConfig as OrchestratorTestingConfig,
)
from cursor_orchestrator.models import Feature, FeaturePlan, Task


def _config() -> OrchestratorConfig:
    return OrchestratorConfig(
        models=ModelsConfig(planner="p", plan_critic="c", implementer="i", tester="t", reviewer="r"),
        limits=LimitsConfig(
            max_parallel_agents=3,
            max_review_iterations=3,
            max_babysit_iterations=5,
            max_babysit_wall_clock_minutes=60,
            babysit_poll_interval_seconds=0.01,
            cursor_agent_timeout_seconds=60,
            gh_timeout_seconds=30,
            merge_poll_interval_seconds=0.01,
        ),
        git=GitConfig(base_branch="main", pr_backend="gh"),
        testing=OrchestratorTestingConfig(command="true", timeout_seconds=30),
    )


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


class FakeFeaturePlannerClient:
    def __init__(self, feature_plan: FeaturePlan):
        self.feature_plan = feature_plan
        self.calls = 0

    def plan_features(self, architecture_prompt):
        self.calls += 1
        return self.feature_plan


class ScriptedHumanGate:
    def __init__(self, feature_plan_decision="approve"):
        self.feature_plan_decision = feature_plan_decision
        self.feature_plan_calls = 0

    def approve_feature_plan(self, feature_plan):
        self.feature_plan_calls += 1
        return self.feature_plan_decision, ""

    def approve_scope(self, plan, critique=None):
        return "approve", ""

    def iteration_cap_reached(self, findings_summary):
        return "accept_as_is"


class ScriptedMergeCursorClient(MockCursorClient):
    """Merge state is scripted per PR branch, so different tasks' PRs can
    take a different number of polls to resolve."""

    def __init__(self, merge_states_by_branch: dict[str, list[str]]):
        super().__init__()
        self._merge_states_by_branch = {k: list(v) for k, v in merge_states_by_branch.items()}
        self.merge_state_call_counts: dict[str, int] = defaultdict(int)

    def get_pr_merge_state(self, pr: PullRequest) -> str:
        self.merge_state_call_counts[pr.branch] += 1
        queue = self._merge_states_by_branch[pr.branch]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class AlwaysFailingCiCursorClient(MockCursorClient):
    def get_pr_status(self, pr):
        return CIStatus(state="failure", failing_checks=["unit-tests"], logs={"unit-tests": "boom"})


def _two_task_feature_plan() -> FeaturePlan:
    return FeaturePlan(
        summary="two features",
        features=[
            Feature(id="f1", description="f1", tasks=[Task(id="a", description="task a")]),
            Feature(
                id="f2",
                description="f2",
                tasks=[Task(id="b", description="task b", depends_on=["a"])],
            ),
        ],
    )


def _runner(cursor_client, feature_planner_client, human_gate, dry_run=True, repo_path=".") -> CampaignRunner:
    return CampaignRunner(
        config=_config(),
        cursor_client=cursor_client,
        test_client=MockTestClient(),
        reviewer_client=MockReviewerClient(),
        plan_critic_client=MockPlanCriticClient(),
        feature_planner_client=feature_planner_client,
        human_gate=human_gate,
        dry_run=dry_run,
        repo_path=repo_path,
    )


def test_happy_path_across_two_sequential_dependent_tasks(tmp_path):
    state_file = tmp_path / "state.json"
    cursor_client = ScriptedMergeCursorClient(
        {"orchestrator/a": ["open", "open", "merged"], "orchestrator/b": ["merged"]}
    )
    feature_planner = FakeFeaturePlannerClient(_two_task_feature_plan())
    gate = ScriptedHumanGate()

    result = _runner(cursor_client, feature_planner, gate).run("build the architecture", state_file)

    assert result.completed
    assert cursor_client.merge_state_call_counts["orchestrator/a"] == 3
    assert cursor_client.merge_state_call_counts["orchestrator/b"] == 1

    state = CampaignState.load(state_file)
    assert state.tasks["a"].status == "merged"
    assert state.tasks["b"].status == "merged"


def test_resumes_from_pre_seeded_state_file_and_skips_merged_task(tmp_path):
    state_file = tmp_path / "state.json"
    feature_plan = _two_task_feature_plan()
    state = CampaignState.new("build the architecture", feature_plan)
    state.tasks["a"] = TaskState(status="merged", pr_url="https://example.invalid/pr/a", branch="orchestrator/a")
    state.save(state_file)

    cursor_client = ScriptedMergeCursorClient({"orchestrator/b": ["merged"]})
    feature_planner = FakeFeaturePlannerClient(feature_plan)
    gate = ScriptedHumanGate()

    result = _runner(cursor_client, feature_planner, gate).run("build the architecture", state_file)

    assert result.completed
    assert feature_planner.calls == 0  # resumed -- never re-planned
    assert gate.feature_plan_calls == 0  # resumed -- never re-approved
    assert "orchestrator/a" not in cursor_client.merge_state_call_counts  # never touched again

    final_state = CampaignState.load(state_file)
    assert final_state.tasks["a"].status == "merged"
    assert final_state.tasks["b"].status == "merged"


def test_abort_at_feature_plan_gate_writes_no_state_file(tmp_path):
    state_file = tmp_path / "state.json"
    cursor_client = MockCursorClient()
    feature_planner = FakeFeaturePlannerClient(_two_task_feature_plan())
    gate = ScriptedHumanGate(feature_plan_decision="abort")

    result = _runner(cursor_client, feature_planner, gate).run("build the architecture", state_file)

    assert result.aborted
    assert not state_file.exists()


def test_mid_task_escalation_halts_campaign_before_merge_wait(tmp_path):
    state_file = tmp_path / "state.json"
    cursor_client = AlwaysFailingCiCursorClient()
    feature_planner = FakeFeaturePlannerClient(_two_task_feature_plan())
    gate = ScriptedHumanGate()

    result = _runner(cursor_client, feature_planner, gate).run("build the architecture", state_file)

    assert result.halted_reason is not None
    assert "escalated" in result.halted_reason

    state = CampaignState.load(state_file)
    assert state.tasks["a"].status == "pr_open"
    assert state.tasks["b"].status == "pending"


def test_pr_closed_without_merging_halts_campaign(tmp_path):
    state_file = tmp_path / "state.json"
    cursor_client = ScriptedMergeCursorClient({"orchestrator/a": ["closed"]})
    feature_planner = FakeFeaturePlannerClient(
        FeaturePlan(
            summary="one feature",
            features=[Feature(id="f1", description="f1", tasks=[Task(id="a", description="task a")])],
        )
    )
    gate = ScriptedHumanGate()

    result = _runner(cursor_client, feature_planner, gate).run("build the architecture", state_file)

    assert result.halted_reason is not None
    assert "closed" in result.halted_reason
    state = CampaignState.load(state_file)
    assert state.tasks["a"].status == "pr_open"


def test_syncs_base_branch_before_each_task_when_not_dry_run(tmp_path, monkeypatch):
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git("init", "-b", "main", cwd=repo_path)
    _git("config", "user.email", "test@example.com", cwd=repo_path)
    _git("config", "user.name", "Test", cwd=repo_path)
    (repo_path / "README.md").write_text("initial\n")
    _git("add", "-A", cwd=repo_path)
    _git("commit", "-m", "initial commit", cwd=repo_path)

    sync_calls = []
    monkeypatch.setattr(
        campaign_module,
        "sync_base_branch",
        lambda repo_path, base_branch: sync_calls.append((repo_path, base_branch)),
    )

    state_file = tmp_path / "state.json"
    cursor_client = ScriptedMergeCursorClient(
        {"orchestrator/a": ["merged"], "orchestrator/b": ["merged"]}
    )
    feature_planner = FakeFeaturePlannerClient(_two_task_feature_plan())
    gate = ScriptedHumanGate()

    result = _runner(
        cursor_client, feature_planner, gate, dry_run=False, repo_path=str(repo_path)
    ).run("build the architecture", state_file)

    assert result.completed
    assert sync_calls == [(str(repo_path), "main")] * 2
