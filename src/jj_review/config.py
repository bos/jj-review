"""Configuration loading for `jj-review`."""

from __future__ import annotations

import logging
import subprocess
import tomllib
from collections.abc import Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jj_review.errors import CliError

CONFIG_SECTION = "jj-review"


class RepoConfig(BaseModel):
    """Repository defaults resolved before command planning."""

    model_config = ConfigDict(extra="forbid")

    github_host: str = "github.com"
    github_owner: str | None = None
    github_repo: str | None = None
    labels: list[str] = Field(default_factory=list)
    remote: str | None = None
    reviewers: list[str] = Field(default_factory=list)
    team_reviewers: list[str] = Field(default_factory=list)
    trunk_branch: str | None = None


class ChangeConfig(BaseModel):
    """User-authored per-change configuration."""

    model_config = ConfigDict(extra="forbid")

    bookmark_override: str | None = None


class LoggingConfig(BaseModel):
    """User-configurable logging defaults."""

    model_config = ConfigDict(extra="forbid")

    http_debug: bool = False
    level: str = "WARNING"

    @field_validator("level")
    @classmethod
    def _validate_level(cls, value: str) -> str:
        level_name = value.upper()
        level_names = logging.getLevelNamesMapping()
        if level_name not in level_names:
            valid_levels = ", ".join(sorted(level_names))
            raise ValueError(
                f"Invalid logging level {value!r}. Expected one of: {valid_levels}"
            )
        return level_name


class AppConfig(BaseModel):
    """Top-level configuration model."""

    model_config = ConfigDict(extra="forbid")

    change: dict[str, ChangeConfig] = Field(default_factory=dict)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    repo: RepoConfig = Field(default_factory=RepoConfig)


class _ConfigDocument(BaseModel):
    """Wrapper for `jj-review` keys inside a jj config file."""

    model_config = ConfigDict(extra="ignore")

    jj_review: AppConfig = Field(default_factory=AppConfig, alias=CONFIG_SECTION)


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
        raise CliError("`jj` is not installed or is not on PATH.") from error
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise CliError(f"Could not determine the jj {scope} config path: {message}")
    path_text = completed.stdout.strip()
    if not path_text:
        raise CliError(f"`jj config path --{scope}` returned an empty path.")
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

    try:
        document = _ConfigDocument.model_validate(raw_config)
    except ValidationError as error:
        raise CliError(f"Invalid jj-review config in {config_path}: {error}") from error
    return document.jj_review.model_dump(exclude_unset=True)


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


def _validate_config(config_data: dict[str, object], *, source: str) -> AppConfig:
    try:
        return AppConfig.model_validate(config_data)
    except ValidationError as error:
        raise CliError(f"Invalid jj-review config in {source}: {error}") from error
