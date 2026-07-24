import pytest

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
