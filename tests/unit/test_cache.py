from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from jj_review.cache import ReviewStateError, ReviewStateStore, ReviewStateUnavailable
from jj_review.models.cache import CachedChange, ReviewState


def test_review_state_store_round_trips_and_creates_parent_directories(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "jj-review" / "repos" / "repo-id" / "state.toml"
    store = ReviewStateStore(state_path)

    store.save(
        ReviewState(
            change={
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


def test_review_state_store_returns_defaults_when_file_is_missing(tmp_path: Path) -> None:
    state = ReviewStateStore(tmp_path / "missing" / "state.toml").load()

    assert state.version == 1
    assert state.changes == {}


def test_review_state_store_rejects_unknown_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "state.toml"
    state_path.write_text(
        "\n".join(
            [
                "version = 1",
                "",
                '[change."zvlywqkxtmnpqrstu"]',
                'bookmark_override = "review/custom-name"',
            ]
        ),
        encoding="utf-8",
    )

    with pytest.raises(ReviewStateError, match="bookmark_override"):
        ReviewStateStore(state_path).load()


def test_save_leaves_no_temp_file_on_success(tmp_path: Path) -> None:
    state_path = tmp_path / "state.toml"
    store = ReviewStateStore(state_path)

    store.save(ReviewState(change={"abc": CachedChange(bookmark="review/abc")}))

    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []
    assert state_path.exists()


def test_save_preserves_original_and_leaves_no_temp_file_when_write_fails(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.toml"
    original = ReviewState(change={"abc": CachedChange(bookmark="review/abc-original")})
    store = ReviewStateStore(state_path)
    store.save(original)

    original_text = state_path.read_text(encoding="utf-8")

    real_fdopen = os.fdopen

    def fail_fdopen(fd, *args, **kwargs):
        fobj = real_fdopen(fd, *args, **kwargs)
        fobj.write = lambda _: (_ for _ in ()).throw(OSError("disk full"))
        return fobj

    with patch("os.fdopen", side_effect=fail_fdopen):
        with pytest.raises(ReviewStateError, match="Could not write"):
            store.save(ReviewState(change={"abc": CachedChange(bookmark="review/abc-new")}))

    assert state_path.read_text(encoding="utf-8") == original_text
    leftover = list(tmp_path.glob("*.tmp"))
    assert leftover == []


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
    assert "Review state disabled" in caplog.text
    assert "Skipping review state save" in caplog.text
