"""Unit tests for the intent file module."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jj_review.intent import (
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
        label="cleanup",
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

@pytest.mark.parametrize(
    ("intent_factory", "test_id"),
    [
        (_make_submit_intent, "submit"),
        (_make_cleanup_apply_intent, "cleanup-apply"),
        (_make_cleanup_restack_intent, "cleanup-restack"),
        (lambda: _make_close_intent(cleanup=True), "close"),
        (_make_relink_intent, "relink"),
        (_make_land_intent, "land"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_write_intent_round_trips_supported_intent_kinds(
    tmp_path: Path,
    intent_factory,
    test_id: str,
) -> None:
    del test_id
    intent = intent_factory()
    path = write_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    assert results[0].path == path
    assert results[0].intent == intent


def test_scan_intents_ignores_unparseable_files(tmp_path: Path) -> None:
    bad = tmp_path / "incomplete-2026-01-15-10-30.01.toml"
    bad.write_text("not valid toml ][", encoding="utf-8")
    results = scan_intents(tmp_path)
    assert results == []


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def test_match_ordered_change_ids_returns_exact_for_identical_sequences() -> None:
    assert match_ordered_change_ids(("a", "b"), ("a", "b")) == "exact"


def test_match_ordered_change_ids_returns_superset_for_extended_prefix() -> None:
    # new is a superset: existing is a prefix of new
    assert match_ordered_change_ids(("a", "b"), ("a", "b", "c")) == "superset"


def test_match_ordered_change_ids_returns_overlap_for_partial_overlap() -> None:
    # Shares some IDs but neither is prefix of the other
    assert match_ordered_change_ids(("a", "b"), ("b", "c")) == "overlap"


def test_match_ordered_change_ids_returns_overlap_for_reordered_sequences() -> None:
    # Same IDs but reordered
    assert match_ordered_change_ids(("a", "b"), ("b", "a")) == "overlap"


def test_match_ordered_change_ids_returns_disjoint_for_non_overlapping_sequences() -> None:
    assert match_ordered_change_ids(("a", "b"), ("c", "d")) == "disjoint"


def test_match_ordered_change_ids_requires_prefix_order_for_superset() -> None:
    # ["a","b"] vs ["b","a","c"] — b appears first in new but old starts with a
    assert match_ordered_change_ids(("a", "b"), ("b", "a", "c")) == "overlap"


# ---------------------------------------------------------------------------
# PID liveness
# ---------------------------------------------------------------------------

def test_pid_is_alive_returns_true_for_current_process() -> None:
    assert pid_is_alive(os.getpid()) is True


def test_pid_is_alive_returns_false_for_missing_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_kill(pid: int, sig: int) -> None:
        raise ProcessLookupError(pid)

    monkeypatch.setattr(os, "kill", fake_kill)
    assert pid_is_alive(99999999) is False


# ---------------------------------------------------------------------------
# Retirement
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("new_ordered_change_ids", "should_exist"),
    [
        pytest.param(("a", "b"), False, id="exact-match"),
        pytest.param(("a", "b", "c"), False, id="extended-prefix"),
        pytest.param(("b", "c"), True, id="non-prefix-overlap"),
        pytest.param(("c", "d"), True, id="disjoint"),
    ],
)
def test_retire_superseded_submit_intents_matches_stack_prefix(
    tmp_path: Path,
    new_ordered_change_ids: tuple[str, ...],
    should_exist: bool,
) -> None:
    old = _make_submit_intent(("a", "b"))
    path = write_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_submit_intent(new_ordered_change_ids)
    retire_superseded_intents([loaded], new)
    assert path.exists() is should_exist


# ---------------------------------------------------------------------------
# Stale detection
# ---------------------------------------------------------------------------

def test_stack_intent_is_stale_when_no_change_ids_still_resolve(tmp_path: Path) -> None:
    intent = _make_submit_intent(("aaaa", "bbbb"))
    assert intent_is_stale(intent, lambda cid: False) is True


def test_stack_intent_stays_live_when_any_change_id_still_resolves(tmp_path: Path) -> None:
    intent = _make_submit_intent(("aaaa", "bbbb"))
    assert intent_is_stale(intent, lambda cid: cid == "aaaa") is False


@pytest.mark.parametrize(
    ("intent_factory", "pid", "pid_alive", "now", "expected_stale", "test_id"),
    [
        (
            _make_cleanup_apply_intent,
            99999999,
            False,
            datetime(2026, 1, 2, tzinfo=UTC),
            False,
            "cleanup-apply-recent-dead-pid",
        ),
        (
            _make_cleanup_apply_intent,
            12345,
            True,
            datetime(2030, 1, 1, tzinfo=UTC),
            False,
            "cleanup-apply-live-pid",
        ),
        (
            _make_cleanup_apply_intent,
            99999999,
            False,
            datetime(2026, 1, 9, tzinfo=UTC),
            True,
            "cleanup-apply-old-dead-pid",
        ),
        (
            _make_relink_intent,
            12345,
            True,
            datetime(2030, 1, 1, tzinfo=UTC),
            False,
            "relink-live-pid",
        ),
        (
            _make_relink_intent,
            99999999,
            False,
            datetime(2026, 1, 2, tzinfo=UTC),
            False,
            "relink-recent-dead-pid",
        ),
        (
            _make_relink_intent,
            99999999,
            False,
            datetime(2026, 1, 9, tzinfo=UTC),
            True,
            "relink-old-dead-pid",
        ),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_pid_based_intents_become_stale_only_when_old_and_dead(
    monkeypatch: pytest.MonkeyPatch,
    intent_factory,
    pid: int,
    pid_alive: bool,
    now: datetime,
    expected_stale: bool,
    test_id: str,
) -> None:
    del test_id
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda actual_pid: pid_alive)
    intent = intent_factory(pid=pid)
    assert intent_is_stale(intent, lambda cid: False, now=now) is expected_stale


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


def test_check_same_kind_intent_waits_for_live_same_kind_intent_to_finish(
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
