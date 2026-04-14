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
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from jj_review.formatting import short_change_id
from jj_review.models.intent import (
    CleanupIntent,
    CleanupRestackIntent,
    CloseIntent,
    IntentFile,
    LandIntent,
    LoadedIntent,
    MatchResult,
    RelinkIntent,
    SubmitIntent,
)
from jj_review.submit_recovery import should_retire_submit_after_submit

logger = logging.getLogger(__name__)
_INTENT_ADAPTER = TypeAdapter(IntentFile)

SubmitIntentMatch = Literal[
    "exact",
    "same-logical",
    "covered",
    "trimmed",
    "overlap",
    "disjoint",
]
CloseIntentModeRelation = Literal["same", "expanded", "incompatible"]


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


def write_new_intent(state_dir: Path, intent: IntentFile) -> Path:
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
        loaded for loaded in scan_intents(state_dir) if loaded.intent.kind == new_intent.kind
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
    if len(new) > len(existing) and new[: len(existing)] == existing:
        return "superset"
    if set(existing) & set(new):
        return "overlap"
    return "disjoint"


def describe_intent(intent: IntentFile) -> str:
    """Return a user-facing description for an intent."""

    if isinstance(intent, SubmitIntent):
        return (
            f"submit for {short_change_id(intent.head_change_id)} (from {intent.display_revset})"
        )
    if isinstance(intent, CleanupRestackIntent):
        head_change_id = intent.ordered_change_ids[-1] if intent.ordered_change_ids else "stack"
        return (
            f"cleanup --restack for {short_change_id(head_change_id)} "
            f"(from {intent.display_revset})"
        )
    if isinstance(intent, CloseIntent):
        verb = "close --cleanup" if intent.cleanup else "close"
        head_change_id = intent.ordered_change_ids[-1] if intent.ordered_change_ids else "stack"
        return f"{verb} for {short_change_id(head_change_id)} (from {intent.display_revset})"
    if isinstance(intent, LandIntent):
        head_change_id = intent.ordered_change_ids[-1] if intent.ordered_change_ids else "stack"
        return f"land for {short_change_id(head_change_id)} (from {intent.display_revset})"
    return intent.label


def match_recorded_ordered_stack(
    *,
    recorded_change_ids: tuple[str, ...],
    recorded_commit_ids: tuple[str, ...],
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
) -> SubmitIntentMatch:
    """Classify how a recorded ordered stack relates to the current stack."""

    if recorded_change_ids == current_change_ids:
        if recorded_commit_ids and recorded_commit_ids == current_commit_ids:
            return "exact"
        return "same-logical"
    if set(recorded_change_ids).issubset(current_change_ids):
        return "covered"
    if set(recorded_change_ids) & set(current_change_ids):
        return "overlap"
    return "disjoint"


def match_cleanup_restack_intent(
    *,
    intent: CleanupRestackIntent,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
) -> SubmitIntentMatch:
    """Classify how a recorded restack intent relates to the current stack."""

    if intent.ordered_change_ids == current_change_ids:
        if intent.ordered_commit_ids and intent.ordered_commit_ids == current_commit_ids:
            return "exact"
        return "same-logical"
    # Set-equality (same change IDs, different order) is also same-logical: reordering
    # changes the stack shape even if no commit content changed.
    if set(intent.ordered_change_ids) == set(current_change_ids):
        return "same-logical"
    # trimmed: the current stack is a strict subset — some changes were removed (landed,
    # abandoned) since the interruption. A rerun is safe; it uses the current stack.
    if set(current_change_ids).issubset(intent.ordered_change_ids):
        return "trimmed"
    return match_recorded_ordered_stack(
        recorded_change_ids=intent.ordered_change_ids,
        recorded_commit_ids=intent.ordered_commit_ids,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )


