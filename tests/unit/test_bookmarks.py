from __future__ import annotations

from jj_review.bookmarks import BookmarkResolver, generate_bookmark_name
from jj_review.config import ChangeConfig
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision


def test_generate_bookmark_name_normalizes_subject() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation!!!\n\nBody text.\n",
    )

    bookmark = generate_bookmark_name(revision)

    assert bookmark == "review/fix-cache-invalidation-zvlywqkx"


def test_generate_bookmark_name_falls_back_for_blank_subject() -> None:
    revision = _revision(change_id="abcdefghijklmno", description="\n")

    bookmark = generate_bookmark_name(revision)

    assert bookmark == "review/change-abcdefgh"


def test_bookmark_resolver_pins_generated_names() -> None:
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation\n",
    )

    result = BookmarkResolver(ReviewState()).pin_revisions((revision,))

    assert result.changed is True
    assert result.resolutions[0].bookmark == "review/fix-cache-invalidation-zvlywqkx"
    assert result.resolutions[0].source == "generated"
    assert (
        result.state.changes["zvlywqkxtmnpqrstu"].bookmark
        == "review/fix-cache-invalidation-zvlywqkx"
    )


def test_bookmark_resolver_reuses_pinned_bookmark_after_subject_changes() -> None:
    state = ReviewState(
        change={
            "zvlywqkxtmnpqrstu": CachedChange(bookmark="review/fix-cache-invalidation-zvlywqkx")
        }
    )
    renamed_revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Rewrite cache invalidation from scratch\n",
    )

    result = BookmarkResolver(state).pin_revisions((renamed_revision,))

    assert result.changed is False
    assert result.resolutions[0].bookmark == "review/fix-cache-invalidation-zvlywqkx"
    assert result.resolutions[0].source == "cache"


def test_bookmark_resolver_prefers_explicit_override() -> None:
    state = ReviewState(
        change={
            "zvlywqkxtmnpqrstu": CachedChange(bookmark="review/fix-cache-invalidation-zvlywqkx")
        }
    )
    revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Fix cache invalidation\n",
    )

    result = BookmarkResolver(
        state,
        {"zvlywqkxtmnpqrstu": ChangeConfig(bookmark_override="review/custom-name")},
    ).pin_revisions((revision,))

    assert result.changed is False
    assert result.resolutions[0].bookmark == "review/custom-name"
    assert result.resolutions[0].source == "override"


def _revision(*, change_id: str, description: str) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=f"{change_id}-commit",
        current_working_copy=False,
        description=description,
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("parent",),
    )
