from __future__ import annotations

from collections import defaultdict

from cursor_orchestrator.clients.base import (
    CIStatus,
    CursorClientBase,
    PlanCriticClientBase,
    PullRequest,
    ReviewComment,
    ReviewerClientBase,
    TestClientBase,
)
from cursor_orchestrator.models import (
    Plan,
    PlanCritique,
    PlanCritiqueFinding,
    ReviewFinding,
    ReviewResult,
    SubTask,
    SubTaskResult,
    TestResult,
    Verdict,
)

"""Fully working fakes -- no network calls.

Used by `--dry-run` to walk the entire state machine, including one
simulated needs_revision cycle (a subtask's tests fail once, then pass
after a "fix") and one simulated CI failure during babysitting (fixed on
the next poll), so dry-run is a real end-to-end exercise of the loop
logic, not just a plan/approve stub.
"""


class MockCursorClient(CursorClientBase):
    def __init__(self) -> None:
        self._implement_calls: dict[str, int] = defaultdict(int)
        self._sha_counter = 0
        self._pr_status_calls = 0

    def plan(self, prompt: str) -> Plan:
        return Plan(
            summary=f"[dry-run] plan for: {prompt}",
            subtasks=[
                SubTask(
                    id="core",
                    description="Implement the core change",
                    files_likely_touched=["src/example/core.py"],
                    depends_on=[],
                ),
                SubTask(
                    id="tests",
                    description="Cover the core change with tests",
                    files_likely_touched=["tests/test_core.py"],
                    depends_on=["core"],
                ),
            ],
        )

    def implement_subtask(
        self, subtask: SubTask, worktree_path: str, original_prompt: str
    ) -> tuple[str, str]:
        self._implement_calls[subtask.id] += 1
        attempt = self._implement_calls[subtask.id]
        target_file = subtask.files_likely_touched[0] if subtask.files_likely_touched else "unknown"
        diff = (
            f"--- a/{target_file}\n"
            f"+++ b/{target_file}\n"
            f"@@ dry-run attempt {attempt} for subtask {subtask.id} @@\n"
        )
        rationale = f"[dry-run] implemented {subtask.id!r} (attempt {attempt})"
        return diff, rationale

    def create_pr(
        self, branch: str, base_branch: str, title: str, body: str
    ) -> PullRequest:
        return PullRequest(
            url="https://example.invalid/dry-run/pr/1",
            branch=branch,
            head_sha=self._next_sha(),
        )

    def get_pr_status(self, pr: PullRequest) -> CIStatus:
        self._pr_status_calls += 1
        if self._pr_status_calls == 1:
            return CIStatus(
                state="failure",
                failing_checks=["unit-tests"],
                logs={"unit-tests": "[dry-run] simulated failure on first CI run"},
            )
        return CIStatus(state="success", failing_checks=[], logs={})

    def get_pr_review_comments(self, pr: PullRequest) -> list[ReviewComment]:
        return []

    def get_branch_head_sha(self, branch: str) -> str:
        return f"dryrun-sha-{self._sha_counter}"

    def push_fix_commit(self, branch: str, worktree_path: str, message: str) -> str:
        return self._next_sha()

    def _next_sha(self) -> str:
        self._sha_counter += 1
        return f"dryrun-sha-{self._sha_counter}"


class MockTestClient(TestClientBase):
    def __init__(self) -> None:
        self._run_calls: dict[str, int] = defaultdict(int)

    def write_and_run_tests(self, subtask: SubTask, worktree_path: str) -> TestResult:
        self._run_calls[subtask.id] += 1
        attempt = self._run_calls[subtask.id]
        if subtask.id == "core" and attempt == 1:
            return TestResult(
                subtask_id=subtask.id,
                passed=False,
                details="[dry-run] simulated edge-case failure on first attempt",
            )
        return TestResult(
            subtask_id=subtask.id,
            passed=True,
            details=f"[dry-run] tests passed on attempt {attempt}",
        )


class MockReviewerClient(ReviewerClientBase):
    def review(
        self, original_prompt: str, plan_summary: str, subtask_results: list[SubTaskResult]
    ) -> ReviewResult:
        failing = [
            r for r in subtask_results if r.test_result is not None and not r.test_result.passed
        ]
        if failing:
            return ReviewResult(
                verdict=Verdict.NEEDS_REVISION,
                findings=[
                    ReviewFinding(
                        subtask_id=r.subtask.id,
                        severity="blocking",
                        message=f"tests failed: {r.test_result.details}",
                    )
                    for r in failing
                ],
                revision_requests={r.subtask.id: "fix the failing tests" for r in failing},
            )

        return ReviewResult(
            verdict=Verdict.READY_FOR_PR,
            findings=[
                ReviewFinding(
                    subtask_id=r.subtask.id,
                    severity="minor",
                    message="[dry-run] second opinion placeholder -- verify",
                )
                for r in subtask_results
            ],
        )


class MockPlanCriticClient(PlanCriticClientBase):
    def critique(self, original_prompt: str, plan: Plan) -> PlanCritique:
        return PlanCritique(
            findings=[
                PlanCritiqueFinding(
                    severity="minor",
                    message="[dry-run] plan-critique placeholder -- decomposition looks reasonable",
                )
            ]
        )
