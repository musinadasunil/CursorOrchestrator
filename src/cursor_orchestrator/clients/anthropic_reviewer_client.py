from __future__ import annotations

import json

import anthropic

from cursor_orchestrator.clients.base import ReviewerClientBase
from cursor_orchestrator.models import ReviewFinding, ReviewResult, SubTaskResult, Verdict
from cursor_orchestrator.prompts import REVIEWER_SYSTEM_PROMPT

_RESULT_SCHEMA_HINT = """\
Respond with ONLY a JSON object of this exact shape, no prose outside it:
{
  "verdict": "ready_for_pr" | "needs_revision",
  "findings": [{"subtask_id": "...", "severity": "blocking"|"major"|"minor", "message": "..."}],
  "revision_requests": {"<subtask_id>": "<concrete instructions>"}
}
"""


class AnthropicReviewerClient(ReviewerClientBase):
    """Real reviewer implementation using the Anthropic SDK -- deliberately
    a different model than whatever implements the subtasks (config.py
    enforces reviewer != implementer at load time).
    """

    def __init__(self, model: str, client: anthropic.Anthropic | None = None) -> None:
        self.model = model
        self.client = client or anthropic.Anthropic()

    def review(
        self, original_prompt: str, plan_summary: str, subtask_results: list[SubTaskResult]
    ) -> ReviewResult:
        # Auto-block on any failed test result before spending a model call
        # confirming what's already a fact -- see PLAN.md's single-verdict
        # design (Reviewer is the only place a verdict is produced, but a
        # failing TestResult short-circuits it).
        failing = [r for r in subtask_results if r.test_result and not r.test_result.passed]
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

        user_message = self._build_user_message(original_prompt, plan_summary, subtask_results)
        response = self.client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=REVIEWER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
        text = "".join(block.text for block in response.content if block.type == "text")
        payload = json.loads(text)

        return ReviewResult(
            verdict=Verdict(payload["verdict"]),
            findings=[
                ReviewFinding(
                    subtask_id=f["subtask_id"], severity=f["severity"], message=f["message"]
                )
                for f in payload.get("findings", [])
            ],
            revision_requests=payload.get("revision_requests", {}),
        )

    def _build_user_message(
        self, original_prompt: str, plan_summary: str, subtask_results: list[SubTaskResult]
    ) -> str:
        subtasks_text = "\n\n".join(
            f"### Subtask {r.subtask.id}: {r.subtask.description}\n"
            f"Rationale: {r.rationale}\n"
            f"Test result: {'PASS' if r.test_result and r.test_result.passed else 'FAIL'} "
            f"({r.test_result.details if r.test_result else 'no test result'})\n"
            f"Diff:\n```\n{r.diff}\n```"
            for r in subtask_results
        )
        return (
            f"Original prompt (review against this, not a summary of it):\n{original_prompt}\n\n"
            f"Plan summary: {plan_summary}\n\n"
            f"{subtasks_text}\n\n{_RESULT_SCHEMA_HINT}"
        )
