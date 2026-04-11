from __future__ import annotations

from pathlib import Path

import pytest

import jj_review.config as config_module
from jj_review.config import load_config
from jj_review.errors import CliError


def test_load_config_returns_defaults_when_jj_config_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        config_module,
        "_default_config_paths",
        lambda repo_root: (tmp_path / "user.toml", tmp_path / "repo.toml"),
    )

    config = load_config(repo_root=tmp_path)

    assert config.logging.level == "WARNING"
    assert config.repo.labels == []


def test_load_config_merges_jj_config_layers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change_id = "zvlywqkxtmnpqrstu"
    user_path = tmp_path / "user.toml"
    repo_path = tmp_path / "repo.toml"
    workspace_path = tmp_path / "workspace.toml"
    monkeypatch.setattr(
        config_module,
        "_default_config_paths",
        lambda repo_root: (user_path, repo_path, workspace_path),
    )
    _write_config(
        user_path,
        [
            "[jj-review.logging]",
            'level = "info"',
            "",
            f'[jj-review.change."{change_id}"]',
            'bookmark_override = "review/from-user"',
        ],
    )
    _write_config(
        repo_path,
        [
            "[jj-review.repo]",
            'reviewers = ["octocat"]',
            'team_reviewers = ["platform"]',
        ],
    )
    _write_config(
        workspace_path,
        [
            "[jj-review.repo]",
            'labels = ["needs-review"]',
        ],
    )

    config = load_config(repo_root=tmp_path)

    assert config.logging.level == "INFO"
    assert config.repo.reviewers == ["octocat"]
    assert config.repo.team_reviewers == ["platform"]
    assert config.repo.labels == ["needs-review"]
    assert config.change[change_id].bookmark_override == "review/from-user"


def test_load_config_reads_explicit_jj_review_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "explicit.toml"
    _write_config(
        config_path,
        [
            "[jj-review.repo]",
            'labels = ["needs-review"]',
            "",
            "[jj-review.logging]",
            'level = "DEBUG"',
        ],
    )

    config = load_config(repo_root=None, config_path=config_path)

    assert config.repo.labels == ["needs-review"]
    assert config.logging.level == "DEBUG"


def test_load_config_rejects_missing_explicit_config_path(tmp_path: Path) -> None:
    with pytest.raises(CliError, match="Config file does not exist"):
        load_config(repo_root=None, config_path=tmp_path / "missing.toml")


def test_load_config_ignores_unrelated_jj_config_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "user.toml"
    monkeypatch.setattr(config_module, "_default_config_paths", lambda repo_root: (config_path,))
    _write_config(
        config_path,
        [
            "[git]",
            'push-new-bookmarks = true',
        ],
    )

    config = load_config(repo_root=tmp_path)

    assert config.logging.level == "WARNING"
    assert config.repo.labels == []


def test_load_config_rejects_invalid_keys_inside_jj_review_section(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid.toml"
    _write_config(
        config_path,
        [
            "[jj-review]",
            'remote = "origin"',
        ],
    )

    with pytest.raises(CliError, match="Extra inputs are not permitted"):
        load_config(repo_root=None, config_path=config_path)


def test_load_config_rejects_invalid_logging_level_in_jj_review_section(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid-logging.toml"
    _write_config(
        config_path,
        [
            "[jj-review.logging]",
            'level = "DEBIG"',
        ],
    )

    with pytest.raises(CliError, match="Invalid logging level"):
        load_config(repo_root=None, config_path=config_path)


def _write_config(config_path: Path, lines: list[str]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
