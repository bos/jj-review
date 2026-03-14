"""Configuration loading for `jj-review`."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONFIG_FILENAME = ".jj-review.toml"
_CACHE_TOP_LEVEL_KEYS = {"change", "version"}


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


class LoggingConfig(BaseModel):
    """User-configurable logging defaults."""

    model_config = ConfigDict(extra="forbid")

    http_debug: bool = False
    level: str = "INFO"


class AppConfig(BaseModel):
    """Top-level configuration model."""

    model_config = ConfigDict(extra="forbid")

    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    repo: RepoConfig = Field(default_factory=RepoConfig)


def load_config(*, repo_root: Path | None, config_path: Path | None = None) -> AppConfig:
    """Load `.jj-review.toml` if present, otherwise return defaults."""

    explicit_config_path = config_path is not None
    resolved_path = config_path or _default_config_path(repo_root)
    if resolved_path is None:
        return AppConfig()
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
        config_data = {
            key: value
            for key, value in raw_config.items()
            if key not in _CACHE_TOP_LEVEL_KEYS
        }
        return AppConfig.model_validate(config_data)
    except ValidationError as error:
        raise ConfigError(f"Invalid jj-review config in {resolved_path}: {error}") from error


def _default_config_path(repo_root: Path | None) -> Path | None:
    if repo_root is None:
        return None
    return repo_root / CONFIG_FILENAME
