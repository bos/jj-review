"""Unit tests for the intent file module."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jj_review.intent import (
    check_same_kind_intent,
    intent_is_stale,
    match_cleanup_restack_intent,
    match_close_intent,
    match_ordered_change_ids,
    match_submit_intent,
    pid_is_alive,
    retire_superseded_intents,
    scan_intents,
    write_new_intent,
)
from jj_review.models.intent import (
    CleanupIntent,
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
        ordered_commit_ids=("commit-aaaa", "commit-bbbb"),
        head_change_id="bbbb",
        remote_name="origin",
        github_host="github.test",
        github_owner="octo-org",
        github_repo="stacked-review",
        ordered_change_ids=ordered_change_ids,
        bookmarks={"aaaa": "review/feat-1-aaaa", "bbbb": "review/feat-2-bbbb"},
        bases={},
        started_at="2026-01-01T00:00:00+00:00",
    )


def _make_cleanup_intent(
    pid: int = 12345,
) -> CleanupIntent:
    return CleanupIntent(
        kind="cleanup",
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
        ordered_commit_ids=("commit-aaaa", "commit-bbbb"),
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
        ordered_commit_ids=("commit-aaaa", "commit-bbbb"),
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
        cleanup_bookmarks=True,
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
        (_make_cleanup_intent, "cleanup"),
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
    path = write_new_intent(tmp_path, intent)
    results = scan_intents(tmp_path)
    assert len(results) == 1
    assert results[0].path == path
    assert results[0].intent == intent


def test_scan_intents_ignores_unparseable_files(tmp_path: Path) -> None:
    bad = tmp_path / "incomplete-2026-01-15-10-30.01.json"
    bad.write_text('{"not valid json"', encoding="utf-8")
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


def test_match_submit_intent_returns_exact_for_matching_change_and_commit_ids() -> None:
    assert (
        match_submit_intent(
            intent=_make_submit_intent(),
            current_change_ids=("aaaa", "bbbb"),
            current_commit_ids=("commit-aaaa", "commit-bbbb"),
        )
        == "exact"
    )


def test_match_submit_intent_returns_same_logical_for_rewritten_stack() -> None:
    assert (
        match_submit_intent(
            intent=_make_submit_intent(),
            current_change_ids=("aaaa", "bbbb"),
            current_commit_ids=("new-aaaa", "new-bbbb"),
        )
        == "same-logical"
    )


def test_match_submit_intent_returns_covered_for_extended_current_stack() -> None:
    assert (
        match_submit_intent(
            intent=_make_submit_intent(("aaaa", "bbbb")),
            current_change_ids=("aaaa", "bbbb", "cccc"),
            current_commit_ids=("commit-aaaa", "commit-bbbb", "commit-cccc"),
        )
        == "covered"
    )


def test_match_submit_intent_returns_covered_for_reordered_current_stack() -> None:
    assert (
        match_submit_intent(
            intent=_make_submit_intent(("aaaa", "bbbb")),
            current_change_ids=("bbbb", "aaaa"),
            current_commit_ids=("commit-bbbb", "commit-aaaa"),
        )
        == "covered"
    )


def test_match_cleanup_restack_intent_returns_same_logical_for_rewritten_stack() -> None:
    assert (
        match_cleanup_restack_intent(
            intent=_make_cleanup_restack_intent(),
            current_change_ids=("aaaa", "bbbb"),
            current_commit_ids=("new-aaaa", "new-bbbb"),
        )
        == "same-logical"
    )


def test_match_cleanup_restack_intent_returns_same_logical_for_reordered_stack() -> None:
    assert (
        match_cleanup_restack_intent(
            intent=_make_cleanup_restack_intent(("aaaa", "bbbb")),
            current_change_ids=("bbbb", "aaaa"),
            current_commit_ids=("commit-bbbb", "commit-aaaa"),
        )
        == "same-logical"
    )


def test_match_cleanup_restack_intent_returns_trimmed_for_shrunk_current_stack() -> None:
    assert (
        match_cleanup_restack_intent(
            intent=_make_cleanup_restack_intent(("aaaa", "bbbb", "cccc")),
            current_change_ids=("bbbb", "cccc"),
            current_commit_ids=("commit-bbbb", "commit-cccc"),
        )
        == "trimmed"
    )


def test_retire_superseded_intents_retires_reordered_cleanup_restack_intent(
    tmp_path: Path,
) -> None:
    old_path = write_new_intent(
        tmp_path,
        _make_cleanup_restack_intent(("aaaa", "bbbb")),
    )

    retire_superseded_intents(
        [
            LoadedIntent(
                path=old_path,
                intent=_make_cleanup_restack_intent(("aaaa", "bbbb")),
            )
        ],
        _make_cleanup_restack_intent(("bbbb", "aaaa")),
    )

    assert not old_path.exists()


def test_retire_superseded_intents_retires_cleanup_restack_when_current_stack_shrinks(
    tmp_path: Path,
) -> None:
    old_path = write_new_intent(
        tmp_path,
        _make_cleanup_restack_intent(("aaaa", "bbbb", "cccc")),
    )

    retire_superseded_intents(
        [
            LoadedIntent(
                path=old_path,
                intent=_make_cleanup_restack_intent(("aaaa", "bbbb", "cccc")),
            )
        ],
        _make_cleanup_restack_intent(("bbbb", "cccc")),
    )

    assert not old_path.exists()


def test_retire_superseded_intents_retires_overlapping_cleanup_restack_intent(
    tmp_path: Path,
) -> None:
    old_path = write_new_intent(
        tmp_path,
        _make_cleanup_restack_intent(("aaaa", "bbbb", "cccc")),
    )

    retire_superseded_intents(
        [
            LoadedIntent(
                path=old_path,
                intent=_make_cleanup_restack_intent(("aaaa", "bbbb", "cccc")),
            )
        ],
        _make_cleanup_restack_intent(("bbbb", "dddd")),
    )

    assert not old_path.exists()


def test_retire_superseded_intents_retires_reordered_submit_intent(tmp_path: Path) -> None:
    old_path = write_new_intent(
        tmp_path,
        _make_submit_intent(("aaaa", "bbbb")),
    )

    retire_superseded_intents(
        [LoadedIntent(path=old_path, intent=_make_submit_intent(("aaaa", "bbbb")))],
        _make_submit_intent(("bbbb", "aaaa")),
    )

    assert not old_path.exists()


def test_retire_superseded_intents_keeps_submit_intent_when_new_submit_uses_other_remote(
    tmp_path: Path,
) -> None:
    old = _make_submit_intent(("aaaa", "bbbb"))
    path = write_new_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_submit_intent(("aaaa", "bbbb")).model_copy(update={"remote_name": "upstream"})

    retire_superseded_intents([loaded], new)

    assert path.exists()


def test_retire_superseded_intents_only_retires_matching_remote_submit_intent(
    tmp_path: Path,
) -> None:
    origin = _make_submit_intent(("aaaa", "bbbb"))
    origin_path = write_new_intent(tmp_path, origin)
    upstream = origin.model_copy(
        update={
            "remote_name": "upstream",
            "github_repo": "other-review",
        }
    )
    upstream_path = write_new_intent(tmp_path, upstream)

    retire_superseded_intents(
        [
            LoadedIntent(path=origin_path, intent=origin),
            LoadedIntent(path=upstream_path, intent=upstream),
        ],
        origin,
    )

    assert not origin_path.exists()
    assert upstream_path.exists()


def test_retire_superseded_intents_keeps_submit_intent_when_remote_name_is_reused_for_other_repo(
    tmp_path: Path,
) -> None:
    old = _make_submit_intent(("aaaa", "bbbb"))
    path = write_new_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = old.model_copy(update={"github_repo": "other-review"})

    retire_superseded_intents([loaded], new)

    assert path.exists()


def test_match_close_intent_returns_disjoint_when_cleanup_mode_differs() -> None:
    assert (
        match_close_intent(
            intent=_make_close_intent(cleanup=True),
            current_change_ids=("aaaa", "bbbb"),
            current_commit_ids=("commit-aaaa", "commit-bbbb"),
            current_cleanup=False,
        )
        == "disjoint"
    )


def test_retire_superseded_intents_keeps_close_intent_when_cleanup_mode_differs(
    tmp_path: Path,
) -> None:
    old = _make_close_intent(("aaaa", "bbbb"), cleanup=True)
    path = write_new_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_close_intent(("aaaa", "bbbb"), cleanup=False)

    retire_superseded_intents([loaded], new)

    assert path.exists()


def test_retire_superseded_intents_retires_plain_close_when_cleanup_close_covers_it(
    tmp_path: Path,
) -> None:
    old = _make_close_intent(("aaaa", "bbbb"), cleanup=False)
    path = write_new_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_close_intent(("aaaa", "bbbb"), cleanup=True)

    retire_superseded_intents([loaded], new)

    assert not path.exists()


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
    ("new_ordered_change_ids", "new_bookmarks", "should_exist"),
    [
        pytest.param(("a", "b"), None, False, id="exact-match"),
        pytest.param(
            ("a", "b", "c"),
            {
                "a": "review/feat-1-aaaa",
                "b": "review/feat-2-bbbb",
                "c": "review/feat-3-cccc",
            },
            False,
            id="extended-prefix",
        ),
        pytest.param(
            ("b", "c"),
            {"b": "review/feat-9-bbbb", "c": "review/feat-10-cccc"},
            True,
            id="non-prefix-overlap",
        ),
        pytest.param(
            ("c", "d"),
            {"c": "review/feat-11-cccc", "d": "review/feat-12-dddd"},
            True,
            id="disjoint",
        ),
    ],
)
def test_retire_superseded_submit_intents_requires_matching_bookmark_identity(
    tmp_path: Path,
    new_ordered_change_ids: tuple[str, ...],
    new_bookmarks: dict[str, str] | None,
    should_exist: bool,
) -> None:
    old = _make_submit_intent(("a", "b"))
    path = write_new_intent(tmp_path, old)
    loaded = LoadedIntent(path=path, intent=old)
    new = _make_submit_intent(new_ordered_change_ids)
    if new_bookmarks is not None:
        new = new.model_copy(update={"bookmarks": new_bookmarks})
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
            _make_cleanup_intent,
            99999999,
            False,
            datetime(2026, 1, 2, tzinfo=UTC),
            False,
            "cleanup-recent-dead-pid",
        ),
        (
            _make_cleanup_intent,
            12345,
            True,
            datetime(2030, 1, 1, tzinfo=UTC),
            False,
            "cleanup-live-pid",
        ),
        (
            _make_cleanup_intent,
            99999999,
            False,
            datetime(2026, 1, 9, tzinfo=UTC),
            True,
            "cleanup-old-dead-pid",
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
    write_new_intent(tmp_path, old_intent)

    new_intent = _make_submit_intent(("aaaa", "bbbb"))
    result = check_same_kind_intent(tmp_path, new_intent)

    assert len(result) == 1
    assert result[0].intent == old_intent


def test_check_same_kind_intent_ignores_different_kind(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("jj_review.intent.pid_is_alive", lambda pid: False)
    # Write a cleanup intent (different kind)
    cleanup_intent = _make_cleanup_intent(pid=99999999)
    write_new_intent(tmp_path, cleanup_intent)

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
    write_new_intent(tmp_path, old_intent)

    new_intent = _make_submit_intent(("cccc", "dddd"))
    result = check_same_kind_intent(tmp_path, new_intent)

    # sleep was called while waiting for the PID to die
    assert len(sleep_calls) > 0
    # After the PID died, the intent is not returned as stale —
    # the caller just proceeds (the other process finished cleanly)
    assert result == []
