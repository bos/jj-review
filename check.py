#!/usr/bin/env python3
"""Run the standard local verification checks for this repository."""

from __future__ import annotations

import os
import shlex
import subprocess
from argparse import ArgumentParser
from collections.abc import Sequence
from pathlib import Path
from typing import Literal


def _venv_python_relative_path() -> Path:
    if os.name == "nt":
        return Path("Scripts/python.exe")
    return Path("bin/python")


REPO_ROOT = Path(__file__).resolve().parent
VENV_PYTHON = REPO_ROOT / ".venv" / _venv_python_relative_path()
PytestJobs = int | Literal["auto"]


def _parse_pytest_jobs(value: str) -> PytestJobs:
    if value == "auto":
        return "auto"
    try:
        parsed = int(value)
    except ValueError as error:
        raise ValueError("--pytest-jobs must be a positive integer or 'auto'") from error
    if parsed < 1:
        raise ValueError("--pytest-jobs must be a positive integer or 'auto'")
    return parsed


def _build_checks(
    *,
    pytest_jobs: PytestJobs | None,
    coverage: bool,
    concurrency_report: bool,
) -> tuple[tuple[str, tuple[str, ...]], ...]:
    pytest_command: tuple[str, ...] = ("-m", "pytest")
    if pytest_jobs in (None, "auto"):
        pytest_command = (*pytest_command, "-n", "auto")
    elif isinstance(pytest_jobs, int) and pytest_jobs > 1:
        pytest_command = (*pytest_command, "-n", str(pytest_jobs))
    if concurrency_report:
        pytest_command = (*pytest_command, "--concurrency-report")
    if coverage:
        pytest_command = (
            *pytest_command,
            "--cov=jj_review",
            "--cov-branch",
            "--cov-report=term-missing",
            "--cov-report=html:htmlcov",
        )
    return (
        ("ruff", ("-m", "ruff", "check")),
        ("pyrefly", ("-m", "pyrefly", "check")),
        ("pytest", pytest_command),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run Ruff, Pyrefly, and the test suite in sequence."""

    parser = ArgumentParser(
        prog="check.py",
        description="Run the local Ruff, pyrefly, and pytest checks.",
    )
    parser.add_argument(
        "-n",
        "--pytest-jobs",
        metavar="N",
        help="Run pytest with xdist using N workers or 'auto' (default: auto).",
    )
    parser.add_argument(
        "--coverage",
        action="store_true",
        help=(
            "Run pytest with branch coverage enabled and emit terminal and "
            "HTML reports in htmlcov/."
        ),
    )
    parser.add_argument(
        "--pytest-concurrency-report",
        action="store_true",
        help="Report observed test concurrency and highlight bottlenecks.",
    )
    args = parser.parse_args(argv)
    try:
        pytest_jobs = (
            None if args.pytest_jobs is None else _parse_pytest_jobs(args.pytest_jobs)
        )
    except ValueError as error:
        parser.error(str(error))
    ensure_project_environment()
    command_env = _project_command_env()

    for name, command in _build_checks(
        pytest_jobs=pytest_jobs,
        coverage=args.coverage,
        concurrency_report=args.pytest_concurrency_report,
    ):
        full_command = (str(VENV_PYTHON), *command)
        print(f"==> {name}: {shlex.join(full_command)}", flush=True)
        completed = subprocess.run(
            full_command,
            check=False,
            cwd=REPO_ROOT,
            env=command_env,
        )
        if completed.returncode != 0:
            return completed.returncode

    return 0


def ensure_project_environment() -> None:
    """Refresh the project virtualenv before running the verification suite."""

    sync_command = ("uv", "sync", "--locked")
    print(f"==> bootstrap: {shlex.join(sync_command)}", flush=True)
    completed = subprocess.run(
        sync_command,
        check=False,
        cwd=REPO_ROOT,
        env=_project_command_env(),
    )
    if completed.returncode != 0:
        raise SystemExit(completed.returncode)


def _project_command_env() -> dict[str, str]:
    """Return a subprocess environment pinned to the project virtualenv."""

    return {key: value for key, value in os.environ.items() if key != "VIRTUAL_ENV"}


if __name__ == "__main__":
    raise SystemExit(main())
