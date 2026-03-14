import subprocess
import sys
from pathlib import Path

import pytest

from jj_review.cli import main
from jj_review.config import CONFIG_DIRNAME, CONFIG_FILENAME


def test_main_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "submit" in captured.out
    assert "cleanup" in captured.out


def test_main_reports_invalid_config_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "bad.toml"
    config_path.write_text("[repo]\nremote = [\n", encoding="utf-8")

    exit_code = main(["--config", str(config_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
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

    exit_code = main(["--repository", str(tmp_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Invalid logging level" in captured.err
    assert "DEBIG" in captured.err
    assert "Traceback" not in captured.err


def test_python_m_jj_review_prints_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "jj_review", "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert "JJ-native stacked GitHub review tooling" in completed.stdout


def test_importing_package_main_module_does_not_exit() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import jj_review.__main__; print('ok')"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"
    assert completed.stderr == ""
