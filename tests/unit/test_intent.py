"""Unit tests for the intent file module."""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jj_review.intent import (
    _intent_filename,
    _remove_temporary_intent_file,
    check_same_kind_intent,
    intent_is_stale,
    match_ordered_change_ids,
    pid_is_alive,
    retire_superseded_intents,
    scan_intents,
    write_intent,
)
from jj_review.models.intent import (
    CleanupApplyIntent,
    CleanupRestackIntent,
    CloseIntent,
    LandIntent,
    LoadedIntent,
    RelinkIntent,
    SubmitIntent,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_submit_intent(
    ordered_change_ids: tuple[str, ...] = ("aaaa", "bbbb"),
    pid: int = 12345,
) -> SubmitIntent:
    return SubmitIntent(
        kind="submit",
        pid=pid,
        label="submit on @",
        display_revset="@",
        head_change_id="bbbb",
        ordered_change_ids=ordered_change_ids,
        bookmarks={"aaaa": "review/feat-1-aaaa", "bbbb": "review/feat-2-bbbb"},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )


def _make_cleanup_apply_intent(pid: int = 12345) -> CleanupApplyIntent:
    return CleanupApplyIntent(
        kind="cleanup-apply",
        pid=pid,
        label="cleanup --apply",
        started_at="2026-01-01T00:00:00+00:00",
    )


def _make_cleanup_restack_intent(
    ordered_change_ids: tuple[str, ...] = ("aaaa", "bbbb"),
    pid: int = 12345,
) -> CleanupRestackIntent:
    return CleanupRestackIntent(
        kind="cleanup-restack",
        pid=pid,
        label="cleanup --restack on @",
        display_revset="@",
        ordered_change_ids=ordered_change_ids,
        started_at="2026-01-01T00:00:00+00:00",
    )


def _make_close_intent(
    ordered_change_ids: tuple[str, ...] = ("aaaa", "bbbb"),
    cleanup: bool = False,
    pid: int = 12345,
) -> CloseIntent:
    return CloseIntent(
        kind="close",
        pid=pid,
        label="close on @",
        display_revset="@",
        ordered_change_ids=ordered_change_ids,
        cleanup=cleanup,
        started_at="2026-01-01T00:00:00+00:00",
    )


def _make_relink_intent(change_id: str = "cccc", pid: int = 12345) -> RelinkIntent:
    return RelinkIntent(
        kind="relink",
        pid=pid,
        label="relink for cccccccc",
        change_id=change_id,
        started_at="2026-01-01T00:00:00+00:00",
    )


def _make_land_intent(
    ordered_change_ids: tuple[str, ...] = ("aaaa", "bbbb", "cccc"),
    landed_change_ids: tuple[str, ...] = ("aaaa", "bbbb"),
    pid: int = 12345,
) -> LandIntent:
    return LandIntent(
        kind="land",
        pid=pid,
        label="land on @",
        bypass_readiness=False,
        display_revset="@",
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=("commit-aaaa", "commit-bbbb", "commit-cccc"),
        landed_change_ids=landed_change_ids,
        landed_bookmarks={
            "aaaa": "review/feature-1-aaaa",
            "bbbb": "review/feature-2-bbbb",
        },
        landed_commit_ids={
            "aaaa": "commit-aaaa",
            "bbbb": "commit-bbbb",
        },
        landed_pull_request_numbers={
            "aaaa": 1,
            "bbbb": 2,
        },
        landed_subjects={
            "aaaa": "feature 1",
            "bbbb": "feature 2",
        },
        completed_change_ids=("aaaa",),
        trunk_branch="main",
        trunk_commit_id="trunk-commit",
        landed_commit_id="landed-commit",
        expected_pr_number=2,
        started_at="2026-01-01T00:00:00+00:00",
    )


# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

def test_intent_filename_first_slot(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
    candidate = _intent_filename(tmp_path, now)
    assert candidate.name == "incomplete-2026-01-15-10-30.01.toml"


def test_intent_filename_collision(tmp_path: Path) -> None:
    now = datetime(2026, 1, 15, 10, 30, tzinfo=UTC)
    # Create the first slot
    first = _intent_filename(tmp_path, now)
    first.touch()
    second = _intent_filename(tmp_path, now)
    assert second.name == "incomplete-2026-01-15-10-30.02.toml"


# ---------------------------------------------------------------------------
# Round-trips
# ---------------------------------------------------------------------------

def test_submit_intent_round_trips(tmp_path: Path) -> None:
    intent = _make_submit_intent()
    path = write_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    loaded = results[0]
    assert loaded.path == path
    assert loaded.intent == intent


def test_cleanup_apply_intent_round_trips(tmp_path: Path) -> None:
    intent = _make_cleanup_apply_intent()
    write_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    assert results[0].intent == intent


def test_cleanup_restack_intent_round_trips(tmp_path: Path) -> None:
    intent = _make_cleanup_restack_intent()
    write_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    assert results[0].intent == intent


def test_close_intent_round_trips(tmp_path: Path) -> None:
    intent = _make_close_intent(cleanup=True)
    write_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    assert results[0].intent == intent


def test_relink_intent_round_trips(tmp_path: Path) -> None:
    intent = _make_relink_intent()
    write_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    assert results[0].intent == intent


def test_land_intent_round_trips(tmp_path: Path) -> None:
    intent = _make_land_intent()
    write_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    assert results[0].intent == intent


def test_scan_intents_ignores_unparseable_files(tmp_path: Path) -> None:
    bad = tmp_path / "incomplete-2026-01-15-10-30.01.toml"
    bad.write_text("not valid toml ][", encoding="utf-8")
    results = scan_intents(tmp_path)
    assert results == []


def test_scan_intents_sorted_by_name(tmp_path: Path) -> None:
    # Write two intents with different timestamps by writing files directly
    now1 = datetime(2026, 1, 15, 9, 0, tzinfo=UTC)
    now2 = datetime(2026, 1, 15, 10, 0, tzinfo=UTC)
    intent1 = _make_submit_intent(("aaaa",))
    intent2 = _make_submit_intent(("bbbb",))
    path1 = _intent_filename(tmp_path, now1)
    path2 = _intent_filename(tmp_path, now2)
    # Write via write_intent but rename to desired names
    written1 = write_intent(tmp_path, intent1)
    written1.rename(path1)
    written2 = write_intent(tmp_path, intent2)
    written2.rename(path2)
    results = scan_intents(tmp_path)
    assert len(results) == 2
    assert results[0].path.name < results[1].path.name


def test_remove_temporary_intent_file_logs_cleanup_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    temp_path = tmp_path / "incomplete.toml.tmp"
    temp_path.write_text("temp", encoding="utf-8")

    def fail_unlink(self: Path, missing_ok: bool = False) -> None:
        raise OSError("disk unhappy")

    monkeypatch.setattr(Path, "unlink", fail_unlink)

    with caplog.at_level(logging.WARNING):
        _remove_temporary_intent_file(temp_path)

    assert "Could not remove temporary intent file" in caplog.text


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_match_exact() -> None:
    assert match_ordered_change_ids(("a", "b"), ("a", "b")) == "exact"


def test_match_superset() -> None:
    # new is a superset: existing is a prefix of new
    assert match_ordered_change_ids(("a", "b"), ("a", "b", "c")) == "superset"


def test_match_overlap_partial() -> None:
    # Shares some IDs but neither is prefix of the other
    assert match_ordered_change_ids(("a", "b"), ("b", "c")) == "overlap"


def test_match_overlap_reordered() -> None:
    # Same IDs but reordered
    assert match_ordered_change_ids(("a", "b"), ("b", "a")) == "overlap"


def test_match_disjoint() -> None:
    assert match_ordered_change_ids(("a", "b"), ("c", "d")) == "disjoint"


def test_match_superset_requires_ordered_prefix() -> None:
    # ["a","b"] vs ["b","a","c"] — b appears first in new but old starts with a
    assert match_ordered_change_ids(("a", "b"), ("b", "a", "c")) == "overlap"


# ---------------------------------------------------------------------------
# PID liveness
# ---------------------------------------------------------------------------

def test_pid_is_alive_current_process() -> None:
    assert pid_is_alive(os.getpid()) is True


def test_pid_is_not_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", fake_kill)
    assert pid_is_alive(99999999) is False


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------

def test_retire_on_exact_match(tmp_path: Path) -> None:
    old = _make_submit_intent(("a", "b"))
    path = write_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_submit_intent(("a", "b"))
    retire_superseded_intents([loaded], new)
    assert not path.exists()


def test_retire_on_superset(tmp_path: Path) -> None:
    old = _make_submit_intent(("a", "b"))
    path = write_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_submit_intent(("a", "b", "c"))
    retire_superseded_intents([loaded], new)
    assert not path.exists()


def test_no_retire_on_overlap(tmp_path: Path) -> None:
    old = _make_submit_intent(("a", "b"))
    path = write_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_submit_intent(("b", "c"))
    retire_superseded_intents([loaded], new)
    assert path.exists()


def test_no_retire_on_disjoint(tmp_path: Path) -> None:
    old = _make_submit_intent(("a", "b"))
    path = write_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_submit_intent(("c", "d"))
    retire_superseded_intents([loaded], new)
    assert path.exists()


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------

def test_intent_is_stale_when_no_ids_resolve(tmp_path: Path) -> None:
    intent = _make_submit_intent(("aaaa", "bbbb"))
    assert intent_is_stale(intent, lambda cid: False) is True


def test_intent_is_not_stale_when_some_ids_resolve(tmp_path: Path) -> None:
    intent = _make_submit_intent(("aaaa", "bbbb"))
    assert intent_is_stale(intent, lambda cid: cid == "aaaa") is False


def test_cleanup_apply_intent_is_not_stale_when_recent_and_pid_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Dead PID, but only 1 day old — not stale yet (started_at = 2026-01-01)
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: False)
    intent = _make_cleanup_apply_intent(pid=99999999)
    one_day_after = datetime(2026, 1, 2, tzinfo=UTC)
    assert intent_is_stale(intent, lambda cid: False, now=one_day_after) is False


def test_cleanup_apply_intent_not_stale_when_pid_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: True)
    intent = _make_cleanup_apply_intent(pid=12345)
    # Even very old, if PID is alive → not stale
    old_time = datetime(2030, 1, 1, tzinfo=UTC)
    assert intent_is_stale(intent, lambda cid: False, now=old_time) is False


