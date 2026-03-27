from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_jj_user_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    xdg_config_home = tmp_path / "xdg-config"
    home.mkdir()
    xdg_config_home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))
