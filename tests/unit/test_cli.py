from pathlib import Path

import pytest

from jj_review.cli import _normalize_cli_args, build_parser, main
from jj_review.config import CONFIG_DIRNAME, CONFIG_FILENAME
from jj_review.errors import CliError


def test_main_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "submit" in captured.out
    assert "land" in captured.out
    assert "close" in captured.out
    assert "import" in captured.out
    assert "cleanup" in captured.out
    assert "unlink" not in captured.out
    assert "relink" not in captured.out
    assert "completion" not in captured.out


def test_main_help_all_shows_hidden_commands(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["help", "--all"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "relink" in captured.out
    assert "unlink" in captured.out
    assert "completion" in captured.out


def test_main_help_command_prints_subcommand_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["help", "submit"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Usage: jj-review submit" in captured.out
    assert captured.err == ""


def test_help_command_rejects_invalid_option() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["help", "--version"])

    assert exc_info.value.code == 2


def test_main_time_output_prefixes_help_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--time-output"])
    captured = capsys.readouterr()

    assert exit_code == 0
    lines = [line for line in captured.out.splitlines() if line]
    assert lines
    assert all(line.startswith("[") for line in lines)
    assert any("submit" in line for line in lines)


def test_main_reports_invalid_config_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "bad.toml"
    config_path.write_text("[repo]\nremote = [\n", encoding="utf-8")

    exit_code = main(["--config", str(config_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.startswith("Error: ")
    assert "Invalid jj-review config" in captured.err
    assert "Traceback" not in captured.err


def test_main_reports_missing_repository_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "missing-repo"

    exit_code = main(["--repository", str(repository), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert str(repository) in captured.err
    assert "does not exist" in captured.err
    assert "Traceback" not in captured.err


def test_main_reports_invalid_logging_level_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config-home" / CONFIG_DIRNAME / CONFIG_FILENAME
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[logging]\nlevel = "DEBIG"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    exit_code = main(["--repository", str(tmp_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Invalid logging level" in captured.err
    assert "DEBIG" in captured.err
    assert "Traceback" not in captured.err


def test_normalize_cli_args_rewrites_draft_new() -> None:
    assert _normalize_cli_args(["submit", "--draft=new", "@"]) == [
        "submit",
        "--draft",
        "@",
    ]


def test_normalize_cli_args_rewrites_draft_all() -> None:
    assert _normalize_cli_args(["submit", "--draft=all", "@"]) == [
        "submit",
        "--draft-all",
        "@",
    ]


def test_normalize_cli_args_rejects_invalid_draft_mode() -> None:
    with pytest.raises(CliError) as exc_info:
        _normalize_cli_args(["submit", "--draft=oops", "@"])

    assert "Invalid value for `--draft`" in str(exc_info.value)


def test_main_submit_rejects_draft_and_publish_together() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["submit", "--draft", "--publish", "@"])

    assert exc_info.value.code == 2


def test_main_import_rejects_multiple_selectors() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["import", "--pull-request", "7", "--revset", "@"])

    assert exc_info.value.code == 2


def test_main_reports_non_jj_directory_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plain_dir = tmp_path / "not-a-jj-repo"
    plain_dir.mkdir()

    exit_code = main(["--repository", str(plain_dir), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Not inside a jj workspace" in captured.err
    assert "Traceback" not in captured.err
