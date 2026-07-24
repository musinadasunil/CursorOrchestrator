from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from cursor_orchestrator.models import Plan, ReviewResult, SubTask, SubTaskResult, TestResult


@dataclass
class CIStatus:
    state: str  # "pending" | "success" | "failure"
    failing_checks: list[str]
    logs: dict[str, str]  # check name -> failure log excerpt


@dataclass
class ReviewComment:
    id: str
    body: str
    file: str | None = None


@dataclass
class PullRequest:
    url: str
    branch: str
    head_sha: str


class CursorClientBase(ABC):
    """Talks to whatever implements planning/implementation/PR lifecycle.

    Nothing else in the orchestrator imports a specific SDK -- only this
    interface, so swapping implementations doesn't touch orchestrator logic.
    """

    @abstractmethod
    def plan(self, prompt: str) -> Plan: ...

    @abstractmethod
    def implement_subtask(
        self, subtask: SubTask, worktree_path: str, original_prompt: str
    ) -> tuple[str, str]:
        """Returns (diff, rationale)."""

    @abstractmethod
    def create_pr(
        self, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequest: ...

    @abstractmethod
    def get_pr_status(self, pr: PullRequest) -> CIStatus: ...

    @abstractmethod
    def get_pr_review_comments(self, pr: PullRequest) -> list[ReviewComment]: ...

    @abstractmethod
    def get_branch_head_sha(self, branch: str) -> str: ...

    @abstractmethod
    def push_fix_commit(self, branch: str, worktree_path: str, message: str) -> str:
        """Pushes whatever is staged in worktree_path as a fix commit; returns new HEAD SHA."""


class TestClientBase(ABC):
    @abstractmethod
    def write_and_run_tests(self, subtask: SubTask, worktree_path: str) -> TestResult: ...


class ReviewerClientBase(ABC):
    @abstractmethod
    def review(
        self, original_prompt: str, plan_summary: str, subtask_results: list[SubTaskResult]
    ) -> ReviewResult: ...
