"""Runtime bootstrap helpers for CLI commands."""

from __future__ import annotations

import logging
import subprocess
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path

from jj_review.config import AppConfig, ConfigError, load_config


class BootstrapError(RuntimeError):
    """Raised when CLI bootstrap fails with a user-facing error."""

    exit_code = 1


@dataclass(slots=True, frozen=True)
class RuntimeOptions:
    """Command-line options that influence bootstrap behavior."""

    config_path: Path | None
    debug: bool
    repository: Path | None


@dataclass(slots=True, frozen=True)
class AppContext:
    """Typed runtime state shared by command handlers."""

    config: AppConfig
    options: RuntimeOptions
    repo_root: Path


def bootstrap_context(args: Namespace) -> AppContext:
    """Resolve the repository, load config, and initialize logging."""

    repository = _resolve_optional_path(getattr(args, "repository", None))
    _validate_repository_path(repository)
    config_path = _resolve_optional_path(getattr(args, "config", None))
    try:
        repo_root = resolve_repo_root(repository or Path.cwd())
        config = load_config(repo_root=repo_root, config_path=config_path)
    except ConfigError as error:
        raise BootstrapError(str(error)) from error
    debug = bool(getattr(args, "debug", False))
    configure_logging(debug=debug, configured_level=config.logging.level)

    return AppContext(
        config=config,
        options=RuntimeOptions(
            config_path=config_path,
            debug=debug,
            repository=repository,
        ),
        repo_root=repo_root,
    )


def configure_logging(*, debug: bool, configured_level: str) -> None:
    """Apply process-wide logging defaults for the current command."""

    level_name = "DEBUG" if debug else configured_level.upper()
    level = _resolve_logging_level(level_name, original_value=configured_level)
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
        level=level,
    )


def _resolve_logging_level(level_name: str, *, original_value: str) -> int:
    level_names = logging.getLevelNamesMapping()
    if level_name not in level_names:
        valid_levels = ", ".join(sorted(level_names))
        raise BootstrapError(
            f"Invalid logging level {original_value!r}. Expected one of: {valid_levels}"
        )
    return level_names[level_name]


def resolve_repo_root(start_dir: Path) -> Path:
    """Resolve the jj workspace root from `start_dir`.

    Raises `BootstrapError` if the directory is not inside a jj workspace,
    so callers get a clear diagnostic rather than confusing downstream
    errors from later `jj` commands.
    """

    try:
        completed = subprocess.run(
            ["jj", "root"],
            capture_output=True,
            check=False,
            cwd=start_dir,
            text=True,
        )
    except FileNotFoundError as error:
        raise BootstrapError("`jj` is not installed or is not on PATH.") from error
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise BootstrapError(f"Not inside a jj workspace (from {start_dir}): {message}")

    root = completed.stdout.strip()
    if not root:
        raise BootstrapError(f"`jj root` returned an empty path (from {start_dir}).")
    return Path(root)


def _resolve_optional_path(raw_path: object) -> Path | None:
    if raw_path is None:
        return None
    if isinstance(raw_path, Path):
        return raw_path.resolve()
    return Path(str(raw_path)).resolve()


def _validate_repository_path(repository: Path | None) -> None:
    if repository is None:
        return
    if not repository.exists():
        raise BootstrapError(f"Repository path does not exist: {repository}")
    if not repository.is_dir():
        raise BootstrapError(f"Repository path is not a directory: {repository}")
