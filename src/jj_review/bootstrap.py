"""Runtime bootstrap helpers for CLI commands."""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jj_review.config import AppConfig, load_config
from jj_review.errors import CliError

_MINIMUM_JJ_VERSION = (0, 21, 0)
_MINIMUM_JJ_VERSION_STRING = "0.21.0"


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


def bootstrap_context(
    *,
    repository: Path | None,
    config_path: Path | None,
    debug: bool,
) -> AppContext:
    """Resolve the repository, load config, and initialize logging."""

    repository = _resolve_optional_path(repository)
    _validate_repository_path(repository)
    config_path = _resolve_optional_path(config_path)
    check_jj_version()
    repo_root = resolve_repo_root(repository or Path.cwd())
    config = load_config(repo_root=repo_root, config_path=config_path)
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

    root_level = _resolve_logging_level(
        configured_level.upper(),
        original_value=configured_level,
    )
    logging.basicConfig(
        format="%(levelname)s %(name)s: %(message)s",
        force=True,
        level=root_level,
    )
    app_level = logging.DEBUG if debug else root_level
    logging.getLogger("jj_review").setLevel(app_level)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def _resolve_logging_level(level_name: str, *, original_value: str) -> int:
    level_names = logging.getLevelNamesMapping()
    if level_name not in level_names:
        valid_levels = ", ".join(sorted(level_names))
        raise CliError(
            f"Invalid logging level {original_value!r}. Expected one of: {valid_levels}"
        )
    return level_names[level_name]


def resolve_repo_root(start_dir: Path) -> Path:
    """Resolve the jj workspace root from `start_dir`.

    Raises `CliError` if the directory is not inside a jj workspace,
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
        raise CliError("`jj` is not installed or is not on PATH.") from error
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise CliError(f"Not inside a jj workspace (from {start_dir}): {message}")

    root = completed.stdout.strip()
    if not root:
        raise CliError(f"`jj root` returned an empty path (from {start_dir}).")
    return Path(root)


def check_jj_version() -> None:
    """Verify that the installed `jj` meets the minimum required version.

    Raises `CliError` if `jj` is absent, if its version string cannot be parsed,
    or if the installed version is older than the minimum.
    """

    try:
        completed = subprocess.run(
            ["jj", "--version"],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError as error:
        raise CliError("`jj` is not installed or is not on PATH.") from error

    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise CliError(f"`jj --version` failed: {message}")

    version = _parse_jj_version(completed.stdout.strip())
    if version is None:
        raise CliError(
            f"Could not parse `jj --version` output: {completed.stdout.strip()!r}. "
            f"jj-review requires jj {_MINIMUM_JJ_VERSION_STRING} or later."
        )
    if version < _MINIMUM_JJ_VERSION:
        installed = ".".join(str(x) for x in version)
        raise CliError(
            f"jj {installed} is too old. "
            f"jj-review requires jj {_MINIMUM_JJ_VERSION_STRING} or later. "
            "Please upgrade jj."
        )


def _parse_jj_version(version_output: str) -> tuple[int, ...] | None:
    """Parse version tuple from `jj --version` output.

    Expected formats: ``"jj 0.39.0"`` or ``"jj 0.39.0-<build-hash>"``.
    Returns ``None`` if the output does not match the expected format.
    """

    parts = version_output.split()
    if len(parts) < 2 or parts[0] != "jj":
        return None
    version_str = parts[1].split("-")[0]
    try:
        return tuple(int(x) for x in version_str.split("."))
    except ValueError:
        return None


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
        raise CliError(f"Repository path does not exist: {repository}")
    if not repository.is_dir():
        raise CliError(f"Repository path is not a directory: {repository}")
