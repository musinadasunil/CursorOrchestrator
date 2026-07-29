import subprocess

import pytest

from cursor_orchestrator.clients.base import PullRequest
from cursor_orchestrator.clients.cursor_cli_client import CursorCliClient
from cursor_orchestrator.models import SubTask


def _client(test_command=None, test_timeout_seconds=None) -> CursorCliClient:
    return CursorCliClient(
        model="m",
        cursor_agent_timeout_seconds=5,
        gh_timeout_seconds=5,
        test_command=test_command,
        test_timeout_seconds=test_timeout_seconds,
    )


def test_run_real_tests_reports_a_real_pass(tmp_path):
    client = _client(test_command="true", test_timeout_seconds=5)
    result = client._run_real_tests(SubTask(id="x", description="x"), str(tmp_path))
    assert result.passed is True
    assert "exited 0" in result.details


def test_run_real_tests_reports_a_real_failure_not_a_self_report(tmp_path):
    # This is exactly the gap being closed: passed comes from a real exit
    # code, not from anything an agent claims about its own tests.
    client = _client(test_command="false", test_timeout_seconds=5)
    result = client._run_real_tests(SubTask(id="x", description="x"), str(tmp_path))
    assert result.passed is False
    assert "exited 1" in result.details


def test_run_real_tests_captures_real_output(tmp_path):
    client = _client(test_command="echo hello-from-real-test-run && false", test_timeout_seconds=5)
    result = client._run_real_tests(SubTask(id="x", description="x"), str(tmp_path))
    assert result.passed is False
    assert "hello-from-real-test-run" in result.details


def test_run_real_tests_times_out_instead_of_hanging(tmp_path):
    client = _client(test_command="sleep 5", test_timeout_seconds=0.1)
    result = client._run_real_tests(SubTask(id="x", description="x"), str(tmp_path))
    assert result.passed is False
    assert "timed out" in result.details


def test_write_and_run_tests_requires_test_command_to_be_configured():
    client = _client()  # no test_command
    with pytest.raises(RuntimeError, match="testing.command"):
        client.write_and_run_tests(SubTask(id="x", description="x"), "/tmp")


def _fake_gh_result(stdout: str) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


@pytest.mark.parametrize(
    "stdout,expected",
    [
        ('{"state": "OPEN", "mergedAt": null}', "open"),
        ('{"state": "MERGED", "mergedAt": "2026-01-01T00:00:00Z"}', "merged"),
        ('{"state": "CLOSED", "mergedAt": null}', "closed"),
    ],
)
def test_get_pr_merge_state(monkeypatch, stdout, expected):
    client = _client()
    monkeypatch.setattr(client, "_run_gh", lambda args: _fake_gh_result(stdout))
    pr = PullRequest(url="https://example.invalid/pr/1", branch="feature/x", head_sha="sha")
    assert client.get_pr_merge_state(pr) == expected


def test_plan_features_parses_features_and_tasks(monkeypatch):
    client = _client()
    payload = (
        '{"summary": "s", "features": [{"id": "f1", "description": "d1", '
        '"tasks": [{"id": "t1", "description": "td1", "depends_on": []}]}]}'
    )
    monkeypatch.setattr(client, "_run_cursor_agent", lambda args, stdin_input=None: _fake_gh_result(payload))
    feature_plan = client.plan_features("build the architecture")
    assert feature_plan.summary == "s"
    assert feature_plan.features[0].id == "f1"
    assert feature_plan.features[0].tasks[0].id == "t1"