def test_cleanup_apply_intent_stale_when_old_and_pid_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: False)
    intent = _make_cleanup_apply_intent(pid=99999999)
    # 8 days after started_at (2026-01-01) → stale
    eight_days_after = datetime(2026, 1, 9, tzinfo=UTC)
    assert intent_is_stale(intent, lambda cid: False, now=eight_days_after) is True


def test_relink_intent_not_stale_when_pid_alive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: True)
    intent = _make_relink_intent(pid=12345)
    old_time = datetime(2030, 1, 1, tzinfo=UTC)
    assert intent_is_stale(intent, lambda cid: False, now=old_time) is False


def test_relink_intent_not_stale_when_recent_and_pid_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: False)
    intent = _make_relink_intent(pid=99999999)
    one_day_after = datetime(2026, 1, 2, tzinfo=UTC)
    assert intent_is_stale(intent, lambda cid: False, now=one_day_after) is False


def test_relink_intent_stale_when_old_and_pid_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: False)
    intent = _make_relink_intent(pid=99999999)
    eight_days_after = datetime(2026, 1, 9, tzinfo=UTC)
    assert intent_is_stale(intent, lambda cid: False, now=eight_days_after) is True


# ---------------------------------------------------------------------------
# check_same_kind_intent
# ---------------------------------------------------------------------------

