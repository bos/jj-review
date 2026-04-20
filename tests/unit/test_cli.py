from pathlib import Path

import pytest

from jj_review import ui
from jj_review.cli import main
from jj_review.errors import CliError


@pytest.fixture(autouse=True)
def no_configured_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jj_review.cli._load_configured_jj_color", lambda **kwargs: None)


def test_main_reports_invalid_config_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "bad.toml"
    config_path.write_text("[jj-review]\nbookmark_prefix = [\n", encoding="utf-8")

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


def test_main_renders_cli_error_hint_on_separate_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_status(**kwargs) -> int:
        raise CliError("Problem at trunk.", hint="Run status --fetch and retry.")

    monkeypatch.setattr("jj_review.cli.commands.status.status", fake_status)

    exit_code = main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.splitlines() == [
        "Error: Problem at trunk.",
        "Hint: Run status --fetch and retry.",
    ]


@pytest.mark.parametrize("argv", [["help"], ["help", "--all"], ["help", "submit"]])
def test_main_help_smoke_renders_without_error(
    argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(argv)
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "jj-review" in captured.out
    assert "Traceback" not in captured.err
