from __future__ import annotations

import json
import subprocess

from cursor_orchestrator.clients.base import (
    CIStatus,
    CursorClientBase,
    PullRequest,
    ReviewComment,
    TestClientBase,
)
from cursor_orchestrator.models import Plan, SubTask, TestResult

# NOTE (see PLAN.md "Honest risk notes"): the cursor-agent CLI surface is a
# moving target. Every subprocess call below is a starting point, not
# gospel -- run `cursor-agent --help` against your installed version and
# adjust flags before a first real (non-dry-run) run.
#
# PR/CI operations go through the `gh` CLI (GitHub) rather than a Cursor
# API, since PR lifecycle and CI status are a hosting-provider concern,
# not an agent concern. Requires `gh auth login` to already be done.


class CursorCliClient(CursorClientBase, TestClientBase):
    """Real implementation via subprocess calls to `cursor-agent` (plan +
    implement) and `gh` (PR lifecycle + CI status). One instance is
    constructed per role (planner/implementer/tester) with that role's
    configured model -- see config.yaml's `models` section.
    """

    def __init__(self, model: str) -> None:
        self.model = model

    def plan(self, prompt: str) -> Plan:
        result = self._run_cursor_agent(["--mode", "plan", "--model", self.model, prompt])
        payload = json.loads(result.stdout)
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

    def implement_subtask(
        self, subtask: SubTask, worktree_path: str, original_prompt: str
    ) -> tuple[str, str]:
        result = self._run_cursor_agent(
            [
                "--mode",
                "background",
                "--model",
                self.model,
                "--cwd",
                worktree_path,
                f"{original_prompt}\n\nYour subtask: {subtask.description}",
            ]
        )
        payload = json.loads(result.stdout)
        return payload["diff"], payload["rationale"]

    def write_and_run_tests(self, subtask: SubTask, worktree_path: str) -> TestResult:
        result = self._run_cursor_agent(
            [
                "--mode",
                "background",
                "--model",
                self.model,
                "--cwd",
                worktree_path,
                f"Write and run tests, including edge cases and failure paths, "
                f"for: {subtask.description}",
            ]
        )
        payload = json.loads(result.stdout)
        return TestResult(
            subtask_id=subtask.id, passed=payload["passed"], details=payload.get("details", "")
        )

    def create_pr(self, branch: str, base_branch: str, title: str, body: str) -> PullRequest:
        result = self._run_gh(
            ["pr", "create", "--head", branch, "--base", base_branch, "--title", title,
             "--body", body, "--json", "url,headRefName,headRefOid"]
        )
        payload = json.loads(result.stdout)
        return PullRequest(url=payload["url"], branch=branch, head_sha=payload["headRefOid"])

    def get_pr_status(self, pr: PullRequest) -> CIStatus:
        result = self._run_gh(
            ["pr", "checks", pr.branch, "--json", "name,state,link"]
        )
        checks = json.loads(result.stdout)
        failing = [c["name"] for c in checks if c["state"] not in ("SUCCESS", "success")]
        state = "success" if not failing else "failure"
        logs = {name: self._fetch_check_log(pr, name) for name in failing}
        return CIStatus(state=state, failing_checks=failing, logs=logs)

    def get_pr_review_comments(self, pr: PullRequest) -> list[ReviewComment]:
        result = self._run_gh(
            ["pr", "view", pr.branch, "--json", "reviews", "-q", ".reviews"]
        )
        reviews = json.loads(result.stdout or "[]")
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
            ["gh", "run", "view", "--log-failed"], capture_output=True, text=True
        )
        return result.stdout[-2000:]

    def _run_cursor_agent(self, args: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run(["cursor-agent", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"cursor-agent failed (verify flags against `cursor-agent --help`): "
                f"{result.stderr}"
            )
        return result

    def _run_gh(self, args: list[str]) -> subprocess.CompletedProcess:
        result = subprocess.run(["gh", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"gh command failed: {result.stderr}")
        return result
