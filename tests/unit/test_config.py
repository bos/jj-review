from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jj_review.config import load_config, parse_jj_review_config_toml
from jj_review.errors import CliError
from jj_review.jj import JjCliArgs, JjClient


def _make_client(tmp_path: Path, stdout: str) -> JjClient:
    def runner(command, cwd):
        assert command[0] == "jj"
        assert tuple(command[-3:]) == ("config", "list", "jj-review")
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    return JjClient(tmp_path, runner=runner)


def test_load_config_returns_defaults_when_no_keys_set(tmp_path: Path) -> None:
    config = load_config(jj_client=_make_client(tmp_path, ""))

    assert config.logging.level == "WARNING"
    assert config.bookmark_prefix == "review"
    assert config.cleanup_user_bookmarks is False
    assert config.labels == []


def test_load_config_parses_resolved_jj_review_section(tmp_path: Path) -> None:
    stdout = "\n".join(
        [
            'jj-review.bookmark_prefix = "bosullivan"',
            "jj-review.cleanup_user_bookmarks = true",
            'jj-review.reviewers = ["octocat"]',
            'jj-review.team_reviewers = ["platform"]',
            'jj-review.use_bookmarks = ["potato/*", "", "spam/eggs", "potato/*"]',
            'jj-review.labels = ["needs-review"]',
            'jj-review.logging.level = "info"',
            "",
        ]
    )
    config = load_config(jj_client=_make_client(tmp_path, stdout))

    assert config.logging.level == "INFO"
    assert config.bookmark_prefix == "bosullivan"
    assert config.cleanup_user_bookmarks is True
    assert config.reviewers == ["octocat"]
    assert config.team_reviewers == ["platform"]
    assert config.labels == ["needs-review"]
    assert config.use_bookmarks == ["potato/*", "spam/eggs"]


def test_load_config_ignores_unknown_keys_inside_jj_review_section(tmp_path: Path) -> None:
    stdout = 'jj-review.potato = "round"\n'

    config = load_config(jj_client=_make_client(tmp_path, stdout))

    assert config.bookmark_prefix == "review"
    assert config.labels == []


def test_load_config_rejects_likely_top_level_typo(tmp_path: Path) -> None:
    stdout = 'jj-review.bookmark_prefx = "bos"\n'

    with pytest.raises(CliError, match=r"Did you mean \[jj-review\]\.bookmark_prefix\?"):
        load_config(jj_client=_make_client(tmp_path, stdout))


def test_load_config_rejects_invalid_logging_level(tmp_path: Path) -> None:
    stdout = 'jj-review.logging.level = "DEBIG"\n'

    with pytest.raises(CliError, match="Invalid logging level"):
        load_config(jj_client=_make_client(tmp_path, stdout))


def test_load_config_rejects_bookmark_prefix_with_slash(tmp_path: Path) -> None:
    stdout = 'jj-review.bookmark_prefix = "bosullivan/review"\n'

    with pytest.raises(CliError, match="bookmark_prefix"):
        load_config(jj_client=_make_client(tmp_path, stdout))


def test_parse_jj_review_config_toml_extracts_nested_tables() -> None:
    stdout = "\n".join(
        [
            'jj-review.bookmark_prefix = "bos"',
            'jj-review.logging.level = "INFO"',
            "",
        ]
    )
    parsed = parse_jj_review_config_toml(stdout)
    assert parsed == {"bookmark_prefix": "bos", "logging": {"level": "INFO"}}


def test_load_config_wraps_jj_command_failure_with_user_facing_message(
    tmp_path: Path,
) -> None:
    def failing_runner(command, cwd):
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Config error: Invalid config-file path 'missing.toml'\n",
        )

    client = JjClient(tmp_path, runner=failing_runner)

    with pytest.raises(CliError) as exc_info:
        load_config(jj_client=client)

    message = str(exc_info.value)
    assert message.startswith("Could not load jj-review config:")
    assert "Invalid config-file path" in message


def test_load_config_surfaces_cli_args_through_to_jj(tmp_path: Path) -> None:
    observed_commands: list[tuple[str, ...]] = []

    def runner(command, cwd):
        observed_commands.append(tuple(command))
        return subprocess.CompletedProcess(
            command,
            0,
            stdout='jj-review.bookmark_prefix = "bos"\n',
            stderr="",
        )

    client = JjClient(
        tmp_path,
        cli_args=JjCliArgs(argv=("--config", "jj-review.bookmark_prefix=bos")),
        runner=runner,
    )
    config = load_config(jj_client=client)

    assert config.bookmark_prefix == "bos"
    assert observed_commands == [
        (
            "jj",
            "--config",
            "jj-review.bookmark_prefix=bos",
            "config",
            "list",
            "jj-review",
        )
    ]
