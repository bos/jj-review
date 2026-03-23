"""Filesystem operations and logic for per-operation intent files."""
from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from jj_review.models.intent import (
    CleanupApplyIntent,
    CleanupRestackIntent,
    IntentFile,
    LandIntent,
    LoadedIntent,
    MatchResult,
    RelinkIntent,
    SubmitIntent,
)

logger = logging.getLogger(__name__)

_SIMPLE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _quote_key(key: str) -> str:
    if _SIMPLE_KEY_RE.fullmatch(key):
        return key
    return json.dumps(key)


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------

def _intent_filename(state_dir: Path, now: datetime) -> Path:
    base = now.strftime("%Y-%m-%d-%H-%M")
    for nn in range(1, 100):
        candidate = state_dir / f"incomplete-{base}.{nn:02d}.toml"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate intent file name (100 collisions).")


# ---------------------------------------------------------------------------
# TOML serialisation
# ---------------------------------------------------------------------------

def _render_intent_toml(data: dict) -> str:
    lines: list[str] = []
    scalar_items = [
        (k, v) for k, v in data.items()
        if not isinstance(v, dict) and v is not None
    ]
    sub_table_items = [
        (k, v) for k, v in data.items()
        if isinstance(v, dict)
    ]

    for key, value in scalar_items:
        lines.append(f"{_quote_key(key)} = {_render_intent_value(value)}")

    for key, sub in sub_table_items:
        if lines:
            lines.append("")
        lines.append(f"[{_quote_key(key)}]")
        for sub_key, sub_value in sub.items():
            lines.append(f"{_quote_key(sub_key)} = {_render_intent_value(sub_value)}")

    return "\n".join(lines).rstrip() + "\n"


def _render_intent_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list | tuple):
        items = ", ".join(json.dumps(item) for item in value)
        return f"[{items}]"
    raise TypeError(f"Unsupported intent TOML value type: {type(value)!r}")


def write_intent(state_dir: Path, intent: IntentFile) -> Path:
    """Write an intent file atomically. Returns the path of the created file."""
    state_dir.mkdir(parents=True, exist_ok=True)
    dest = _intent_filename(state_dir, datetime.now(UTC))
    _write_intent_file(dest, intent)
    logger.debug("Wrote intent file %s", dest.name)
    return dest


