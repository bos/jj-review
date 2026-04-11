from __future__ import annotations

from pathlib import Path

import pytest

from jj_review.config import load_config
from jj_review.errors import CliError
from tests.support.integration_helpers import init_repo, run_command


def test_load_config_returns_defaults_when_jj_config_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    repo = init_repo(tmp_path)

    config = load_config(repo_root=repo)

    assert config.logging.level == "WARNING"
    assert config.repo.github_host == "github.com"
    assert config.repo.remote is None


def test_load_config_merges_user_repo_and_workspace_jj_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    repo = init_repo(tmp_path)
    change_id = "zvlywqkxtmnpqrstu"

    _write_config(
        _jj_config_path("user"),
        [
            "[jj-review.repo]",
            'remote = "origin"',
            "",
            "[jj-review.logging]",
            'level = "info"',
            "",
            f'[jj-review.change."{change_id}"]',
            'bookmark_override = "review/from-user"',
        ],
    )
    _write_config(
        _jj_config_path("repo", repo),
        [
            "[jj-review.repo]",
            'trunk_branch = "main"',
            'reviewers = ["octocat"]',
        ],
    )
    _write_config(
        _jj_config_path("workspace", repo),
        [
            "[jj-review.repo]",
            'remote = "upstream"',
        ],
    )

    config = load_config(repo_root=repo)

    assert config.repo.remote == "upstream"
    assert config.repo.trunk_branch == "main"
    assert config.repo.reviewers == ["octocat"]
    assert config.logging.level == "INFO"
    assert config.change[change_id].bookmark_override == "review/from-user"


def test_load_config_reads_explicit_jj_review_config_file(tmp_path: Path) -> None:
    config_path = tmp_path / "explicit.toml"
    config_path.write_text(
        "\n".join(
            [
                "[jj-review.repo]",
                'remote = "origin"',
                'trunk_branch = "main"',
                "",
                "[jj-review.logging]",
                'level = "DEBUG"',
            ]
        ),
        encoding="utf-8",
    )

    config = load_config(repo_root=None, config_path=config_path)

    assert config.repo.remote == "origin"
    assert config.repo.trunk_branch == "main"
    assert config.logging.level == "DEBUG"


def test_load_config_rejects_missing_explicit_config_path(tmp_path: Path) -> None:
    config_path = tmp_path / "missing.toml"

    with pytest.raises(CliError, match="Config file does not exist"):
        load_config(repo_root=None, config_path=config_path)


def test_load_config_ignores_unrelated_jj_config_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    repo = init_repo(tmp_path)
    _write_config(
        _jj_config_path("user"),
        [
            "[git]",
            'push-new-bookmarks = true',
        ],
    )

    config = load_config(repo_root=repo)

    assert config.logging.level == "WARNING"
    assert config.repo.github_host == "github.com"
    assert config.repo.remote is None


def test_load_config_rejects_invalid_keys_inside_jj_review_section(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid.toml"
    config_path.write_text(
        "\n".join(
            [
                "[jj-review]",
                'remote = "origin"',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(CliError, match="Extra inputs are not permitted"):
        load_config(repo_root=None, config_path=config_path)


def test_load_config_rejects_invalid_logging_level_in_jj_review_section(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "invalid-logging.toml"
    config_path.write_text(
        "\n".join(
            [
                "[jj-review.logging]",
                'level = "DEBIG"',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(CliError, match="Invalid logging level"):
        load_config(repo_root=None, config_path=config_path)


def _jj_config_path(scope: str, repo_root: Path | None = None) -> Path:
    command = ["jj", "config", "path", f"--{scope}"]
    if repo_root is not None:
        command.extend(["-R", str(repo_root)])
    completed = run_command(command, repo_root or Path.cwd())
    return Path(completed.stdout.strip())


def _write_config(config_path: Path, lines: list[str]) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
