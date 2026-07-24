from cursor_orchestrator.clients.mock_clients import (
    MockCursorClient,
    MockReviewerClient,
    MockTestClient,
)
from cursor_orchestrator.config import GitConfig, LimitsConfig, ModelsConfig, OrchestratorConfig
from cursor_orchestrator.orchestrator import Orchestrator


def _config(**limit_overrides) -> OrchestratorConfig:
    limits = dict(
        max_parallel_agents=3,
        max_review_iterations=3,
        max_babysit_iterations=5,
        max_babysit_wall_clock_minutes=60,
        babysit_poll_interval_seconds=0.01,
    )
    limits.update(limit_overrides)
    return OrchestratorConfig(
        models=ModelsConfig(planner="p", implementer="i", tester="t", reviewer="r"),
        limits=LimitsConfig(**limits),
        git=GitConfig(base_branch="main", pr_backend="gh"),
    )


class ScriptedHumanGate:
    """Drives the orchestrator's two human gates without a real terminal."""

    def __init__(self, scope_decision="approve", iteration_decision="accept_as_is"):
        self.scope_decision = scope_decision
        self.iteration_decision = iteration_decision
        self.scope_calls = 0
        self.iteration_calls = 0

    def approve_scope(self, plan):
        self.scope_calls += 1
        return self.scope_decision, ""

    def iteration_cap_reached(self, findings_summary):
        self.iteration_calls += 1
        return self.iteration_decision


def _orchestrator(human_gate, **limit_overrides) -> Orchestrator:
    return Orchestrator(
        config=_config(**limit_overrides),
        cursor_client=MockCursorClient(),
        test_client=MockTestClient(),
        reviewer_client=MockReviewerClient(),
        human_gate=human_gate,
        dry_run=True,
    )


def test_full_dry_run_opens_a_pr_after_the_simulated_revision_cycle():
    gate = ScriptedHumanGate(scope_decision="approve")
    result = _orchestrator(gate).run("add a widget")

    assert not result.aborted
    assert not result.took_over
    assert result.pr_url is not None
    assert gate.scope_calls == 1


def test_human_can_abort_at_scope_approval():
    gate = ScriptedHumanGate(scope_decision="abort")
    result = _orchestrator(gate).run("add a widget")

    assert result.aborted
    assert result.pr_url is None


def test_human_can_take_over_at_iteration_cap():
    # max_review_iterations=1 forces the cap on the very first needs_revision
    # (the mock's "core" subtask fails its first test, so this always fires).
    gate = ScriptedHumanGate(scope_decision="approve", iteration_decision="take_over")
    result = _orchestrator(gate, max_review_iterations=1).run("add a widget")

    assert result.took_over
    assert result.pr_url is None
    assert gate.iteration_calls == 1
