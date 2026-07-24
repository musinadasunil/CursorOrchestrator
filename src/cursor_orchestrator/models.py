from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TaskStatus(Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    NEEDS_REVISION = "needs_revision"
    DONE = "done"


class Verdict(Enum):
    READY_FOR_PR = "ready_for_pr"
    NEEDS_REVISION = "needs_revision"


@dataclass
class SubTask:
    id: str
    description: str
    files_likely_touched: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING


@dataclass
class Plan:
    summary: str
    subtasks: list[SubTask]

    def parallel_groups(self) -> list[list[SubTask]]:
        """Topologically batch subtasks into dependency-ordered groups.

        Each group's subtasks have no dependency on one another and can
        run concurrently; groups themselves must run in order.
        """
        by_id = {t.id: t for t in self.subtasks}
        for task in self.subtasks:
            for dep in task.depends_on:
                if dep not in by_id:
                    raise ValueError(
                        f"subtask {task.id!r} depends on unknown subtask {dep!r}"
                    )

        remaining = {t.id: set(t.depends_on) for t in self.subtasks}
        done: set[str] = set()
        groups: list[list[SubTask]] = []

        while remaining:
            ready = [tid for tid, deps in remaining.items() if deps <= done]
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise ValueError(f"dependency cycle detected among subtasks: {cycle}")
            groups.append([by_id[tid] for tid in ready])
            done.update(ready)
            for tid in ready:
                del remaining[tid]

        return groups

    def chunked_parallel_groups(self, max_parallel_agents: int) -> list[list[SubTask]]:
        """Same as parallel_groups(), but no group exceeds max_parallel_agents.

        Oversized groups are split into sequential sub-batches rather than
        launching more concurrent agents than the configured cap allows.
        """
        chunked: list[list[SubTask]] = []
        for group in self.parallel_groups():
            for start in range(0, len(group), max_parallel_agents):
                chunked.append(group[start : start + max_parallel_agents])
        return chunked


@dataclass
class TestResult:
    subtask_id: str
    passed: bool
    details: str = ""


@dataclass
class ReviewFinding:
    subtask_id: str
    severity: str  # "blocking" | "major" | "minor"
    message: str


@dataclass
class ReviewResult:
    verdict: Verdict
    findings: list[ReviewFinding] = field(default_factory=list)
    revision_requests: dict[str, str] = field(default_factory=dict)


@dataclass
class SubTaskResult:
    subtask: SubTask
    diff: str
    rationale: str
    branch_or_worktree: str
    test_result: TestResult | None = None
