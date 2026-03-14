from pathlib import Path

import pytest

from jj_review.config import CONFIG_DIRNAME, CONFIG_FILENAME, ConfigError, load_config


def test_load_config_returns_defaults_when_config_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    config = load_config(repo_root=tmp_path)

    assert config.logging.level == "INFO"
    assert config.repo.github_host == "github.com"
    assert config.repo.remote is None


def test_load_config_reads_repository_and_logging_sections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _default_config_path(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                "[repo]",
                'remote = "origin"',
                'trunk_branch = "main"',
                "",
                "[logging]",
                'level = "DEBUG"',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    config = load_config(repo_root=tmp_path)

    assert config.repo.remote == "origin"
    assert config.repo.trunk_branch == "main"
    assert config.logging.level == "DEBUG"


def test_load_config_rejects_missing_explicit_config_path(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"

    with pytest.raises(ConfigError, match="Config file does not exist"):
        load_config(repo_root=tmp_path, config_path=config_path)


def test_load_config_applies_matching_repo_path_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo_root = tmp_path / "repos" / "example"
    repo_root.mkdir(parents=True)
    config_path = _default_config_path(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                "[repo]",
                'remote = "origin"',
                "",
                f'[repositories."{tmp_path / "repos"}"]',
                'remote = "upstream"',
                "",
                f'[repositories."{repo_root}"]',
                'trunk_branch = "main"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    config = load_config(repo_root=repo_root)

    assert config.repo.remote == "upstream"
    assert config.repo.trunk_branch == "main"


def test_load_config_reads_per_change_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change_id = "zvlywqkxtmnpqrstu"
    config_path = _default_config_path(tmp_path)
    config_path.write_text(
        "\n".join(
            [
                f'[change."{change_id}"]',
                'bookmark_override = "review/custom-name"',
                "draft = true",
                "skip = false",
                "",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    config = load_config(repo_root=tmp_path)

    assert config.change[change_id].bookmark_override == "review/custom-name"
    assert config.change[change_id].draft is True
    assert config.change[change_id].skip is False


def test_load_config_rejects_unexpected_top_level_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _default_config_path(tmp_path)
    config_path.write_text('remote = "origin"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))

    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        load_config(repo_root=tmp_path)


def _default_config_path(tmp_path: Path) -> Path:
    config_root = tmp_path / "config-home" / CONFIG_DIRNAME
    config_root.mkdir(parents=True, exist_ok=True)
    return config_root / CONFIG_FILENAME
