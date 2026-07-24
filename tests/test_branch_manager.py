import subprocess
from pathlib import Path

import pytest

from cursor_orchestrator.branch_manager import BranchManager, MergeConflictError


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


def test_create_worktree_and_merge_independent_subtasks(repo: Path):
    bm = BranchManager(str(repo), base_branch="main", feature_branch="feature/test")
    bm.create_feature_branch()

    wt_a = bm.create_worktree("a")
    (Path(wt_a.path) / "a.txt").write_text("from a\n")
    bm.commit_worktree("a", "implement a")

    wt_b = bm.create_worktree("b")
    (Path(wt_b.path) / "b.txt").write_text("from b\n")
    bm.commit_worktree("b", "implement b")

    bm.merge_worktree("a")
    bm.remove_worktree("a")
    bm.merge_worktree("b")
    bm.remove_worktree("b")

    assert (repo / "a.txt").exists()
    assert (repo / "b.txt").exists()
    bm.cleanup()


def test_merge_conflict_is_a_hard_stop(repo: Path):
    bm = BranchManager(str(repo), base_branch="main", feature_branch="feature/conflict")
    bm.create_feature_branch()

    # Both worktrees must branch off the same feature-branch HEAD *before*
    # either merges -- this is how a real parallel group works, and it's
    # what actually produces divergent history to conflict on. Creating
    # "b" only after "a" already merged would make b's edit just build on
    # top of a's (no conflict), which isn't what this test is checking.
    wt_a = bm.create_worktree("a")
    wt_b = bm.create_worktree("b")

    (Path(wt_a.path) / "README.md").write_text("changed by a\n")
    bm.commit_worktree("a", "implement a")

    (Path(wt_b.path) / "README.md").write_text("changed by b, conflicting\n")
    bm.commit_worktree("b", "implement b")

    bm.merge_worktree("a")
    bm.remove_worktree("a")

    with pytest.raises(MergeConflictError):
        bm.merge_worktree("b")

    bm.remove_worktree("b")
    bm.cleanup()


def test_get_head_sha_matches_git_rev_parse(repo: Path):
    bm = BranchManager(str(repo), base_branch="main", feature_branch="feature/sha")
    bm.create_feature_branch()
    expected = _git("rev-parse", "feature/sha", cwd=repo).stdout.strip()
    assert bm.get_head_sha() == expected
    bm.cleanup()
