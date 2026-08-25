"""Shared fixtures for the unit test suite."""

from __future__ import annotations

import pytest

import jj_stack.cli as cli_module


@pytest.fixture
def no_configured_color(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run without a `jj` color setting, as repos outside this suite do."""

    monkeypatch.setattr(cli_module, "_load_configured_jj_color", lambda **kwargs: None)
