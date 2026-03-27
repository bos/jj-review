import logging
import subprocess
from unittest.mock import patch

import pytest

from jj_review.bootstrap import (
    _parse_jj_version,
    check_jj_version,
    configure_logging,
)
from jj_review.errors import CliError


def _fake_jj_version(version_string: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["jj", "--version"],
        returncode=0,
        stdout=f"jj {version_string}\n",
        stderr="",
    )


# --- _parse_jj_version ---


def test_parse_jj_version_stable() -> None:
    assert _parse_jj_version("jj 0.21.0") == (0, 21, 0)


def test_parse_jj_version_with_build_hash() -> None:
    assert _parse_jj_version("jj 0.39.0-d9689cd9b51b") == (0, 39, 0)


def test_parse_jj_version_returns_none_for_unexpected_format() -> None:
    assert _parse_jj_version("git version 2.40.0") is None
    assert _parse_jj_version("") is None
    assert _parse_jj_version("jj notaversion") is None


# --- check_jj_version ---


def test_check_jj_version_accepts_minimum_version() -> None:
    with patch("subprocess.run", return_value=_fake_jj_version("0.21.0")):
        check_jj_version()  # should not raise


def test_check_jj_version_accepts_newer_version() -> None:
    with patch("subprocess.run", return_value=_fake_jj_version("0.39.0-abc123")):
        check_jj_version()  # should not raise


def test_check_jj_version_rejects_older_version() -> None:
    with patch("subprocess.run", return_value=_fake_jj_version("0.20.0")):
        with pytest.raises(CliError, match="0.20.0 is too old"):
            check_jj_version()


def test_check_jj_version_rejects_unparseable_output() -> None:
    bad_output = subprocess.CompletedProcess(
        args=["jj", "--version"],
        returncode=0,
        stdout="not jj output\n",
        stderr="",
    )
    with patch("subprocess.run", return_value=bad_output):
        with pytest.raises(CliError, match="Could not parse"):
            check_jj_version()


def test_check_jj_version_raises_when_jj_not_installed() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError):
        with pytest.raises(CliError, match="not installed or is not on PATH"):
            check_jj_version()


def test_check_jj_version_raises_when_version_command_fails() -> None:
    failed = subprocess.CompletedProcess(
        args=["jj", "--version"],
        returncode=1,
        stdout="",
        stderr="some error",
    )
    with patch("subprocess.run", return_value=failed):
        with pytest.raises(CliError, match="failed"):
            check_jj_version()


# --- configure_logging ---


def test_configure_logging_uses_warning_by_default() -> None:
    configure_logging(debug=False, configured_level="WARNING")

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("jj_review").getEffectiveLevel() == logging.WARNING


def test_configure_logging_uses_debug_when_requested() -> None:
    configure_logging(debug=True, configured_level="WARNING")

    assert logging.getLogger().getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("jj_review").getEffectiveLevel() == logging.DEBUG
    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("asyncio").getEffectiveLevel() == logging.WARNING
