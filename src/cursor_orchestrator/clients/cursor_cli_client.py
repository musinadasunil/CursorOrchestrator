from __future__ import annotations

import json
import subprocess

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
from cursor_orchestrator.prompts import PLAN_CRITIC_SYSTEM_PROMPT, REVIEWER_SYSTEM_PROMPT

# NOTE (see PLAN.md "Honest risk notes"): the cursor-agent CLI surface is a
# moving target. Every subprocess call below is a starting point, not
# gospel -- run `cursor-agent --help` against your installed version and
# adjust flags before a first real (non-dry-run) run. In particular, the
# prompt/task text is passed via stdin rather than as a trailing argv
# element (see _run_cursor_agent) on the assumption that `cursor-agent`
# reads it that way when no positional prompt is given -- verify this; if
# it doesn't support stdin, you'll need to pass it as a positional arg
# instead, understanding the ARG_MAX/escaping risk that reintroduces for
# large payloads (e.g. a multi-subtask review's diffs).
#
# PR/CI operations go through the `gh` CLI (GitHub) rather than a Cursor
# API, since PR lifecycle and CI status are a hosting-provider concern,
# not an agent concern. Requires `gh auth login` to already be done.
#
# write_and_run_tests() does NOT trust the agent's self-report of whether
# its own tests pass -- it independently runs testing.command (config.yaml)
# as a real subprocess and uses its actual exit code. The agent is only
# asked to write the test code, which does need an LLM; verification is
# deliberately not delegated to it. Known simplification: the same
# configured command runs for every subtask at the worktree root, not
# scoped to just that subtask's files -- there's no attempt at
# test-selection/impact analysis here.

_REVIEW_RESULT_SCHEMA_HINT = """\
Respond with ONLY a JSON object of this exact shape, no prose outside it:
{
  "verdict": "ready_for_pr" | "needs_revision",
  "findings": [{"subtask_id": "...", "severity": "blocking"|"major"|"minor", "message": "..."}],
  "revision_requests": {"<subtask_id>": "<concrete instructions>"}
}
"""

_PLAN_CRITIQUE_SCHEMA_HINT = """\
Respond with ONLY a JSON object of this exact shape, no prose outside it:
{
  "findings": [{"severity": "blocking"|"major"|"minor", "message": "..."}]
}
"""


