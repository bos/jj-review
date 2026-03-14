from __future__ import annotations

import logging
from pathlib import Path

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
                )
            }
        )
    )

    loaded_state = store.load()

    assert loaded_state.changes["zvlywqkxtmnpqrstu"].bookmark == (
        "review/fix-cache-invalidation-zvlywqkx"
    )
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

    with caplog.at_level(logging.DEBUG):
        store = ReviewStateStore.for_repo(repo)
        loaded_state = store.load()
        store.save(ReviewState())

    assert loaded_state == ReviewState()
    assert "Review state disabled" in caplog.text
    assert "Skipping review state save" in caplog.text