def match_close_intent(
    *,
    intent: CloseIntent,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
    current_cleanup: bool | None = None,
) -> SubmitIntentMatch:
    """Classify how a recorded close intent relates to the current stack.

    Pass current_cleanup=None to get a pure stack-shape match, ignoring whether
    the modes (plain close vs. --cleanup) are compatible. Callers use this to
    separately answer "does the stack match?" and "does the mode match?" — for
    example, to detect a recorded cleanup run whose stack still matches but whose
    mode cannot be resumed by a plain close.
    """

    if (
        current_cleanup is not None
        and close_intent_mode_relation(
            recorded_cleanup=intent.cleanup,
            current_cleanup=current_cleanup,
        )
        == "incompatible"
    ):
        return "disjoint"
    return match_recorded_ordered_stack(
        recorded_change_ids=intent.ordered_change_ids,
        recorded_commit_ids=intent.ordered_commit_ids,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )


def close_intent_mode_relation(
    *,
    recorded_cleanup: bool,
    current_cleanup: bool,
) -> CloseIntentModeRelation:
    """Classify whether a close mode can resume or supersede a recorded close."""

    if recorded_cleanup == current_cleanup:
        return "same"
    if current_cleanup and not recorded_cleanup:
        return "expanded"
    return "incompatible"


# ---------------------------------------------------------------------------
# Stale intent check
# ---------------------------------------------------------------------------


def intent_is_stale(
    intent: IntentFile,
    resolve_change_id: Callable[[str], bool],
    *,
    now: datetime | None = None,
) -> bool:
    """Return True if the intent is considered stale.

    For intents backed by review-stack change IDs
    (SubmitIntent, CleanupRestackIntent, CloseIntent, LandIntent):
        stale if none of the change IDs resolve in the local repo.
    For CleanupIntent and RelinkIntent:
        stale if the PID is dead AND the intent is older than 7 days.
    """
    if isinstance(intent, CleanupIntent | RelinkIntent):
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

    ids = intent.change_ids()
    if not ids:
        return False
    # "None resolve" rather than "any resolve": an intent remains actionable as
    # long as at least one of its change IDs still exists locally. A
    # partially-landed submit (some changes merged, some still local) should
    # stay visible in status rather than silently disappearing.
    return not any(resolve_change_id(cid) for cid in ids)


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------


def retire_superseded_intents(
    stale_intents: list[LoadedIntent],
    new_intent: IntentFile,
) -> None:
    """Auto-retire stale intents that a later successful run has superseded."""
    if not isinstance(new_intent, SubmitIntent | CleanupRestackIntent | CloseIntent | LandIntent):
        return
    new_ids = new_intent.ordered_change_ids
    for loaded in stale_intents:
        old = loaded.intent
        if isinstance(new_intent, SubmitIntent):
            if not isinstance(old, SubmitIntent):
                continue
            should_retire = should_retire_submit_after_submit(
                old_intent=old,
                new_intent=new_intent,
            )
        elif isinstance(new_intent, CloseIntent):
            if isinstance(old, CloseIntent):
                # Same subset rule as submit, plus mode compatibility: a plain
                # close does not retire an old cleanup run because the cleanup
                # steps may still be outstanding.
                should_retire = close_intent_mode_relation(
                    recorded_cleanup=old.cleanup,
                    current_cleanup=new_intent.cleanup,
                ) != "incompatible" and set(old.ordered_change_ids).issubset(new_ids)
            else:
                continue
        elif isinstance(new_intent, CleanupRestackIntent):
            if not isinstance(old, CleanupRestackIntent):
                continue
            # Any overlap is enough for restack: a later successful restack
            # implies the shared changes have been rewritten into the new
            # topology, so the old record is no longer a useful resumption
            # point. Unlike submit, there is no risk of leaving unaccounted
            # changes — the restack target is the whole current stack.
            should_retire = bool(set(old.ordered_change_ids) & set(new_ids))
        else:
            if not isinstance(old, LandIntent):
                continue
            result = match_ordered_change_ids(old.ordered_change_ids, new_ids)
            should_retire = result in ("exact", "superset")
        if should_retire:
            loaded.path.unlink(missing_ok=True)
            logger.debug("Retired superseded intent %s", loaded.path.name)
