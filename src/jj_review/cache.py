"""Persistence helpers for sparse local review state."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from jj_review.errors import CliError
from jj_review.models.cache import ReviewState

_CACHE_KEYS = {"change", "version"}
_SIMPLE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class ReviewStateError(CliError):
    """Raised when the local review state file is unreadable or invalid."""


class ReviewStateStore:
    """Load and save sparse review state inside `.jj-review.toml`."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> ReviewState:
        """Load the persisted state, or defaults when the file is missing."""

        raw_data = self._load_raw_data()
        cache_data = {key: raw_data[key] for key in _CACHE_KEYS if key in raw_data}
        if not cache_data:
            return ReviewState()
        try:
            return ReviewState.model_validate(cache_data)
        except ValidationError as error:
            raise ReviewStateError(
                f"Invalid jj-review state in {self._path}: {error}"
            ) from error

    def save(self, state: ReviewState) -> None:
        """Persist the supplied state while preserving non-cache config sections."""

        raw_data = self._load_raw_data()
        for key in _CACHE_KEYS:
            raw_data.pop(key, None)

        serialized_state = state.model_dump(by_alias=True, exclude_none=True)
        raw_data.update(serialized_state)
        rendered = _render_toml(raw_data)
        try:
            self._path.write_text(rendered, encoding="utf-8")
        except OSError as error:
            raise ReviewStateError(
                f"Could not write jj-review state file {self._path}: {error}"
            ) from error

    def _load_raw_data(self) -> dict[str, Any]:
        if not self._path.exists():
            return {}
        if not self._path.is_file():
            raise ReviewStateError(f"jj-review state path is not a file: {self._path}")
        try:
            with self._path.open("rb") as file:
                data = tomllib.load(file)
        except tomllib.TOMLDecodeError as error:
            raise ReviewStateError(
                f"Invalid jj-review state in {self._path}: {error}"
            ) from error
        except OSError as error:
            raise ReviewStateError(
                f"Could not read jj-review state file {self._path}: {error}"
            ) from error

        if not isinstance(data, dict):
            raise ReviewStateError(f"Invalid jj-review state in {self._path}: expected a table.")
        return dict(data)


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
