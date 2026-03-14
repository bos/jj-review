from pathlib import Path

import pytest

from jj_review.config import CONFIG_FILENAME, ConfigError, load_config


def test_load_config_returns_defaults_when_config_is_missing(tmp_path: Path) -> None:
    config = load_config(repo_root=tmp_path)

    assert config.logging.level == "INFO"
    assert config.repo.github_host == "github.com"
    assert config.repo.remote is None


def test_load_config_reads_repository_and_logging_sections(tmp_path: Path) -> None:
    config_path = tmp_path / CONFIG_FILENAME
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

    config = load_config(repo_root=tmp_path)

    assert config.repo.remote == "origin"
    assert config.repo.trunk_branch == "main"
    assert config.logging.level == "DEBUG"


def test_load_config_rejects_missing_explicit_config_path(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"

    with pytest.raises(ConfigError, match="Config file does not exist"):
        load_config(repo_root=tmp_path, config_path=config_path)


def test_load_config_ignores_cache_sections(tmp_path: Path) -> None:
    config_path = tmp_path / CONFIG_FILENAME
    config_path.write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[repo]",
                'remote = "origin"',
                "",
                '[change."zvlywqkxtmnpqrstu"]',
                'bookmark = "review/fix-cache-invalidation-zvlywqkx"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(repo_root=tmp_path)

    assert config.repo.remote == "origin"


def test_load_config_rejects_unexpected_top_level_keys(tmp_path: Path) -> None:
    config_path = tmp_path / CONFIG_FILENAME
    config_path.write_text(
        "\n".join(
            [
                'remote = "origin"',
                "",
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="Extra inputs are not permitted"):
        load_config(repo_root=tmp_path)
