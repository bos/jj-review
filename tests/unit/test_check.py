import importlib.util
import subprocess
from pathlib import Path

import pytest


def _load_check_module():
    module_path = Path(__file__).resolve().parents[2] / "check.py"
    spec = importlib.util.spec_from_file_location("repo_check", module_path)
    assert spec is not None
    assert spec.loader is not None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_script = _load_check_module()


def test_ensure_project_environment_syncs_even_when_venv_exists(
    monkeypatch,
    tmp_path: Path,
) -> None:
    venv_python = tmp_path / ".venv" / check_script._venv_python_relative_path()
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(check_script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_script, "VENV_PYTHON", venv_python)
    monkeypatch.setenv("VIRTUAL_ENV", "/tmp/already-active")

    commands: list[tuple[str, ...]] = []
    cwd_values: list[Path] = []
    env_values: list[dict[str, str]] = []

    def fake_run(
        command: tuple[str, ...],
        *,
        check: bool,
        cwd: Path,
        env: dict[str, str],
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        commands.append(command)
        cwd_values.append(cwd)
        env_values.append(env)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check_script.subprocess, "run", fake_run)

    check_script.ensure_project_environment()

    assert commands == [("uv", "sync", "--locked")]
    assert cwd_values == [tmp_path]
    assert len(env_values) == 1
    assert "VIRTUAL_ENV" not in env_values[0]


def test_main_adds_xdist_workers_when_requested(
    monkeypatch,
    tmp_path: Path,
) -> None:
    venv_python = tmp_path / ".venv" / check_script._venv_python_relative_path()
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(check_script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_script, "VENV_PYTHON", venv_python)
    monkeypatch.setattr(check_script, "ensure_project_environment", lambda: None)

    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        *,
        check: bool,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert cwd == tmp_path
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check_script.subprocess, "run", fake_run)

    exit_code = check_script.main(["-n", "4"])

    assert exit_code == 0
    assert commands == [
        (str(venv_python), "-m", "ruff", "check"),
        (str(venv_python), "-m", "pyrefly", "check"),
        (str(venv_python), "-m", "pytest", "-n", "4"),
    ]


def test_main_uses_auto_xdist_workers_by_default(
    monkeypatch,
    tmp_path: Path,
) -> None:
    venv_python = tmp_path / ".venv" / check_script._venv_python_relative_path()
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(check_script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_script, "VENV_PYTHON", venv_python)
    monkeypatch.setattr(check_script, "ensure_project_environment", lambda: None)

    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        *,
        check: bool,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert cwd == tmp_path
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check_script.subprocess, "run", fake_run)

    exit_code = check_script.main([])

    assert exit_code == 0
    assert commands == [
        (str(venv_python), "-m", "ruff", "check"),
        (str(venv_python), "-m", "pyrefly", "check"),
        (str(venv_python), "-m", "pytest", "-n", "auto"),
    ]


def test_main_adds_auto_xdist_workers_when_requested(
    monkeypatch,
    tmp_path: Path,
) -> None:
    venv_python = tmp_path / ".venv" / check_script._venv_python_relative_path()
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(check_script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_script, "VENV_PYTHON", venv_python)
    monkeypatch.setattr(check_script, "ensure_project_environment", lambda: None)

    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        *,
        check: bool,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert cwd == tmp_path
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check_script.subprocess, "run", fake_run)

    exit_code = check_script.main(["--pytest-jobs", "auto"])

    assert exit_code == 0
    assert commands == [
        (str(venv_python), "-m", "ruff", "check"),
        (str(venv_python), "-m", "pyrefly", "check"),
        (str(venv_python), "-m", "pytest", "-n", "auto"),
    ]


def test_main_runs_pytest_serially_when_requested(
    monkeypatch,
    tmp_path: Path,
) -> None:
    venv_python = tmp_path / ".venv" / check_script._venv_python_relative_path()
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="utf-8")
    monkeypatch.setattr(check_script, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(check_script, "VENV_PYTHON", venv_python)
    monkeypatch.setattr(check_script, "ensure_project_environment", lambda: None)

    commands: list[tuple[str, ...]] = []

    def fake_run(
        command: tuple[str, ...],
        *,
        check: bool,
        cwd: Path,
    ) -> subprocess.CompletedProcess[str]:
        assert check is False
        assert cwd == tmp_path
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(check_script.subprocess, "run", fake_run)

    exit_code = check_script.main(["-n", "1"])

    assert exit_code == 0
    assert commands == [
        (str(venv_python), "-m", "ruff", "check"),
        (str(venv_python), "-m", "pyrefly", "check"),
        (str(venv_python), "-m", "pytest"),
    ]


def test_main_rejects_non_positive_pytest_jobs() -> None:
    with pytest.raises(SystemExit, match="2"):
        check_script.main(["--pytest-jobs", "0"])


def test_main_rejects_invalid_pytest_jobs_value() -> None:
    with pytest.raises(SystemExit, match="2"):
        check_script.main(["-n", "banana"])
