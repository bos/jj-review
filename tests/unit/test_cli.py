from pathlib import Path

import pytest

from jj_review import ui
from jj_review.cli import _normalize_cli_args, build_parser, main
from jj_review.errors import CliError


@pytest.fixture(autouse=True)
def no_configured_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jj_review.cli._load_configured_jj_color", lambda **kwargs: None)


def test_main_reports_invalid_config_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "bad.toml"
    config_path.write_text("[jj-review.repo]\nremote = [\n", encoding="utf-8")

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
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "bad-logging.toml"
    config_path.write_text('[jj-review.logging]\nlevel = "DEBIG"\n', encoding="utf-8")

    exit_code = main(["--config", str(config_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Invalid logging level" in captured.err
    assert "DEBIG" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["submit", "--draft=new", "@"], ["submit", "--draft", "@"]),
        (["submit", "--draft=all", "@"], ["submit", "--draft-all", "@"]),
        (
            ["help", "cleanup", "--color=never"],
            ["--color=never", "help", "cleanup"],
        ),
        (
            ["cleanup", "--help", "--color=never"],
            ["--color=never", "help", "cleanup"],
        ),
    ],
)
def test_normalize_cli_args_rewrites_shorthand_forms(
    args: list[str],
    expected: list[str],
) -> None:
    assert _normalize_cli_args(args) == expected


def test_normalize_cli_args_rejects_invalid_draft_mode() -> None:
    with pytest.raises(CliError) as exc_info:
        _normalize_cli_args(["submit", "--draft=oops", "@"])

    assert "Invalid value for --draft" in str(exc_info.value)


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


def test_main_renders_semantic_cli_errors_without_flattening_first(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_status(**kwargs) -> int:
        raise CliError(("Problem at ", ui.change_id("abcdefgh1234")))

    monkeypatch.setattr("jj_review.cli.commands.status.status", fake_status)

    exit_code = main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Error: Problem at abcdefgh" in captured.err


def test_main_renders_inline_backtick_help_spans_without_literal_backticks(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--color=never", "help", "cleanup"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "`--restack`" not in captured.out
    assert "`--dry-run`" not in captured.out
    assert "--restack" in captured.out
    assert "--dry-run" in captured.out
