from __future__ import annotations

import time

from cursor_orchestrator.branch_manager import MergeConflictError
from cursor_orchestrator.clients.base import CursorClientBase, PullRequest, TestClientBase
from cursor_orchestrator.config import OrchestratorConfig
from cursor_orchestrator.models import SubTask


class BabysitterEscalation(Exception):
    """Raised when the babysitter can't safely proceed and must hand off to a human."""


class Babysitter:
    """Step [7]: keeps watching an opened PR, routing CI failures and human
    review comments back through the implementer and pushing fix commits.
    Bounded by both an iteration cap and a wall-clock cap. Never approves
    its own PR, dismisses a review, merges, or force-pushes over a human's
    manual commit -- it checks the branch HEAD SHA before every push and
    backs off if it moved.

    Also watches the *base* branch: if it's moved since the last poll, it
    merges it into the PR branch immediately (never rebases -- a rebase
    would rewrite the branch's SHAs and require a force-push, which both
    defeats the manual-push HEAD-SHA check above and risks clobbering a
    human's local checkout of the same branch). A merge conflict is a hard
    stop, same policy as branch_manager.py's group-boundary merges: abort,
    escalate, never auto-resolve. A clean merge gets re-tested locally
    before being pushed, so "base moved" can't silently reintroduce a
    breakage that individual subtask tests never would have caught.

    There's exactly one feature branch and one PR (see branch_manager.py),
    so a fix doesn't need to be attributed back to a specific original
    subtask -- the implementer is simply asked to fix the branch, with the
    original prompt as context, same as every other stage.
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        cursor_client: CursorClientBase,
        test_client: TestClientBase | None = None,
        branch_manager=None,
        human_gate=None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self.config = config
        self.cursor_client = cursor_client
        self.test_client = test_client
        self.branch_manager = branch_manager
        self.human_gate = human_gate
        self.poll_interval_seconds = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else config.limits.babysit_poll_interval_seconds
        )

    def babysit(self, pr: PullRequest, original_prompt: str) -> str:
        """Returns terminal state: "clean" | "escalated"."""
        deadline = time.monotonic() + self.config.limits.max_babysit_wall_clock_minutes * 60
        iterations = 0
        last_failing: tuple[str, ...] | None = None
        repeat_count = 0
        # Baseline is the base branch's SHA when the feature branch was
        # created, not "whatever it is when babysitting starts" -- that way
        # drift picked up on this very first poll also covers anything that
        # landed on the base branch during the earlier build/review phase,
        # not just drift that happens while actively babysitting.
        last_base_sha = (
            self.branch_manager.base_branch_sha_at_creation
            if self.branch_manager is not None
            else None
        )

        while True:
            if time.monotonic() > deadline:
                self._escalate(pr, "babysit wall-clock cap reached")
                return "escalated"
            if iterations >= self.config.limits.max_babysit_iterations:
                self._escalate(pr, "babysit iteration cap reached")
                return "escalated"

            if self.branch_manager is not None:
                current_base_sha = self.branch_manager.get_head_sha(self.config.git.base_branch)
                if current_base_sha != last_base_sha:
                    last_base_sha = current_base_sha
                    try:
                        self._merge_base_into_pr_branch(pr, original_prompt)
                    except BabysitterEscalation:
                        return "escalated"

            status = self.cursor_client.get_pr_status(pr)
            comments = self.cursor_client.get_pr_review_comments(pr)

            if status.state == "success" and not comments:
                return "clean"

            if status.state == "failure":
                failing = tuple(sorted(status.failing_checks))
                repeat_count = repeat_count + 1 if failing == last_failing else 0
                last_failing = failing

                if repeat_count >= 2:
                    self._escalate(
                        pr, f"check(s) {failing!r} failed {repeat_count + 1} times in a row"
                    )
                    return "escalated"

                logs = "\n".join(f"{c}: {status.logs.get(c, '')}" for c in status.failing_checks)
                try:
                    self._push_fix(pr, f"CI failing: {logs}", original_prompt)
                except BabysitterEscalation:
                    return "escalated"

            for comment in comments:
                try:
                    self._push_fix(pr, f"review comment: {comment.body}", original_prompt)
                except BabysitterEscalation:
                    return "escalated"

            iterations += 1
            time.sleep(self.poll_interval_seconds)

    def _repo_path(self, pr: PullRequest) -> str:
        # The feature branch is already checked out in branch_manager's main
        # working directory for the whole run (build phase, then babysit) --
        # no separate worktree needed here. Falls back to the branch name
        # itself only when no branch_manager was supplied (unit tests).
        if self.branch_manager is not None:
            return str(self.branch_manager.repo_path)
        return pr.branch

    def _merge_base_into_pr_branch(self, pr: PullRequest, original_prompt: str) -> None:
        base_branch = self.config.git.base_branch
        repo_path = self._repo_path(pr)
        try:
            self.branch_manager.merge_ref_into_feature_branch(base_branch)
        except MergeConflictError as e:
            self._escalate(
                pr,
                f"{base_branch} moved and merging it into {pr.branch} conflicted -- "
                f"escalating instead of auto-resolving: {e}",
            )
            raise BabysitterEscalation("base merge conflict") from e

        if self.test_client is not None:
            fix_task = SubTask(
                id="pr-fix",
                description=f"re-test after merging {base_branch}",
                files_likely_touched=[],
            )
            test_result = self.test_client.write_and_run_tests(fix_task, repo_path)
            if not test_result.passed:
                self._push_fix(
                    pr,
                    f"merging {base_branch} broke tests: {test_result.details}",
                    original_prompt,
                )
                return

        new_sha = self.cursor_client.push_fix_commit(
            pr.branch, repo_path, f"merge {base_branch} into {pr.branch}"
        )
        pr.head_sha = new_sha

    def _push_fix(self, pr: PullRequest, instructions: str, original_prompt: str) -> None:
        current_sha = self.cursor_client.get_branch_head_sha(pr.branch)
        if current_sha != pr.head_sha:
            self._escalate(
                pr,
                f"branch HEAD moved since last read ({pr.head_sha} -> {current_sha}) -- "
                "a human likely pushed manually, backing off instead of overwriting",
            )
            raise BabysitterEscalation("head moved")

        repo_path = self._repo_path(pr)
        fix_task = SubTask(id="pr-fix", description="babysit fix", files_likely_touched=[])
        _diff, _rationale = self.cursor_client.implement_subtask(
            fix_task, repo_path, f"{original_prompt}\n\nFix requested: {instructions}"
        )
        new_sha = self.cursor_client.push_fix_commit(
            pr.branch, repo_path, f"fix: {instructions[:72]}"
        )
        pr.head_sha = new_sha

    def _escalate(self, pr: PullRequest, reason: str) -> None:
        print(f"[babysitter] escalating to human on PR {pr.url}: {reason}")
