from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


class BranchManagerError(RuntimeError):
    pass


class MergeConflictError(BranchManagerError):
    pass


@dataclass
class Worktree:
    subtask_id: str
    path: str
    branch: str


class BranchManager:
    """Owns the single feature branch's lifecycle.

    One feature branch is created off base_branch and is the only branch
    that ever becomes the PR. Each subtask in an active group gets its own
    worktree, checked out from the feature branch's current HEAD; once a
    group completes, each worktree is merged back into the feature branch
    sequentially. A merge conflict is a hard stop -- never auto-resolved.
    """

    def __init__(self, repo_path: str, base_branch: str, feature_branch: str) -> None:
        self.repo_path = Path(repo_path)
        self.base_branch = base_branch
        self.feature_branch = feature_branch
        self._worktree_root = Path(tempfile.mkdtemp(prefix="cursor-orchestrator-"))
        self._worktrees: dict[str, Worktree] = {}
        self.base_branch_sha_at_creation: str | None = None

    def create_feature_branch(self) -> None:
        self._run(["git", "checkout", self.base_branch])
        self.base_branch_sha_at_creation = self.get_head_sha(self.base_branch)
        self._run(["git", "checkout", "-b", self.feature_branch])

    def create_worktree(self, subtask_id: str) -> Worktree:
        branch = f"{self.feature_branch}-{subtask_id}"
        path = self._worktree_root / subtask_id
        self._run(["git", "worktree", "add", "-b", branch, str(path), self.feature_branch])
        worktree = Worktree(subtask_id=subtask_id, path=str(path), branch=branch)
        self._worktrees[subtask_id] = worktree
        return worktree

    def commit_worktree(self, subtask_id: str, message: str) -> None:
        worktree = self._worktrees[subtask_id]
        self._run(["git", "add", "-A"], cwd=worktree.path)
        self._run(["git", "commit", "-m", message, "--allow-empty"], cwd=worktree.path)

    def merge_worktree(self, subtask_id: str) -> None:
        """Merge one subtask's worktree branch into the feature branch.

        Callers must process subtasks within a group one at a time so each
        merge sees the result of the previous one.
        """
        worktree = self._worktrees[subtask_id]
        self._run(["git", "checkout", self.feature_branch])
        result = subprocess.run(
            ["git", "merge", "--no-ff", worktree.branch, "-m", f"merge {subtask_id}"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=self.repo_path, capture_output=True)
            raise MergeConflictError(
                f"merge conflict merging subtask {subtask_id!r} "
                f"(branch {worktree.branch!r}) into {self.feature_branch!r} -- "
                f"escalate to a human, do not auto-resolve:\n"
                f"{result.stdout}\n{result.stderr}"
            )

    def merge_ref_into_feature_branch(self, ref: str) -> None:
        """Merge `ref` (e.g. the base branch) into the feature branch, in
        place, in the main repo working directory -- not a worktree. The
        feature branch is already checked out there for the whole run (no
        subtask worktrees are active by the time babysitting starts, since
        each group's worktrees are merged and removed before the next group
        or the PR is opened), so there's nothing to create here; git also
        won't let the same branch be checked out in a second worktree while
        it's checked out here. Hard stop on conflict -- aborts and raises
        MergeConflictError, never auto-resolved, same policy as
        `merge_worktree`.
        """
        self._run(["git", "checkout", self.feature_branch])
        result = subprocess.run(
            ["git", "merge", "--no-ff", ref, "-m", f"merge {ref} into {self.feature_branch}"],
            cwd=self.repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            subprocess.run(["git", "merge", "--abort"], cwd=self.repo_path, capture_output=True)
            raise MergeConflictError(
                f"merge conflict merging {ref!r} into {self.feature_branch!r} -- "
                f"escalate to a human, do not auto-resolve:\n"
                f"{result.stdout}\n{result.stderr}"
            )

    def remove_worktree(self, subtask_id: str) -> None:
        worktree = self._worktrees.pop(subtask_id, None)
        if worktree is None:
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", worktree.path],
            cwd=self.repo_path,
            capture_output=True,
        )

    def get_head_sha(self, branch: str | None = None) -> str:
        result = self._run(["git", "rev-parse", branch or self.feature_branch])
        return result.stdout.strip()

    def cleanup(self) -> None:
        for subtask_id in list(self._worktrees):
            self.remove_worktree(subtask_id)

    def _run(self, cmd: list[str], cwd: Path | str | None = None) -> subprocess.CompletedProcess:
        result = subprocess.run(
            cmd, cwd=str(cwd or self.repo_path), capture_output=True, text=True
        )
        if result.returncode != 0:
            raise BranchManagerError(f"command failed: {' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
        return result


class NullBranchManager:
    """Dry-run stand-in with the same interface, no subprocess/git calls.

    Keeps the orchestrator's control flow identical between dry-run and
    real runs -- dry-run must never touch the user's actual repo.
    """

    def __init__(self, base_branch: str, feature_branch: str) -> None:
        self.base_branch = base_branch
        self.feature_branch = feature_branch
        self.repo_path = f"[dry-run]/{feature_branch}"
        self.base_branch_sha_at_creation: str | None = None

    def create_feature_branch(self) -> None:
        self.base_branch_sha_at_creation = self.get_head_sha(self.base_branch)

    def create_worktree(self, subtask_id: str) -> Worktree:
        return Worktree(subtask_id=subtask_id, path=f"[dry-run]/{subtask_id}", branch=f"{self.feature_branch}-{subtask_id}")

    def commit_worktree(self, subtask_id: str, message: str) -> None:
        pass

    def merge_worktree(self, subtask_id: str) -> None:
        pass

    def merge_ref_into_feature_branch(self, ref: str) -> None:
        pass

    def remove_worktree(self, subtask_id: str) -> None:
        pass

    def get_head_sha(self, branch: str | None = None) -> str:
        # Stable per branch -- dry-run never simulates real drift here;
        # base-branch-moved detection is covered by test_babysitter.py's
        # fakes instead, where the sequence of returned SHAs is scripted.
        return f"dryrun-branch-sha-{branch or self.feature_branch}"

    def cleanup(self) -> None:
        pass
