import pytest

from cursor_orchestrator.babysitter import Babysitter
from cursor_orchestrator.clients.base import CIStatus, CursorClientBase, PullRequest, ReviewComment
from cursor_orchestrator.config import GitConfig, LimitsConfig, ModelsConfig, OrchestratorConfig


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
        git=GitConfig(base_branch="main"),
    )


class FakeCursorClient(CursorClientBase):
    def __init__(self):
        self.statuses: list[CIStatus] = []
        self.comments: list[list[ReviewComment]] = []
        self.head_shas: list[str] = []
        self.push_count = 0

    def plan(self, prompt):
        raise NotImplementedError

    def implement_subtask(self, subtask, worktree_path, original_prompt):
        return "diff", "rationale"

    def create_pr(self, branch, base_branch, title, body):
        raise NotImplementedError

    def get_pr_status(self, pr):
        return self.statuses.pop(0)

    def get_pr_review_comments(self, pr):
        return self.comments.pop(0) if self.comments else []

    def get_branch_head_sha(self, branch):
        return self.head_shas.pop(0)

    def push_fix_commit(self, branch, worktree_path, message):
        self.push_count += 1
        return f"sha-after-push-{self.push_count}"


def test_babysit_resolves_a_transient_ci_failure():
    client = FakeCursorClient()
    client.statuses = [
        CIStatus(state="failure", failing_checks=["unit-tests"], logs={"unit-tests": "boom"}),
        CIStatus(state="success", failing_checks=[], logs={}),
    ]
    client.comments = [[], []]
    client.head_shas = ["sha-0"]  # matches pr.head_sha before the one push

    pr = PullRequest(url="https://example.invalid/pr/1", branch="feature/x", head_sha="sha-0")
    outcome = Babysitter(_config(), client).babysit(pr, "original prompt")

    assert outcome == "clean"
    assert client.push_count == 1


def test_babysit_escalates_on_repeated_same_failure():
    client = FakeCursorClient()
    failure = CIStatus(state="failure", failing_checks=["unit-tests"], logs={"unit-tests": "boom"})
    client.statuses = [failure, failure, failure]
    client.comments = [[], [], []]
    client.head_shas = ["sha-0", "sha-after-push-1"]

    pr = PullRequest(url="https://example.invalid/pr/2", branch="feature/y", head_sha="sha-0")
    outcome = Babysitter(_config(), client).babysit(pr, "original prompt")

    assert outcome == "escalated"


def test_babysit_backs_off_when_human_pushed_manually():
    client = FakeCursorClient()
    client.statuses = [CIStatus(state="failure", failing_checks=["unit-tests"], logs={})]
    client.comments = [[]]
    client.head_shas = ["sha-moved-by-human"]  # doesn't match pr.head_sha

    pr = PullRequest(url="https://example.invalid/pr/3", branch="feature/z", head_sha="sha-0")
    outcome = Babysitter(_config(), client).babysit(pr, "original prompt")

    assert outcome == "escalated"
    assert client.push_count == 0


def test_babysit_escalates_at_iteration_cap():
    client = FakeCursorClient()
    n = 3
    client.statuses = [
        CIStatus(state="failure", failing_checks=[f"check-{i}"], logs={}) for i in range(n)
    ]
    client.comments = [[] for _ in range(n)]
    client.head_shas = [f"sha-{i}" for i in range(n)]

    pr = PullRequest(url="https://example.invalid/pr/4", branch="feature/w", head_sha="sha-0")
    config = _config(max_babysit_iterations=n)

    # Make every push_fix_commit's returned sha match the *next* head_sha we
    # hand out, so the HEAD-SHA safety check never trips in this test --
    # we're specifically exercising the iteration cap here.
    def push(branch, worktree_path, message):
        client.push_count += 1
        return client.head_shas[0] if client.head_shas else "sha-final"

    client.push_fix_commit = push  # type: ignore[method-assign]
    # head_sha check reads get_branch_head_sha before each fix; keep it in
    # lockstep with pr.head_sha by re-using the same sequence.
    client.head_shas = ["sha-0"] * n

    outcome = Babysitter(config, client).babysit(pr, "original prompt")
    assert outcome == "escalated"
