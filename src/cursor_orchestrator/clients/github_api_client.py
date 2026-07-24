from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request

from cursor_orchestrator.clients.base import (
    CIStatus,
    CursorClientBase,
    PullRequest,
    ReviewComment,
)
from cursor_orchestrator.models import Plan, SubTask

# Alternative to cursor_cli_client.py's `gh`-based PR lifecycle, for
# environments where the `gh` CLI itself isn't allowed (e.g. an org-locked
# machine) but direct HTTPS to GitHub's REST API is fine. Talks to the API
# with stdlib `urllib` only -- no extra dependency.
#
# Planning/implementing/testing are unrelated to GitHub hosting, so this
# class does NOT reimplement them -- it wraps another CursorClientBase
# (in practice, a CursorCliClient) and delegates plan/implement_subtask/
# get_branch_head_sha/push_fix_commit straight through (the last two are
# already plain `git` subprocess calls with no `gh` involved). Only
# create_pr/get_pr_status/get_pr_review_comments -- the actual GitHub-
# hosting-specific operations -- are reimplemented here against the REST
# API.


class GithubApiError(RuntimeError):
    pass


class GithubApiClient(CursorClientBase):
    def __init__(
        self,
        agent_client: CursorClientBase,
        repo_path: str,
        token: str | None = None,
    ) -> None:
        self.agent_client = agent_client
        self.repo_path = repo_path
        self.token = token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not self.token:
            raise GithubApiError(
                "GITHUB_TOKEN (or GH_TOKEN) must be set to use the REST API PR backend -- "
                "create a token with 'repo' scope (or fine-grained PR read/write) and "
                "export it before running with git.pr_backend: api."
            )
        self.owner, self.repo, self.api_base = self._detect_from_origin(repo_path)

    def plan(self, prompt: str) -> Plan:
        return self.agent_client.plan(prompt)

    def implement_subtask(
        self, subtask: SubTask, worktree_path: str, original_prompt: str
    ) -> tuple[str, str]:
        return self.agent_client.implement_subtask(subtask, worktree_path, original_prompt)

    def get_branch_head_sha(self, branch: str) -> str:
        return self.agent_client.get_branch_head_sha(branch)

    def push_fix_commit(self, branch: str, worktree_path: str, message: str) -> str:
        return self.agent_client.push_fix_commit(branch, worktree_path, message)

    def create_pr(self, branch: str, base_branch: str, title: str, body: str) -> PullRequest:
        payload = self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/pulls",
            {"title": title, "head": branch, "base": base_branch, "body": body},
        )
        return PullRequest(url=payload["html_url"], branch=branch, head_sha=payload["head"]["sha"])

    def get_pr_status(self, pr: PullRequest) -> CIStatus:
        # Checks API only (GitHub Actions and most modern CI post here). If
        # your CI posts only to the older classic commit-status API instead
        # of Checks, this won't see it -- extend this method to also query
        # `/commits/{sha}/status` if that's your setup.
        payload = self._request(
            "GET", f"/repos/{self.owner}/{self.repo}/commits/{pr.head_sha}/check-runs"
        )
        runs = payload.get("check_runs", [])
        pending = any(r.get("status") != "completed" for r in runs)
        failing_runs = {
            r["name"]: r
            for r in runs
            if r.get("status") == "completed"
            and r.get("conclusion") not in ("success", "neutral", "skipped")
        }
        state = "failure" if failing_runs else ("pending" if pending else "success")
        logs = {name: r.get("details_url", "") for name, r in failing_runs.items()}
        return CIStatus(state=state, failing_checks=list(failing_runs), logs=logs)

    def get_pr_review_comments(self, pr: PullRequest) -> list[ReviewComment]:
        number = self._pr_number(pr)
        reviews = self._request(
            "GET", f"/repos/{self.owner}/{self.repo}/pulls/{number}/reviews"
        )
        return [
            ReviewComment(id=str(r["id"]), body=r.get("body", ""))
            for r in reviews
            if r.get("state") == "CHANGES_REQUESTED" and r.get("body")
        ]

    def _pr_number(self, pr: PullRequest) -> int:
        match = re.search(r"/pull/(\d+)", pr.url)
        if not match:
            raise GithubApiError(f"couldn't parse a PR number out of {pr.url!r}")
        return int(match.group(1))

    def _detect_from_origin(self, repo_path: str) -> tuple[str, str, str]:
        """Parses the `origin` remote to get (owner, repo, api_base_url) --
        no config needed for the common case. Handles GitHub.com and GitHub
        Enterprise, both SSH (`git@host:owner/repo.git`) and HTTPS remote
        forms (including HTTPS remotes with embedded credentials, e.g. CI
        checkouts that set `origin` to `https://x-access-token:***@host/...`).
        Verify `api_base` against your GHE instance's actual API path if
        this guesses wrong -- `/api/v3` is the standard convention but
        instance setups vary.
        """
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GithubApiError(
                f"couldn't read the 'origin' remote in {repo_path!r} to figure out "
                f"owner/repo: {result.stderr}"
            )
        url = result.stdout.strip()
        if "://" in url:
            parsed = urllib.parse.urlsplit(url)
            host = parsed.hostname or ""
            path = parsed.path
        else:
            # SCP-like syntax: [user@]host:owner/repo.git
            _, _, after_at = url.rpartition("@")
            host, _, path = after_at.partition(":")
        path = path.strip("/")
        if path.endswith(".git"):
            path = path[: -len(".git")]
        if "/" not in path:
            raise GithubApiError(f"couldn't parse owner/repo out of origin remote {url!r}")
        owner, repo = path.split("/", 1)
        api_base = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
        return owner, repo, api_base

    def _request(self, method: str, path: str, body: dict | None = None):
        req = urllib.request.Request(
            f"{self.api_base}{path}",
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            raise GithubApiError(
                f"GitHub API {method} {path} failed ({e.code}): {e.read().decode()}"
            ) from e
