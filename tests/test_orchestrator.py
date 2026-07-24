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
from cursor_orchestrator.models import PlanCritique, PlanCritiqueFinding
from cursor_orchestrator.orchestrator import Orchestrator


def _config(**limit_overrides) -> OrchestratorConfig:
    limits = dict(
        max_parallel_agents=3,
        max_review_iterations=3,
        max_babysit_iterations=5,
        max_babysit_wall_clock_minutes=60,
        babysit_poll_interval_seconds=0.01,
        cursor_agent_timeout_seconds=60,
        gh_timeout_seconds=30,
    )
    limits.update(limit_overrides)
    return OrchestratorConfig(
        models=ModelsConfig(planner="p", plan_critic="c", implementer="i", tester="t", reviewer="r"),
        limits=LimitsConfig(**limits),
        git=GitConfig(base_branch="main", pr_backend="gh"),
        testing=OrchestratorTestingConfig(command="true", timeout_seconds=30),
    )


class ScriptedHumanGate:
    """Drives the orchestrator's two human gates without a real terminal."""

    def __init__(self, scope_decision="approve", iteration_decision="accept_as_is"):
        self.scope_decision = scope_decision
        self.iteration_decision = iteration_decision
        self.scope_calls = 0
        self.iteration_calls = 0
        self.last_critique = None

    def approve_scope(self, plan, critique=None):
        self.scope_calls += 1
        self.last_critique = critique
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
        plan_critic_client=MockPlanCriticClient(),
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


class FakePlanCriticClient:
    def __init__(self, critique: PlanCritique):
        self.critique_value = critique
        self.calls = 0

    def critique(self, original_prompt, plan):
        self.calls += 1
        return self.critique_value


def test_plan_critique_reaches_the_human_gate():
    critique = PlanCritique(
        findings=[PlanCritiqueFinding(severity="major", message="subtask 'core' is too coarse")]
    )
    critic = FakePlanCriticClient(critique)
    gate = ScriptedHumanGate(scope_decision="approve")
    orchestrator = Orchestrator(
        config=_config(),
        cursor_client=MockCursorClient(),
        test_client=MockTestClient(),
        reviewer_client=MockReviewerClient(),
        plan_critic_client=critic,
        human_gate=gate,
        dry_run=True,
    )

    orchestrator.run("add a widget")

    assert critic.calls == 1
    assert gate.last_critique is critique
    assert gate.last_critique.findings[0].message == "subtask 'core' is too coarse"
