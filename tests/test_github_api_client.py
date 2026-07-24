import subprocess
from pathlib import Path

import pytest

from cursor_orchestrator.clients.github_api_client import GithubApiClient, GithubApiError


def _git(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)


@pytest.fixture
def repo_with_origin(tmp_path: Path):
    def _make(origin_url: str) -> Path:
        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        _git("init", "-b", "main", cwd=repo_path)
        _git("remote", "add", "origin", origin_url, cwd=repo_path)
        return repo_path

    return _make


@pytest.mark.parametrize(
    "origin_url,expected",
    [
        ("git@github.com:owner/repo.git", ("owner", "repo", "https://api.github.com")),
        ("https://github.com/owner/repo.git", ("owner", "repo", "https://api.github.com")),
        ("https://github.com/owner/repo", ("owner", "repo", "https://api.github.com")),
        (
            "git@ghe.example.com:owner/repo.git",
            ("owner", "repo", "https://ghe.example.com/api/v3"),
        ),
        (
            "https://ghe.example.com/owner/repo.git",
            ("owner", "repo", "https://ghe.example.com/api/v3"),
        ),
        (
            # CI checkouts often set origin with an embedded credential.
            "https://x-access-token:abc123@github.com/owner/repo.git",
            ("owner", "repo", "https://api.github.com"),
        ),
    ],
)
def test_detects_owner_repo_and_api_base_from_origin(repo_with_origin, origin_url, expected):
    repo_path = repo_with_origin(origin_url)
    client = GithubApiClient(agent_client=object(), repo_path=str(repo_path), token="fake-token")
    assert (client.owner, client.repo, client.api_base) == expected


def test_missing_token_fails_fast(repo_with_origin, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    repo_path = repo_with_origin("git@github.com:owner/repo.git")
    with pytest.raises(GithubApiError, match="GITHUB_TOKEN"):
        GithubApiClient(agent_client=object(), repo_path=str(repo_path))


def test_pr_number_parsed_from_url():
    from cursor_orchestrator.clients.base import PullRequest

    client = GithubApiClient.__new__(GithubApiClient)  # skip __init__, no repo/token needed
    pr = PullRequest(url="https://github.com/owner/repo/pull/42", branch="feature/x", head_sha="sha")
    assert client._pr_number(pr) == 42


def test_pr_number_raises_on_unparseable_url():
    from cursor_orchestrator.clients.base import PullRequest

    client = GithubApiClient.__new__(GithubApiClient)
    pr = PullRequest(url="https://example.invalid/not-a-pr-url", branch="feature/x", head_sha="sha")
    with pytest.raises(GithubApiError):
        client._pr_number(pr)
