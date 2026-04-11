"""Filesystem operations and logic for per-operation intent files."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from jj_review.models.intent import (
    CleanupApplyIntent,
    CleanupRestackIntent,
    CloseIntent,
    IntentFile,
    LandIntent,
    LoadedIntent,
    MatchResult,
    RelinkIntent,
    SubmitIntent,
)

logger = logging.getLogger(__name__)
_INTENT_ADAPTER = TypeAdapter(IntentFile)


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------

def _intent_filename(state_dir: Path, now: datetime) -> Path:
    base = now.strftime("%Y-%m-%d-%H-%M")
    for nn in range(1, 100):
        candidate = state_dir / f"incomplete-{base}.{nn:02d}.json"
        if not candidate.exists():
            return candidate
    raise RuntimeError("Could not allocate intent file name (100 collisions).")


def write_intent(state_dir: Path, intent: IntentFile) -> Path:
    """Write an intent file atomically. Returns the path of the created file."""
    state_dir.mkdir(parents=True, exist_ok=True)
    dest = _intent_filename(state_dir, datetime.now(UTC))
    save_intent(dest, intent)
    logger.debug("Wrote intent file %s", dest.name)
    return dest


def save_intent(path: Path, intent: IntentFile) -> None:
    """Persist an intent atomically at a known path."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _write_intent_file(path, intent)


def _write_intent_file(path: Path, intent: IntentFile) -> None:
    rendered = intent.model_dump_json(exclude_none=True, indent=2) + "\n"
    fd, tmp_path_str = tempfile.mkstemp(dir=path.parent, suffix=".json.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(rendered)
        Path(tmp_path_str).replace(path)
    except Exception:
        try:
            Path(tmp_path_str).unlink()
        except FileNotFoundError:
            pass
        except OSError as error:
            logger.warning(
                "Could not remove temporary intent file %s: %s",
                tmp_path_str,
                error,
            )
        raise


def scan_intents(state_dir: Path) -> list[LoadedIntent]:
    results = []
    for p in sorted(state_dir.glob("incomplete-*.json")):
        try:
            loaded = LoadedIntent(
                path=p,
                intent=_INTENT_ADAPTER.validate_json(p.read_text(encoding="utf-8")),
            )
        except OSError as error:
            logger.error("Could not read intent file %s: %s", p, error)
            continue
        except ValidationError as error:
            logger.error("Could not parse intent file %s: %s", p, error)
            continue
        results.append(loaded)
    return results

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
    if isinstance(intent, CloseIntent):
        return frozenset(intent.ordered_change_ids)
    if isinstance(intent, RelinkIntent):
        return frozenset([intent.change_id])
    return frozenset()


# Keep private alias so internal callers can continue to use the clearer public name.
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
    if not isinstance(
        new_intent,
        SubmitIntent | CleanupRestackIntent | CloseIntent | LandIntent,
    ):
        return
    new_ids = new_intent.ordered_change_ids
    for loaded in stale_intents:
        old = loaded.intent
        if not isinstance(old, type(new_intent)):
            continue
        result = match_ordered_change_ids(old.ordered_change_ids, new_ids)
        if result in ("exact", "superset"):
            loaded.path.unlink(missing_ok=True)
            logger.debug("Retired superseded intent %s", loaded.path.name)