def replace_intent(path: Path, intent: IntentFile) -> None:
    """Rewrite an existing intent file atomically."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_intent_file(path, intent)
    logger.debug("Updated intent file %s", path.name)


def _write_intent_file(path: Path, intent: IntentFile) -> None:
    data = dataclasses.asdict(intent)
    rendered = _render_intent_toml(data)
    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, suffix=".toml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        Path(tmp_path_str).replace(path)
    except Exception:
        try:
            os.unlink(tmp_path_str)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Read / Scan
# ---------------------------------------------------------------------------

def _parse_intent(data: dict, path: Path) -> LoadedIntent | None:
    """Parse raw TOML dict into a LoadedIntent. Returns None on parse error."""
    kind = data.get("kind")
    try:
        if kind == "submit":
            intent = SubmitIntent(
                kind="submit",
                pid=int(data["pid"]),
                label=str(data["label"]),
                display_revset=str(data["display_revset"]),
                head_change_id=str(data["head_change_id"]),
                ordered_change_ids=tuple(str(x) for x in data.get("ordered_change_ids", [])),
                bookmarks=dict(data.get("bookmarks", {})),
                bases=dict(data.get("bases", {})),
                started_at=str(data["started_at"]),
            )
        elif kind == "cleanup-apply":
            intent = CleanupApplyIntent(
                kind="cleanup-apply",
                pid=int(data["pid"]),
                label=str(data["label"]),
                started_at=str(data["started_at"]),
            )
        elif kind == "cleanup-restack":
            intent = CleanupRestackIntent(
                kind="cleanup-restack",
                pid=int(data["pid"]),
                label=str(data.get("label", data["display_revset"])),
                display_revset=str(data["display_revset"]),
                ordered_change_ids=tuple(str(x) for x in data.get("ordered_change_ids", [])),
                started_at=str(data["started_at"]),
            )
        elif kind in {"relink", "adopt"}:
            intent = RelinkIntent(
                kind="relink",
                pid=int(data["pid"]),
                label=str(data["label"]),
                change_id=str(data["change_id"]),
                started_at=str(data["started_at"]),
            )
        elif kind == "land":
            expected_pr_number = data.get("expected_pr_number")
            intent = LandIntent(
                kind="land",
                pid=int(data["pid"]),
                label=str(data["label"]),
                display_revset=str(data["display_revset"]),
                ordered_change_ids=tuple(str(x) for x in data.get("ordered_change_ids", [])),
                ordered_commit_ids=tuple(str(x) for x in data.get("ordered_commit_ids", [])),
                landed_change_ids=tuple(str(x) for x in data.get("landed_change_ids", [])),
                landed_bookmarks={
                    str(key): str(value)
                    for key, value in dict(data.get("landed_bookmarks", {})).items()
                },
                landed_commit_ids={
                    str(key): str(value)
                    for key, value in dict(data.get("landed_commit_ids", {})).items()
                },
                landed_pull_request_numbers={
                    str(key): int(value)
                    for key, value in dict(
                        data.get("landed_pull_request_numbers", {})
                    ).items()
                },
                landed_subjects={
                    str(key): str(value)
                    for key, value in dict(data.get("landed_subjects", {})).items()
                },
                completed_change_ids=tuple(
                    str(x) for x in data.get("completed_change_ids", [])
                ),
                trunk_branch=str(data["trunk_branch"]),
                trunk_commit_id=str(data["trunk_commit_id"]),
                landed_commit_id=str(data["landed_commit_id"]),
                expected_pr_number=(
                    None if expected_pr_number is None else int(expected_pr_number)
                ),
                started_at=str(data["started_at"]),
            )
        else:
            logger.debug("Unknown intent kind %r in %s, skipping", kind, path)
            return None
        return LoadedIntent(path=path, intent=intent)
    except (KeyError, ValueError, TypeError) as error:
        logger.debug("Could not parse intent file %s: %s", path, error)
        return None


def scan_intents(state_dir: Path) -> list[LoadedIntent]:
    results = []
    for p in sorted(state_dir.glob("incomplete-*.toml")):
        try:
            with p.open("rb") as f:
                data = tomllib.load(f)
        except (tomllib.TOMLDecodeError, OSError) as error:
            logger.debug("Could not read intent file %s: %s", p, error)
            continue
        loaded = _parse_intent(data, p)
        if loaded is not None:
            results.append(loaded)
    return results


# ---------------------------------------------------------------------------
# Delete
# ---------------------------------------------------------------------------

def delete_intent(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# PID liveness
# ---------------------------------------------------------------------------

def pid_is_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    else:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by another user


# ---------------------------------------------------------------------------
# Concurrency check
# ---------------------------------------------------------------------------

def check_same_kind_intent(
    state_dir: Path,
    new_intent: IntentFile,
    *,
    print_fn: Callable[[str], None] = print,
) -> list[LoadedIntent]:
    """
    Scan for existing intents of the same kind.
    - If any PID is alive: poll every 0.5s until dead (max 5 minutes then notice).
    - Returns list of stale (dead-PID) same-kind intents.
    """
    existing = [
        loaded for loaded in scan_intents(state_dir)
        if loaded.intent.kind == new_intent.kind
    ]
    stale: list[LoadedIntent] = []
    for loaded in existing:
        if pid_is_alive(loaded.intent.pid):
            print_fn(
                f"Another {loaded.intent.label} is in progress "
                f"(PID {loaded.intent.pid}). Waiting..."
            )
            elapsed = 0.0
            warned = False
            while pid_is_alive(loaded.intent.pid):
                time.sleep(0.5)
                elapsed += 0.5
                if not warned and elapsed >= 300:
                    print_fn(
                        f"Still waiting for PID {loaded.intent.pid} after 5 minutes. "
                        "Press Ctrl-C to abort."
                    )
                    warned = True
        else:
            stale.append(loaded)
    return stale


# ---------------------------------------------------------------------------
# Match logic
# ---------------------------------------------------------------------------

def match_ordered_change_ids(
    existing: tuple[str, ...],
    new: tuple[str, ...],
) -> MatchResult:
    if existing == new:
        return "exact"
    if len(new) > len(existing) and new[:len(existing)] == existing:
        return "superset"
    if set(existing) & set(new):
        return "overlap"
    return "disjoint"


# ---------------------------------------------------------------------------
# Stale intent check
# ---------------------------------------------------------------------------

def intent_change_ids(intent: IntentFile) -> frozenset[str]:
    if isinstance(intent, SubmitIntent | CleanupRestackIntent | LandIntent):
        return frozenset(intent.ordered_change_ids)
    if isinstance(intent, RelinkIntent):
        return frozenset([intent.change_id])
    return frozenset()


# Keep private alias for backward compat within this module
_intent_change_ids = intent_change_ids


def intent_is_stale(
    intent: IntentFile,
    resolve_change_id: Callable[[str], bool],
    *,
    now: datetime | None = None,
) -> bool:
    """Return True if the intent is considered stale.

    For intents with change IDs (SubmitIntent, CleanupRestackIntent):
        stale if none of the change IDs resolve in the local repo.
    For CleanupApplyIntent and RelinkIntent (no useful change IDs):
        stale if the PID is dead AND the intent is older than 7 days.
    """
    if isinstance(intent, CleanupApplyIntent | RelinkIntent):
        if pid_is_alive(intent.pid):
            return False
        if now is None:
            now = datetime.now(UTC)
        try:
            started = datetime.fromisoformat(intent.started_at)
            if started.tzinfo is None:
                started = started.replace(tzinfo=UTC)
        except ValueError:
            return True  # can't parse → treat as stale
        return (now - started).days >= 7

    ids = intent_change_ids(intent)
    if not ids:
        return False
    return not any(resolve_change_id(cid) for cid in ids)


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------

def retire_superseded_intents(
    stale_intents: list[LoadedIntent],
    new_intent: IntentFile,
) -> None:
    """Auto-retire stale intents that are exact matches or strict ordered subsets."""
    if not isinstance(new_intent, SubmitIntent | CleanupRestackIntent | LandIntent):
        return
    new_ids = new_intent.ordered_change_ids
    for loaded in stale_intents:
        old = loaded.intent
        if not isinstance(old, type(new_intent)):
            continue
        result = match_ordered_change_ids(old.ordered_change_ids, new_ids)
        if result in ("exact", "superset"):
            delete_intent(loaded.path)
            logger.debug("Retired superseded intent %s", loaded.path.name)