class CursorCliClient(CursorClientBase, TestClientBase, ReviewerClientBase, PlanCriticClientBase):
    """Real implementation via subprocess calls to `cursor-agent` (plan,
    critique, implement, test, and review) and `gh` (PR lifecycle + CI
    status). One instance is constructed per role (planner/plan_critic/
    implementer/tester/reviewer) with that role's configured model -- see
    config.yaml's `models` section. The reviewer role is just another
    `cursor-agent` invocation pointed at a different model than the
    implementer -- config.py enforces `models.reviewer != models.implementer`
    (and `models.plan_critic != models.planner`) at load time so
    "independent second opinion" stays a structural guarantee, not a
    convention.
    """

    def __init__(
        self,
        model: str,
        cursor_agent_timeout_seconds: float,
        gh_timeout_seconds: float,
        test_command: str | None = None,
        test_timeout_seconds: float | None = None,
    ) -> None:
        self.model = model
        self.cursor_agent_timeout_seconds = cursor_agent_timeout_seconds
        self.gh_timeout_seconds = gh_timeout_seconds
        # Only meaningful for the instance constructed for the tester role
        # (see cli.py) -- write_and_run_tests() below requires these to
        # actually verify anything, rather than trusting the agent's own
        # self-report of whether its tests passed.
        self.test_command = test_command
        self.test_timeout_seconds = test_timeout_seconds

    def plan(self, prompt: str) -> Plan:
        result = self._run_cursor_agent(
            ["--mode", "plan", "--model", self.model], stdin_input=prompt
        )
        payload = self._parse_json(result.stdout, "planning")
        return Plan(
            summary=payload["summary"],
            subtasks=[
                SubTask(
                    id=s["id"],
                    description=s["description"],
                    files_likely_touched=s.get("files_likely_touched", []),
                    depends_on=s.get("depends_on", []),
                )
                for s in payload["subtasks"]
            ],
        )

    def critique(self, original_prompt: str, plan: Plan) -> PlanCritique:
        subtasks_text = "\n".join(
            f"- [{t.id}] {t.description} "
            f"(files_likely_touched={t.files_likely_touched}, depends_on={t.depends_on})"
            for t in plan.subtasks
        )
        prompt = (
            f"{PLAN_CRITIC_SYSTEM_PROMPT}\n\n"
            f"Original prompt:\n{original_prompt}\n\n"
            f"Plan summary: {plan.summary}\n\nSubtasks:\n{subtasks_text}\n\n"
            f"{_PLAN_CRITIQUE_SCHEMA_HINT}"
        )
        result = self._run_cursor_agent(
            ["--mode", "print", "--model", self.model], stdin_input=prompt
        )
        payload = self._parse_json(result.stdout, "critiquing the plan")
        return PlanCritique(
            findings=[
                PlanCritiqueFinding(severity=f["severity"], message=f["message"])
                for f in payload.get("findings", [])
            ]
        )

    def implement_subtask(
        self, subtask: SubTask, worktree_path: str, original_prompt: str
    ) -> tuple[str, str]:
        task_prompt = f"{original_prompt}\n\nYour subtask: {subtask.description}"
        result = self._run_cursor_agent(
            ["--mode", "background", "--model", self.model, "--cwd", worktree_path],
            stdin_input=task_prompt,
        )
        payload = self._parse_json(result.stdout, f"implementing subtask {subtask.id!r}")
        return payload["diff"], payload["rationale"]

    def write_and_run_tests(self, subtask: SubTask, worktree_path: str) -> TestResult:
        if not self.test_command:
            raise RuntimeError(
                "testing.command must be set in config.yaml for the Tester role -- "
                "without it there's no independent way to verify test results, only "
                "the agent's own self-report of whether its tests passed."
            )
        # The agent writes the test code (that part genuinely needs an LLM);
        # whether they *pass* is then verified independently by actually
        # running the configured test command below, rather than trusting
        # whatever the agent claims about its own tests -- see PLAN.md's
        # risk note on this. No particular output format is required from
        # this call, so there's nothing to parse/trust here.
        task_prompt = (
            f"Write tests, including edge cases and failure paths, for: "
            f"{subtask.description}\n\n"
            f"Your tests will be run independently afterwards with: "
            f"{self.test_command!r} -- you don't need to report whether they pass."
        )
        self._run_cursor_agent(
            ["--mode", "background", "--model", self.model, "--cwd", worktree_path],
            stdin_input=task_prompt,
        )
        return self._run_real_tests(subtask, worktree_path)

    def _run_real_tests(self, subtask: SubTask, worktree_path: str) -> TestResult:
        try:
            result = subprocess.run(
                self.test_command,
                shell=True,
                cwd=worktree_path,
                capture_output=True,
                text=True,
                timeout=self.test_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            return TestResult(
                subtask_id=subtask.id,
                passed=False,
                details=(
                    f"test command timed out after {self.test_timeout_seconds}s: "
                    f"{self.test_command!r}"
                ),
            )
        output = (result.stdout + result.stderr)[-4000:]
        return TestResult(
            subtask_id=subtask.id,
            passed=result.returncode == 0,
            details=f"`{self.test_command}` exited {result.returncode}\n{output}",
        )

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

        prompt = (
            f"{REVIEWER_SYSTEM_PROMPT}\n\n"
            f"Original prompt (review against this, not a summary of it):\n{original_prompt}\n\n"
            f"Plan summary: {plan_summary}\n\n"
            f"{self._render_subtask_results(subtask_results)}\n\n{_REVIEW_RESULT_SCHEMA_HINT}"
        )
        result = self._run_cursor_agent(
            ["--mode", "print", "--model", self.model], stdin_input=prompt
        )
        payload = self._parse_json(result.stdout, "reviewing")
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

    def _render_subtask_results(self, subtask_results: list[SubTaskResult]) -> str:
        return "\n\n".join(
            f"### Subtask {r.subtask.id}: {r.subtask.description}\n"
            f"Rationale: {r.rationale}\n"
            f"Test result: {'PASS' if r.test_result and r.test_result.passed else 'FAIL'} "
            f"({r.test_result.details if r.test_result else 'no test result'})\n"
            f"Diff:\n```\n{r.diff}\n```"
            for r in subtask_results
        )

    def create_pr(self, branch: str, base_branch: str, title: str, body: str) -> PullRequest:
        result = self._run_gh(
            ["pr", "create", "--head", branch, "--base", base_branch, "--title", title,
             "--body", body, "--json", "url,headRefName,headRefOid"]
        )
        payload = self._parse_json(result.stdout, "creating the PR")
        return PullRequest(url=payload["url"], branch=branch, head_sha=payload["headRefOid"])

    def get_pr_status(self, pr: PullRequest) -> CIStatus:
        result = self._run_gh(
            ["pr", "checks", pr.branch, "--json", "name,state,link"]
        )
        checks = self._parse_json(result.stdout, "reading PR checks")
        failing = [c["name"] for c in checks if c["state"] not in ("SUCCESS", "success")]
        state = "success" if not failing else "failure"
        logs = {name: self._fetch_check_log(pr, name) for name in failing}
        return CIStatus(state=state, failing_checks=failing, logs=logs)

    def get_pr_review_comments(self, pr: PullRequest) -> list[ReviewComment]:
        result = self._run_gh(
            ["pr", "view", pr.branch, "--json", "reviews", "-q", ".reviews"]
        )
        reviews = self._parse_json(result.stdout or "[]", "reading PR review comments")
        return [
            ReviewComment(id=str(r.get("id", i)), body=r.get("body", ""))
            for i, r in enumerate(reviews)
            if r.get("state") == "CHANGES_REQUESTED" and r.get("body")
        ]

    def get_branch_head_sha(self, branch: str) -> str:
        result = subprocess.run(
            ["git", "rev-parse", branch], capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def push_fix_commit(self, branch: str, worktree_path: str, message: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=worktree_path, check=True)
        subprocess.run(["git", "commit", "-m", message, "--allow-empty"], cwd=worktree_path, check=True)
        subprocess.run(["git", "push", "origin", branch], cwd=worktree_path, check=True)
        return self.get_branch_head_sha(branch)

    def _fetch_check_log(self, pr: PullRequest, check_name: str) -> str:
        # Best-effort: gh doesn't have a single documented "get failing log
        # excerpt" command across all CI providers. This shells out to the
        # run-log viewer and truncates; verify this against your CI setup.
        result = subprocess.run(
            ["gh", "run", "view", "--log-failed"],
            capture_output=True,
            text=True,
            timeout=self.gh_timeout_seconds,
        )
        return result.stdout[-2000:]

    def _run_cursor_agent(
        self, args: list[str], stdin_input: str | None = None
    ) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["cursor-agent", *args],
                input=stdin_input,
                capture_output=True,
                text=True,
                timeout=self.cursor_agent_timeout_seconds,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"cursor-agent timed out after {self.cursor_agent_timeout_seconds}s "
                f"(args: {args}) -- a hung agent process would otherwise hang the whole "
                f"orchestrator forever. If this is a false positive on a legitimately slow "
                f"task, raise limits.cursor_agent_timeout_seconds in config.yaml."
            ) from e
        if result.returncode != 0:
            raise RuntimeError(
                f"cursor-agent failed (verify flags against `cursor-agent --help`): "
                f"{result.stderr}"
            )
        return result

    def _run_gh(self, args: list[str]) -> subprocess.CompletedProcess:
        try:
            result = subprocess.run(
                ["gh", *args], capture_output=True, text=True, timeout=self.gh_timeout_seconds
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"gh timed out after {self.gh_timeout_seconds}s (args: {args}) -- "
                f"raise limits.gh_timeout_seconds in config.yaml if this is a false positive."
            ) from e
        if result.returncode != 0:
            raise RuntimeError(f"gh command failed: {result.stderr}")
        return result

    def _parse_json(self, stdout: str, context: str):
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(
                f"couldn't parse JSON from stdout while {context} -- either the CLI's output "
                f"format doesn't match what's expected here, or it printed something other "
                f"than pure JSON (e.g. progress logs) to stdout. Got: {stdout[:500]!r}"
            ) from e
