"""Persistence helpers for sparse local review state."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jj_review.errors import CliError
from jj_review.models.cache import ReviewState

logger = logging.getLogger(__name__)

STATE_DIRNAME = "jj-review"
STATE_FILENAME = "state.toml"
_SIMPLE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_REPO_ID_RE = re.compile(r"^[0-9a-f]+$")


class ReviewStateError(CliError):
    """Raised when the local review state file is unreadable or invalid."""


class ReviewStateUnavailable(RuntimeError):
    """Raised when optional repo-scoped review state cannot be used."""


class ReviewStateStore:
    """Load and save sparse review state in a user state directory."""

    def __init__(self, path: Path | None, *, disabled_reason: str | None = None) -> None:
        self._path = path
        self._disabled_reason = disabled_reason

    @classmethod
    def for_repo(cls, repo_root: Path) -> ReviewStateStore:
        """Build a review state store for the supplied repository root."""

        try:
            return cls(resolve_state_path(repo_root))
        except ReviewStateUnavailable as error:
            logger.debug("Review state disabled for %s: %s", repo_root, error)
            return cls(path=None, disabled_reason=str(error))

    def load(self) -> ReviewState:
        """Load the persisted state, or defaults when the file is missing."""

        if self._path is None:
            return ReviewState()
        raw_data = self._load_raw_data()
        if not raw_data:
            return ReviewState()
        try:
            return ReviewState.model_validate(raw_data)
        except ValidationError as error:
            raise ReviewStateError(
                f"Invalid jj-review state in {self._path}: {error}"
            ) from error

    def save(self, state: ReviewState) -> None:
        """Persist the supplied state."""

        if self._path is None:
            logger.debug("Skipping review state save: %s", self._disabled_reason)
            return
        serialized_state = state.model_dump(by_alias=True, exclude_none=True)
        rendered = _render_toml(serialized_state)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(rendered, encoding="utf-8")
        except OSError as error:
            raise ReviewStateError(
                f"Could not write jj-review state file {self._path}: {error}"
            ) from error

    def _load_raw_data(self) -> dict[str, Any]:
        path = self._path
        if path is None:
            return {}
        if not path.exists():
            return {}
        if not path.is_file():
            raise ReviewStateError(f"jj-review state path is not a file: {path}")
        try:
            with path.open("rb") as file:
                data = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise ReviewStateError(
                f"Invalid jj-review state in {path}: {error}"
            ) from error
        except OSError as error:
            raise ReviewStateError(
                f"Could not read jj-review state file {path}: {error}"
            ) from error

        if not isinstance(data, dict):
            raise ReviewStateError(f"Invalid jj-review state in {path}: expected a table.")
        return dict(data)


def resolve_state_path(repo_root: Path) -> Path:
    """Return the machine-written review state path for the repo."""

    repo_id = _resolve_repo_id(repo_root)
    return default_state_root() / STATE_DIRNAME / "repos" / repo_id / STATE_FILENAME


def default_state_root() -> Path:
    """Return the base directory used for machine-written state."""

    configured = os.environ.get("XDG_STATE_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path("~", ".local", "state").expanduser().resolve()


def _resolve_repo_id(repo_root: Path) -> str:
    config_id_path = repo_root / ".jj" / "repo" / "config-id"
    repo_id = _read_repo_id(config_id_path)
    if repo_id is not None:
        return repo_id

    _materialize_repo_config_id(repo_root)
    repo_id = _read_repo_id(config_id_path)
    if repo_id is not None:
        return repo_id
    raise ReviewStateUnavailable(
        f"Could not determine jj repo config ID for {repo_root}: "
        "`jj config path --repo` did not create `.jj/repo/config-id`."
    )


def _read_repo_id(config_id_path: Path) -> str | None:
    if not config_id_path.exists():
        return None
    if not config_id_path.is_file():
        raise ReviewStateError(f"jj repo config ID path is not a file: {config_id_path}")
    try:
        repo_id = config_id_path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ReviewStateError(
            f"Could not read jj repo config ID {config_id_path}: {error}"
        ) from error
    if not repo_id:
        raise ReviewStateError(f"jj repo config ID file is empty: {config_id_path}")
    if not _REPO_ID_RE.fullmatch(repo_id):
        raise ReviewStateError(f"Invalid jj repo config ID in {config_id_path}: {repo_id!r}")
    return repo_id


def _materialize_repo_config_id(repo_root: Path) -> None:
    try:
        completed = subprocess.run(
            ["jj", "config", "path", "--repo"],
            capture_output=True,
            check=False,
            cwd=repo_root,
            text=True,
        )
    except FileNotFoundError as error:
        raise ReviewStateError("`jj` is not installed or is not on PATH.") from error
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise ReviewStateUnavailable(
            f"Could not determine jj repo config path for {repo_root}: {message}"
        )


def _render_toml(data: dict[str, Any]) -> str:
    lines: list[str] = []
    _append_table(lines, (), data)
    return "\n".join(lines).rstrip() + "\n"


def _append_table(lines: list[str], path: tuple[str, ...], table: dict[str, Any]) -> None:
    scalar_items = [
        (key, value)
        for key, value in table.items()
        if value is not None and not isinstance(value, dict)
    ]
    nested_items = [
        (key, value)
        for key, value in table.items()
        if value is not None and isinstance(value, dict)
    ]

    if path:
        lines.append(f"[{'.'.join(_quote_key(part) for part in path)}]")
    for key, value in scalar_items:
        lines.append(f"{_quote_key(key)} = {_render_value(value)}")
    if scalar_items and nested_items:
        lines.append("")

    for index, (key, value) in enumerate(nested_items):
        if path or index > 0 or scalar_items:
            if lines and lines[-1] != "":
                lines.append("")
        _append_table(lines, (*path, key), value)


def _quote_key(key: str) -> str:
    if _SIMPLE_KEY_RE.fullmatch(key):
        return key
    return json.dumps(key)


def _render_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    raise TypeError(f"Unsupported TOML value type: {type(value)!r}")
