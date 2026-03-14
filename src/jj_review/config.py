"""Configuration loading for `jj-review`."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_DIRNAME = "jj-review"
CONFIG_FILENAME = "config.toml"


class ConfigError(RuntimeError):
    """Raised when the local configuration file is invalid."""


class RepoConfig(BaseModel):
    """Repository defaults resolved before command planning."""

    model_config = ConfigDict(extra="forbid")

    github_host: str = "github.com"
    github_owner: str | None = None
    github_repo: str | None = None
    remote: str | None = None
    trunk_branch: str | None = None


class RepoConfigOverride(BaseModel):
    """Path-scoped repository defaults that override the base repo config."""

    model_config = ConfigDict(extra="forbid")

    github_host: str | None = None
    github_owner: str | None = None
    github_repo: str | None = None
    remote: str | None = None
    trunk_branch: str | None = None


class ChangeConfig(BaseModel):
    """User-authored per-change configuration."""

    model_config = ConfigDict(extra="forbid")

    bookmark_override: str | None = None
    draft: bool | None = None
    skip: bool | None = None


class LoggingConfig(BaseModel):
    """User-configurable logging defaults."""

    model_config = ConfigDict(extra="forbid")

    http_debug: bool = False
    level: str = "INFO"


class AppConfig(BaseModel):
    """Top-level configuration model."""

    model_config = ConfigDict(extra="forbid")

    change: dict[str, ChangeConfig] = Field(default_factory=dict)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    repo: RepoConfig = Field(default_factory=RepoConfig)
    repositories: dict[str, RepoConfigOverride] = Field(default_factory=dict)


def load_config(*, repo_root: Path | None, config_path: Path | None = None) -> AppConfig:
    """Load the user config file if present, otherwise return defaults."""

    explicit_config_path = config_path is not None
    resolved_path = config_path or default_config_path()
    if not resolved_path.exists():
        if explicit_config_path:
            raise ConfigError(f"Config file does not exist: {resolved_path}")
        return AppConfig()
    if not resolved_path.is_file():
        raise ConfigError(f"Config path is not a file: {resolved_path}")

    try:
        with resolved_path.open("rb") as file:
            raw_config = tomllib.load(file)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"Invalid jj-review config in {resolved_path}: {error}") from error
    except OSError as error:
        raise ConfigError(f"Could not read config file {resolved_path}: {error}") from error

    try:
        config = AppConfig.model_validate(raw_config)
    except ValidationError as error:
        raise ConfigError(f"Invalid jj-review config in {resolved_path}: {error}") from error
    return _apply_repo_overrides(config, repo_root)


def default_config_path() -> Path:
    """Return the default user config path."""

    return _xdg_path(
        env_var="XDG_CONFIG_HOME",
        fallback=("~", ".config"),
    ) / CONFIG_DIRNAME / CONFIG_FILENAME


def _apply_repo_overrides(config: AppConfig, repo_root: Path | None) -> AppConfig:
    if repo_root is None or not config.repositories:
        return config

    resolved_repo_root = repo_root.resolve()
    repo_data = config.repo.model_dump()
    matches = sorted(
        (
            (path_str, override)
            for path_str, override in config.repositories.items()
            if _matches_repo_path(resolved_repo_root, path_str)
        ),
        key=lambda item: len(str(_resolve_configured_path(item[0]))),
    )
    for _, override in matches:
        for key, value in override.model_dump(exclude_none=True).items():
            repo_data[key] = value
    return config.model_copy(update={"repo": RepoConfig.model_validate(repo_data)})


def _matches_repo_path(repo_root: Path, configured_path: str) -> bool:
    candidate = _resolve_configured_path(configured_path)
    return repo_root == candidate or repo_root.is_relative_to(candidate)


def _resolve_configured_path(configured_path: str) -> Path:
    return Path(configured_path).expanduser().resolve()


def _xdg_path(*, env_var: str, fallback: tuple[str, ...]) -> Path:
    configured = os.environ.get(env_var)
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(*fallback).expanduser().resolve()
