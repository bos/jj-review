"""Configuration loading for `jj-review`."""

from __future__ import annotations

import difflib
import logging
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jj_review import ui
from jj_review.errors import CliError

CONFIG_SECTION = "jj-review"
DEFAULT_BOOKMARK_PREFIX = "review"
_TYPO_CUTOFF = 0.75


class RepoConfig(BaseModel):
    """Repository defaults resolved before command planning."""

    model_config = ConfigDict(extra="ignore")

    bookmark_prefix: str = DEFAULT_BOOKMARK_PREFIX
    cleanup_user_bookmarks: bool = False
    labels: list[str] = Field(default_factory=list)
    reviewers: list[str] = Field(default_factory=list)
    team_reviewers: list[str] = Field(default_factory=list)
    use_bookmarks: list[str] = Field(default_factory=list)

    @field_validator("bookmark_prefix")
    @classmethod
    def _validate_bookmark_prefix(cls, value: str) -> str:
        prefix = value.strip()
        if not prefix:
            raise ValueError("bookmark_prefix must not be empty")
        if "/" in prefix:
            raise ValueError("bookmark_prefix must not contain '/'")
        return prefix

    @field_validator("use_bookmarks")
    @classmethod
    def _validate_use_bookmarks(cls, values: list[str]) -> list[str]:
        patterns: list[str] = []
        seen: set[str] = set()
        for value in values:
            pattern = value.strip()
            if not pattern:
                continue
            if pattern in seen:
                continue
            seen.add(pattern)
            patterns.append(pattern)
        return patterns


class LoggingConfig(BaseModel):
    """User-configurable logging defaults."""

    model_config = ConfigDict(extra="ignore")

    http_debug: bool = False
    level: str = "WARNING"

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        level_name = value.upper()
        level_names = logging.getLevelNamesMapping()
        if level_name not in level_names:
            valid_levels = ", ".join(sorted(level_names))
            raise ValueError(f"Invalid logging level {value}. Expected one of: {valid_levels}")
        return level_name


class AppConfig(RepoConfig):
    """Top-level configuration model."""

    model_config = ConfigDict(extra="ignore")

    logging: LoggingConfig = Field(default_factory=LoggingConfig)


def load_config(*, repo_root: Path | None, config_path: Path | None = None) -> AppConfig:
    """Load `jj-review` config from jj config scopes or an explicit config file."""

    if config_path is not None:
        return _load_explicit_config(config_path)

    merged_config: dict[str, object] = {}
    for path in _default_config_paths(repo_root):
        layer = _load_config_layer(path)
        merged_config = _merge_config_data(merged_config, layer)
    return _validate_config(merged_config, source="jj config")


def _load_explicit_config(config_path: Path) -> AppConfig:
    if not config_path.exists():
        raise CliError(f"Config file does not exist: {config_path}")
    if not config_path.is_file():
        raise CliError(f"Config path is not a file: {config_path}")
    return _validate_config(_load_config_layer(config_path), source=str(config_path))


def _default_config_paths(repo_root: Path | None) -> tuple[Path, ...]:
    paths = [_jj_config_path(scope="user")]
    if repo_root is not None:
        paths.append(_jj_config_path(scope="repo", repo_root=repo_root))
        paths.append(_jj_config_path(scope="workspace", repo_root=repo_root))
    return tuple(paths)


def _jj_config_path(*, scope: str, repo_root: Path | None = None) -> Path:
    command = ["jj", "config", "path", f"--{scope}"]
    if scope in {"repo", "workspace"}:
        if repo_root is None:
            raise ValueError(f"`repo_root` is required for the {scope} config scope.")
        command.extend(["-R", str(repo_root)])
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as error:
        raise CliError(t"{ui.cmd('jj')} is not installed or is not on PATH.") from error
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise CliError(f"Could not determine the jj {scope} config path: {message}")
    path_text = completed.stdout.strip()
    if not path_text:
        raise CliError(t"{ui.cmd(f'jj config path --{scope}')} returned an empty path.")
    return Path(path_text)


def _load_config_layer(config_path: Path) -> dict[str, object]:
    if not config_path.exists():
        return {}
    if not config_path.is_file():
        raise CliError(f"Config path is not a file: {config_path}")
    try:
        with config_path.open("rb") as file:
            raw_config = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise CliError(f"Invalid jj-review config in {config_path}: {error}") from error
    except OSError as error:
        raise CliError(f"Could not read config file {config_path}: {error}") from error

    config_section = raw_config.get(CONFIG_SECTION)
    if config_section is None:
        return {}
    if not isinstance(config_section, Mapping):
        raise CliError(
            f"Invalid jj-review config in {config_path}: [{CONFIG_SECTION}] must be a table."
        )

    config_data = dict(config_section)
    _raise_on_likely_config_typos(config_data=config_data, source=str(config_path))
    return _validate_config(config_data, source=str(config_path)).model_dump(exclude_unset=True)


def _merge_config_data(
    base: dict[str, object],
    override: dict[str, object],
) -> dict[str, object]:
    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_config_data(dict(existing), dict(value))
            continue
        merged[key] = value
    return merged


def _raise_on_likely_config_typos(*, config_data: Mapping[str, object], source: str) -> None:
    _raise_on_likely_unknown_keys(
        table_path=f"[{CONFIG_SECTION}]",
        config_data=config_data,
        allowed_keys=(*RepoConfig.model_fields, "logging"),
        source=source,
    )

    logging_config = config_data.get("logging")
    if isinstance(logging_config, Mapping):
        _raise_on_likely_unknown_keys(
            table_path=f"[{CONFIG_SECTION}.logging]",
            config_data=logging_config,
            allowed_keys=tuple(LoggingConfig.model_fields),
            source=source,
        )


def _raise_on_likely_unknown_keys(
    *,
    table_path: str,
    config_data: Mapping[str, object],
    allowed_keys: tuple[str, ...],
    source: str,
) -> None:
    allowed_key_set = set(allowed_keys)
    for key in config_data:
        if key in allowed_key_set:
            continue
        suggestion = difflib.get_close_matches(key, allowed_keys, n=1, cutoff=_TYPO_CUTOFF)
        if not suggestion:
            continue
        raise CliError(
            f"Invalid jj-review config in {source}: unknown key {table_path}.{key}. "
            f"Did you mean {table_path}.{suggestion[0]}?"
        )


def _validate_config(config_data: dict[str, object], *, source: str) -> AppConfig:
    try:
        return AppConfig.model_validate(config_data)
    except ValidationError as error:
        raise CliError(_format_validation_error(source=source, error=error)) from error


def _format_validation_error(*, source: str, error: ValidationError) -> str:
    details = [
        _format_validation_issue(tuple(str(part) for part in issue["loc"]), str(issue["msg"]))
        for issue in error.errors(include_url=False)
    ]
    return f"Invalid jj-review config in {source}: {'; '.join(details)}"


def _format_validation_issue(location: tuple[str, ...], message: str) -> str:
    if len(location) == 1:
        return f"[{CONFIG_SECTION}].{location[0]}: {message}"
    if location[:1] == ("logging",) and len(location) == 2:
        return f"[{CONFIG_SECTION}.logging].{location[1]}: {message}"
    if not location:
        return message
    return f"[{CONFIG_SECTION}].{'.'.join(location)}: {message}"
