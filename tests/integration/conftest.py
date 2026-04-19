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
    jj_config = tmp_path / "jj-test-config.toml"
    jj_config.write_text(
        '[revset-aliases]\n"trunk()" = "main"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))
    monkeypatch.setenv("JJ_USER", "Test User")
    monkeypatch.setenv("JJ_EMAIL", "test@example.com")
    monkeypatch.setenv("JJ_CONFIG", str(jj_config))
