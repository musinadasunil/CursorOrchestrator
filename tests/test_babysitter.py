import subprocess
from pathlib import Path

import pytest

from cursor_orchestrator.babysitter import Babysitter
from cursor_orchestrator.branch_manager import BranchManager
from cursor_orchestrator.clients.base import CIStatus, CursorClientBase, PullRequest, ReviewComment
from cursor_orchestrator.config import (
    GitConfig,
    LimitsConfig,
    ModelsConfig,
    OrchestratorConfig,
    TestingConfig as OrchestratorTestingConfig,
)
from cursor_orchestrator.models import TestResult as SubtaskTestResult


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


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    repo_path.mkdir()
    _git("init", "-b", "main", cwd=repo_path)
    _git("config", "user.email", "test@example.com", cwd=repo_path)
    _git("config", "user.name", "Test", cwd=repo_path)
    (repo_path / "README.md").write_text("initial\n")
    _git("add", "-A", cwd=repo_path)
    _git("commit", "-m", "initial commit", cwd=repo_path)
    return repo_path


class FakeTestClient:
    def __init__(self, passed: bool = True):
        self.passed = passed
        self.calls = 0

    def write_and_run_tests(self, subtask, worktree_path):
        self.calls += 1
        return SubtaskTestResult(subtask_id=subtask.id, passed=self.passed, details="fake test run")


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


def test_babysit_merges_clean_base_drift_and_retests_before_pushing(repo: Path):
    bm = BranchManager(str(repo), base_branch="main", feature_branch="feature/drift")
    bm.create_feature_branch()

    # Simulate base branch drift: something lands on main after the
    # feature branch (and its eventual PR) already exist.
    _git("checkout", "main", cwd=repo)
    (repo / "new_on_main.txt").write_text("landed on main after the PR opened\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "unrelated change on main", cwd=repo)
    _git("checkout", "feature/drift", cwd=repo)

    client = FakeCursorClient()
    client.statuses = [CIStatus(state="success", failing_checks=[], logs={})]
    client.comments = [[]]
    test_client = FakeTestClient(passed=True)

    pr = PullRequest(url="https://example.invalid/pr/drift", branch="feature/drift", head_sha="sha-0")
    outcome = Babysitter(_config(), client, test_client=test_client, branch_manager=bm).babysit(
        pr, "original prompt"
    )

    assert outcome == "clean"
    assert test_client.calls == 1
    assert client.push_count == 1  # merge pushed directly; tests passed, no implementer fix needed
    assert (repo / "new_on_main.txt").exists()  # merge actually landed on the feature branch
    bm.cleanup()


def test_babysit_escalates_on_base_drift_merge_conflict(repo: Path):
    bm = BranchManager(str(repo), base_branch="main", feature_branch="feature/conflict")
    bm.create_feature_branch()
    (repo / "README.md").write_text("changed on feature branch\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "feature branch edits README", cwd=repo)

    # Conflicting edit lands on main after the feature branch diverged.
    _git("checkout", "main", cwd=repo)
    (repo / "README.md").write_text("changed on main, conflicting\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "main also edits README", cwd=repo)
    _git("checkout", "feature/conflict", cwd=repo)

    client = FakeCursorClient()  # no statuses queued -- CI must never be checked
    test_client = FakeTestClient(passed=True)

    pr = PullRequest(url="https://example.invalid/pr/conflict", branch="feature/conflict", head_sha="sha-0")
    outcome = Babysitter(_config(), client, test_client=test_client, branch_manager=bm).babysit(
        pr, "original prompt"
    )

    assert outcome == "escalated"
    assert test_client.calls == 0  # never got past the conflict to re-test
    assert client.push_count == 0  # never pushed anything
    bm.cleanup()


def test_babysit_fixes_via_implementer_when_base_drift_merge_breaks_tests(repo: Path):
    bm = BranchManager(str(repo), base_branch="main", feature_branch="feature/drift-breaks")
    bm.create_feature_branch()

    _git("checkout", "main", cwd=repo)
    (repo / "new_on_main.txt").write_text("landed on main\n")
    _git("add", "-A", cwd=repo)
    _git("commit", "-m", "unrelated change on main", cwd=repo)
    _git("checkout", "feature/drift-breaks", cwd=repo)

    client = FakeCursorClient()
    client.statuses = [CIStatus(state="success", failing_checks=[], logs={})]
    client.comments = [[]]
    client.head_shas = ["sha-0"]  # matches pr.head_sha, so the fix's HEAD check passes
    test_client = FakeTestClient(passed=False)  # merge "breaks" tests

    pr = PullRequest(
        url="https://example.invalid/pr/drift-breaks", branch="feature/drift-breaks", head_sha="sha-0"
    )
    outcome = Babysitter(_config(), client, test_client=test_client, branch_manager=bm).babysit(
        pr, "original prompt"
    )

    assert outcome == "clean"
    assert test_client.calls == 1
    assert client.push_count == 1  # went through _push_fix's implementer path, then pushed
