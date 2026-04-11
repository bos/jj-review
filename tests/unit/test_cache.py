from __future__ import annotations

import logging
from pathlib import Path

import pytest

from jj_review.cache import ReviewStateError, ReviewStateStore, ReviewStateUnavailable
from jj_review.models.cache import CachedChange, ReviewState


def test_review_state_store_round_trips_and_creates_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-review" / "repos" / "repo-id" / "state.json"
    store = ReviewStateStore(state_path)

    store.save(
        ReviewState(
            changes={
                "zvlywqkxtmnpqrstu": CachedChange(
                    bookmark="review/fix-cache-invalidation-zvlywqkx",
                    pr_review_decision="approved",
                    pr_state="open",
                )
            }
        )
    )

    loaded_state = store.load()

    assert loaded_state.changes["zvlywqkxtmnpqrstu"].bookmark == (
        "review/fix-cache-invalidation-zvlywqkx"
    )
    assert loaded_state.changes["zvlywqkxtmnpqrstu"].pr_review_decision == "approved"
    assert loaded_state.changes["zvlywqkxtmnpqrstu"].pr_state == "open"
    assert state_path.exists()


def test_review_state_store_preserves_unlinked_change_metadata(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    store = ReviewStateStore(state_path)

    store.save(
        ReviewState(
            changes={
                "zvlywqkxtmnpqrstu": CachedChange(
                    bookmark="review/fix-cache-invalidation-zvlywqkx",
                    unlinked_at="2026-03-22T12:34:56+00:00",
                    link_state="unlinked",
                )
            }
        )
    )

    loaded_change = store.load().changes["zvlywqkxtmnpqrstu"]

    assert loaded_change.bookmark == "review/fix-cache-invalidation-zvlywqkx"
    assert loaded_change.unlinked_at == "2026-03-22T12:34:56+00:00"
    assert loaded_change.is_unlinked is True


def test_review_state_store_returns_defaults_when_file_is_missing(tmp_path: Path) -> None:
    state = ReviewStateStore(tmp_path / "missing" / "state.json").load()

    assert state.version == 1
    assert state.changes == {}


def test_review_state_store_rejects_unknown_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(
        (
            '{\n'
            '  "version": 1,\n'
            '  "changes": {\n'
            '    "zvlywqkxtmnpqrstu": {\n'
            '      "bookmark_override": "review/custom-name"\n'
            "    }\n"
            "  }\n"
            "}\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReviewStateError, match="bookmark_override"):
        ReviewStateStore(state_path).load()


def test_require_writable_raises_when_disabled(tmp_path: Path) -> None:
    store = ReviewStateStore(None, disabled_reason="test reason")
    with pytest.raises(ReviewStateUnavailable, match="test reason"):
        store.require_writable()


def test_review_state_store_disables_persistence_when_repo_id_is_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def fail_resolve_state_path(_: Path) -> Path:
        raise ReviewStateUnavailable("repo config ID is unavailable")

    monkeypatch.setattr("jj_review.cache.resolve_state_path", fail_resolve_state_path)

    with caplog.at_level(logging.DEBUG, logger="jj_review.cache"):
        store = ReviewStateStore.for_repo(repo)
        loaded_state = store.load()
        store.save(ReviewState())

    assert loaded_state == ReviewState()
    assert "jj-review data disabled" in caplog.text
    assert "Skipping jj-review data save" in caplog.text
