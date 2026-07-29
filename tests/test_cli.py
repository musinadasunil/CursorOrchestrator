import pytest

from cursor_orchestrator.cli import _parse_args


def test_prompt_can_be_passed_inline():
    args = _parse_args(["do the thing"])
    assert args.prompt == "do the thing"


def test_prompt_can_be_passed_via_file(tmp_path):
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("  build the whole architecture  \n")
    args = _parse_args(["--prompt-file", str(prompt_file)])
    assert args.prompt == "build the whole architecture"


def test_requires_exactly_one_prompt_source():
    with pytest.raises(SystemExit):
        _parse_args([])


def test_rejects_both_prompt_and_prompt_file(tmp_path):
    prompt_file = tmp_path / "prompt.md"
    prompt_file.write_text("x")
    with pytest.raises(SystemExit):
        _parse_args(["inline prompt", "--prompt-file", str(prompt_file)])


def test_errors_on_missing_prompt_file(tmp_path):
    missing = tmp_path / "nope.md"
    with pytest.raises(SystemExit):
        _parse_args(["--prompt-file", str(missing)])


def test_errors_on_empty_prompt_file(tmp_path):
    prompt_file = tmp_path / "empty.md"
    prompt_file.write_text("   \n")
    with pytest.raises(SystemExit):
        _parse_args(["--prompt-file", str(prompt_file)])
