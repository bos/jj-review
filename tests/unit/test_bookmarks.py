from __future__ import annotations

import pytest

from jj_review.bookmarks import (
    BookmarkResolver,
    ResolvedBookmark,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
    generate_bookmark_name,
)
from jj_review.config import ChangeConfig
from jj_review.errors import CliError
from jj_review.models.bookmarks import BookmarkState, RemoteBookmarkState
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


def test_bookmark_resolver_generates_and_pins_bookmark_when_no_mapping_exists() -> None:
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


def test_bookmark_resolver_keeps_cached_bookmark_stable_after_subject_change() -> None:
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


def test_bookmark_resolver_prefers_override_over_cached_bookmark() -> None:
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


def test_bookmark_resolver_reuses_discovered_bookmark_when_cache_is_missing() -> None:
    renamed_revision = _revision(
        change_id="zvlywqkxtmnpqrstu",
        description="Rewrite cache invalidation from scratch\n",
    )

    result = BookmarkResolver(
        ReviewState(),
        discovered_bookmarks={
            "zvlywqkxtmnpqrstu": "review/fix-cache-invalidation-zvlywqkx"
        },
    ).pin_revisions((renamed_revision,))

    assert result.changed is True
    assert result.resolutions[0].bookmark == "review/fix-cache-invalidation-zvlywqkx"
    assert result.resolutions[0].source == "discovered"
    assert (
        result.state.changes["zvlywqkxtmnpqrstu"].bookmark
        == "review/fix-cache-invalidation-zvlywqkx"
    )


def test_discover_bookmarks_reuses_unique_remote_bookmark_with_matching_change_id_suffix(
) -> None:
    bookmarks = discover_bookmarks_for_revisions(
        bookmark_states={
            "review/original-title-zvlywqkx": BookmarkState(
                name="review/original-title-zvlywqkx",
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("abc123",)),),
            ),
        },
        remote_name="origin",
        revisions=(
            _revision(change_id="zvlywqkxtmnpqrstu", description=""),
        ),
    )

    assert bookmarks == {"zvlywqkxtmnpqrstu": "review/original-title-zvlywqkx"}


def test_discover_bookmarks_for_revisions_rejects_ambiguous_matches() -> None:
    with pytest.raises(
        CliError,
        match="multiple existing bookmarks match",
    ):
        discover_bookmarks_for_revisions(
            bookmark_states={
                "review/first-zvlywqkx": BookmarkState(
                    name="review/first-zvlywqkx",
                    remote_targets=(
                        RemoteBookmarkState(remote="origin", targets=("abc123",)),
                    ),
                ),
                "review/second-zvlywqkx": BookmarkState(
                    name="review/second-zvlywqkx",
                    remote_targets=(
                        RemoteBookmarkState(remote="origin", targets=("def456",)),
                    ),
                ),
            },
            remote_name="origin",
            revisions=(
                _revision(change_id="zvlywqkxtmnpqrstu", description=""),
            ),
        )


def test_ensure_unique_bookmarks_rejects_multiple_changes_resolving_to_same_bookmark() -> None:
    resolutions = (
        ResolvedBookmark(
            bookmark="review/shared-name",
            change_id="change-a",
            source="override",
        ),
        ResolvedBookmark(
            bookmark="review/shared-name",
            change_id="change-b",
            source="cache",
        ),
    )

    with pytest.raises(
        CliError,
        match="multiple changes to the same bookmark",
    ):
        ensure_unique_bookmarks(resolutions)


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