def test_check_same_kind_intent_returns_stale_dead_pid_intents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: False)
    monkeypatch.setattr("jj_review.intent.time.sleep", lambda s: None)
    old_intent = _make_submit_intent(("aaaa", "bbbb"), pid=99999999)
    write_intent(tmp_path, old_intent)

    new_intent = _make_submit_intent(("aaaa", "bbbb"))
    result = check_same_kind_intent(tmp_path, new_intent)

    assert len(result) == 1
    assert result[0].intent == old_intent


def test_check_same_kind_intent_ignores_different_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: False)
    # Write a cleanup-apply intent (different kind)
    cleanup_intent = _make_cleanup_apply_intent(pid=99999999)
    write_intent(tmp_path, cleanup_intent)

    # Check for submit kind — should return nothing
    new_submit_intent = _make_submit_intent()
    result = check_same_kind_intent(tmp_path, new_submit_intent)

    assert result == []


def test_check_same_kind_intent_polls_until_pid_dies(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    call_count = [0]

    def fake_pid_is_alive(pid: int) -> bool:
        call_count[0] += 1
        # Return True twice (initial check + first poll), then False
        return call_count[0] <= 2

    sleep_calls: list[float] = []

    monkeypatch.setattr("jj_review.intent.pid_is_alive", fake_pid_is_alive)
    monkeypatch.setattr("jj_review.intent.time.sleep", lambda s: sleep_calls.append(s))

    old_intent = _make_submit_intent(("aaaa", "bbbb"), pid=99999999)
    write_intent(tmp_path, old_intent)

    new_intent = _make_submit_intent(("cccc", "dddd"))
    result = check_same_kind_intent(tmp_path, new_intent)

    # sleep was called while waiting for the PID to die
    assert len(sleep_calls) > 0
    # After the PID died, the intent is not returned as stale —
    # the caller just proceeds (the other process finished cleanly)
    assert result == []
