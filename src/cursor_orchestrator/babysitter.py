from __future__ import annotations

import time

from cursor_orchestrator.clients.base import CursorClientBase, PullRequest
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

    There's exactly one feature branch and one PR (see branch_manager.py),
    so a fix doesn't need to be attributed back to a specific original
    subtask -- the implementer is simply asked to fix the branch, with the
    original prompt as context, same as every other stage.
    """

    def __init__(
        self,
        config: OrchestratorConfig,
        cursor_client: CursorClientBase,
        human_gate=None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        self.config = config
        self.cursor_client = cursor_client
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

        while True:
            if time.monotonic() > deadline:
                self._escalate(pr, "babysit wall-clock cap reached")
                return "escalated"
            if iterations >= self.config.limits.max_babysit_iterations:
                self._escalate(pr, "babysit iteration cap reached")
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

    def _push_fix(self, pr: PullRequest, instructions: str, original_prompt: str) -> None:
        current_sha = self.cursor_client.get_branch_head_sha(pr.branch)
        if current_sha != pr.head_sha:
            self._escalate(
                pr,
                f"branch HEAD moved since last read ({pr.head_sha} -> {current_sha}) -- "
                "a human likely pushed manually, backing off instead of overwriting",
            )
            raise BabysitterEscalation("head moved")

        fix_task = SubTask(id="pr-fix", description="babysit fix", files_likely_touched=[])
        _diff, _rationale = self.cursor_client.implement_subtask(
            fix_task, pr.branch, f"{original_prompt}\n\nFix requested: {instructions}"
        )
        new_sha = self.cursor_client.push_fix_commit(
            pr.branch, pr.branch, f"fix: {instructions[:72]}"
        )
        pr.head_sha = new_sha

    def _escalate(self, pr: PullRequest, reason: str) -> None:
        print(f"[babysitter] escalating to human on PR {pr.url}: {reason}")
