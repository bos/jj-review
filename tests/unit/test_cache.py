from __future__ import annotations

from pathlib import Path

from jj_review.cache import ReviewStateStore
from jj_review.config import load_config
from jj_review.models.cache import CachedChange, ReviewState


def test_review_state_store_round_trips_and_preserves_config_sections(tmp_path: Path) -> None:
    state_path = tmp_path / ".jj-review.toml"
    state_path.write_text(
        "\n".join(
            [
                "[repo]",
                'remote = "origin"',
                'trunk_branch = "main"',
                "",
                "[logging]",
                'level = "DEBUG"',
                "",
            ]
        ),
        encoding="utf-8",
    )
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
    loaded_config = load_config(repo_root=tmp_path)

    assert loaded_state.changes["zvlywqkxtmnpqrstu"].bookmark == (
        "review/fix-cache-invalidation-zvlywqkx"
    )
    assert loaded_config.repo.remote == "origin"
    assert loaded_config.repo.trunk_branch == "main"
    assert loaded_config.logging.level == "DEBUG"


def test_review_state_store_returns_defaults_when_file_is_missing(tmp_path: Path) -> None:
    state = ReviewStateStore(tmp_path / ".jj-review.toml").load()

    assert state.version == 1
    assert state.changes == {}
