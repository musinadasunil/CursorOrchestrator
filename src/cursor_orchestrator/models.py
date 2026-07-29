from __future__ import annotations

from collections import Counter
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
        duplicates = sorted(
            tid for tid, count in Counter(t.id for t in self.subtasks).items() if count > 1
        )
        if duplicates:
            # Planner output is model-generated, not guaranteed unique -- a
            # silent dict-comprehension overwrite below would drop one of
            # the colliding subtasks from execution while `self.subtasks`
            # (the raw list, e.g. what the human-gate printout shows) still
            # lists both, which is a correctness bug worth failing loudly on.
            raise ValueError(f"duplicate subtask ids in plan: {duplicates}")

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


@dataclass
class PlanCritiqueFinding:
    severity: str  # "blocking" | "major" | "minor" -- advisory only, see PlanCritique
    message: str


@dataclass
class Task:
    id: str
    description: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class Feature:
    id: str
    description: str
    tasks: list[Task]


@dataclass
class FeaturePlan:
    """The top tier of planning, one level above Plan/SubTask: an entire
    architecture decomposed into features, each into small tasks. Each
    Task is sized to become exactly one branch and one PR -- small enough
    for a human to review -- and is itself handed to the normal
    plan/build/review/PR machinery (its own Plan/SubTask breakdown) once
    its turn comes up in campaign.py's sequential execution.
    """

    summary: str
    features: list[Feature]

    def ordered_tasks(self) -> list[Task]:
        """Flatten every feature's tasks into one strict sequential build
        order, respecting depends_on across the whole plan (not just
        within a feature). Unlike Plan.parallel_groups(), there is no
        batching -- campaign.py always builds one task at a time -- so
        this returns a flat list: a stable topological sort, ties broken
        by declared feature/task order.
        """
        all_tasks = [t for f in self.features for t in f.tasks]

        duplicates = sorted(
            tid for tid, count in Counter(t.id for t in all_tasks).items() if count > 1
        )
        if duplicates:
            raise ValueError(f"duplicate task ids in feature plan: {duplicates}")

        by_id = {t.id: t for t in all_tasks}
        for task in all_tasks:
            for dep in task.depends_on:
                if dep not in by_id:
                    raise ValueError(
                        f"task {task.id!r} depends on unknown task {dep!r}"
                    )

        remaining = {t.id: set(t.depends_on) for t in all_tasks}
        done: set[str] = set()
        ordered: list[Task] = []

        while remaining:
            ready = [t for t in all_tasks if t.id in remaining and remaining[t.id] <= done]
            if not ready:
                cycle = ", ".join(sorted(remaining))
                raise ValueError(f"dependency cycle detected among tasks: {cycle}")
            for task in ready:
                ordered.append(task)
                done.add(task.id)
                del remaining[task.id]

        return ordered


@dataclass
class PlanCritique:
    """Advisory second opinion on the plan itself -- decomposition, missing
    or unnecessary depends_on edges, over/under-granular subtasks. Shown to
    the human alongside the plan at the scope-approval gate.

    Deliberately has no verdict field and never auto-blocks anything: the
    Reviewer role is the system's one and only verdict authority (see
    PLAN.md's single-verdict-authority design). Adding a second thing that
    looks like a gate here would reintroduce exactly the "who wins"
    ambiguity that design was built to avoid. This is pure input to the
    human's own approve/edit/abort decision, same spirit as the code
    reviewer's findings being labeled "second opinion -- verify," not fact.
    """

    findings: list[PlanCritiqueFinding] = field(default_factory=list)
