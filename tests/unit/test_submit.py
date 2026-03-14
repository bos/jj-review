from __future__ import annotations

import pytest

from jj_review.bookmarks import ResolvedBookmark
from jj_review.commands.submit import (
    SubmitBookmarkCollisionError,
    SubmitBookmarkConflictError,
    SubmitRemoteBookmarkConflictError,
    SubmitRemoteBookmarkOwnershipError,
    SubmitRemoteResolutionError,
    _bookmark_linkage_is_proven,
    _ensure_remote_can_be_updated,
    _ensure_unique_bookmarks,
    _remote_is_up_to_date,
    _resolve_local_action,
    _should_update_untracked_remote_with_git,
    select_submit_remote,
)
from jj_review.config import RepoConfig
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, ReviewState


def test_select_submit_remote_prefers_configured_remote() -> None:
    remote = select_submit_remote(
        RepoConfig(remote="upstream"),
        (
            GitRemote(name="origin", url="git@example.com:org/repo.git"),
            GitRemote(name="upstream", url="git@example.com:org/repo.git"),
        ),
    )

    assert remote.name == "upstream"


def test_select_submit_remote_uses_origin_when_multiple_remotes_exist() -> None:
    remote = select_submit_remote(
        RepoConfig(),
        (
            GitRemote(name="origin", url="git@example.com:org/repo.git"),
            GitRemote(name="backup", url="git@example.com:org/repo.git"),
        ),
    )

    assert remote.name == "origin"


def test_select_submit_remote_uses_only_remote_when_unambiguous() -> None:
    remote = select_submit_remote(
        RepoConfig(),
        (GitRemote(name="upstream", url="git@example.com:org/repo.git"),),
    )

    assert remote.name == "upstream"


def test_select_submit_remote_rejects_missing_configured_remote() -> None:
    with pytest.raises(SubmitRemoteResolutionError, match="Configured remote 'origin'"):
        select_submit_remote(
            RepoConfig(remote="origin"),
            (GitRemote(name="upstream", url="git@example.com:org/repo.git"),),
        )


def test_select_submit_remote_rejects_ambiguous_remote_set_without_origin() -> None:
    with pytest.raises(
        SubmitRemoteResolutionError,
        match="Could not determine which Git remote to use for submit",
    ):
        select_submit_remote(
            RepoConfig(),
            (
                GitRemote(name="backup", url="git@example.com:org/repo.git"),
                GitRemote(name="upstream", url="git@example.com:org/repo.git"),
            ),
        )


def test_select_submit_remote_rejects_empty_remote_list() -> None:
    with pytest.raises(
        SubmitRemoteResolutionError,
        match="Could not determine which Git remote to use for submit",
    ):
        select_submit_remote(RepoConfig(), ())


def test_resolve_local_action_created_when_no_local_targets() -> None:
    assert _resolve_local_action("review/foo", (), "abc123") == "created"


def test_resolve_local_action_unchanged_when_target_matches() -> None:
    assert _resolve_local_action("review/foo", ("abc123",), "abc123") == "unchanged"


def test_resolve_local_action_moved_when_target_differs() -> None:
    assert _resolve_local_action("review/foo", ("old123",), "abc123") == "moved"


def test_resolve_local_action_rejects_conflicted_bookmark() -> None:
    with pytest.raises(
        SubmitBookmarkConflictError,
        match="2 conflicting local targets",
    ):
        _resolve_local_action("review/foo", ("abc123", "def456"), "abc123")


def test_remote_is_up_to_date_when_untracked_remote_target_matches() -> None:
    remote_state = RemoteBookmarkState(
        remote="origin",
        targets=("abc123",),
        tracking_targets=(),
    )

    assert _remote_is_up_to_date(remote_state, "abc123") is True


def test_bookmark_linkage_is_proven_by_existing_local_bookmark() -> None:
    assert _bookmark_linkage_is_proven(
        bookmark="review/foo",
        bookmark_source="generated",
        bookmark_state=BookmarkState(name="review/foo", local_targets=("abc123",)),
        change_id="change-a",
        state=ReviewState(),
    )


def test_bookmark_linkage_is_proven_by_cached_bookmark() -> None:
    assert _bookmark_linkage_is_proven(
        bookmark="review/foo",
        bookmark_source="cache",
        bookmark_state=BookmarkState(name="review/foo"),
        change_id="change-a",
        state=ReviewState(change={"change-a": CachedChange(bookmark="review/foo")}),
    )


def test_bookmark_linkage_is_not_proven_by_newly_generated_name() -> None:
    assert not _bookmark_linkage_is_proven(
        bookmark="review/foo",
        bookmark_source="generated",
        bookmark_state=BookmarkState(name="review/foo"),
        change_id="change-a",
        state=ReviewState(change={"change-a": CachedChange(bookmark="review/foo")}),
    )


def test_ensure_remote_can_be_updated_rejects_conflicted_remote_bookmark() -> None:
    with pytest.raises(
        SubmitRemoteBookmarkConflictError,
        match="Remote bookmark 'review/foo'@origin is conflicted",
    ):
        _ensure_remote_can_be_updated(
            bookmark="review/foo",
            bookmark_source="cache",
            bookmark_state=BookmarkState(name="review/foo"),
            change_id="change-a",
            desired_target="zzz999",
            remote="origin",
            remote_state=RemoteBookmarkState(
                remote="origin",
                targets=("abc123", "def456"),
                tracking_targets=("abc123", "def456"),
            ),
            state=ReviewState(change={"change-a": CachedChange(bookmark="review/foo")}),
        )


def test_ensure_remote_can_be_updated_rejects_unproven_existing_remote_branch() -> None:
    with pytest.raises(
        SubmitRemoteBookmarkOwnershipError,
        match="already exists and points elsewhere",
    ):
        _ensure_remote_can_be_updated(
            bookmark="review/foo",
            bookmark_source="generated",
            bookmark_state=BookmarkState(name="review/foo"),
            change_id="change-a",
            desired_target="def456",
            remote="origin",
            remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
            state=ReviewState(),
        )


def test_ensure_remote_can_be_updated_allows_matching_untracked_remote_branch() -> None:
    _ensure_remote_can_be_updated(
        bookmark="review/foo",
        bookmark_source="generated",
        bookmark_state=BookmarkState(name="review/foo"),
        change_id="change-a",
        desired_target="abc123",
        remote="origin",
        remote_state=RemoteBookmarkState(remote="origin", targets=("abc123",)),
        state=ReviewState(),
    )


def test_should_update_untracked_remote_with_git_only_for_differing_untracked_remote() -> None:
    assert not _should_update_untracked_remote_with_git(None, "def456")
    assert not _should_update_untracked_remote_with_git(
        RemoteBookmarkState(remote="origin", targets=("abc123",), tracking_targets=("abc123",)),
        "def456",
    )
    assert not _should_update_untracked_remote_with_git(
        RemoteBookmarkState(remote="origin", targets=("abc123",), tracking_targets=()),
        "abc123",
    )
    assert _should_update_untracked_remote_with_git(
        RemoteBookmarkState(remote="origin", targets=("abc123",), tracking_targets=()),
        "def456",
    )


def test_ensure_unique_bookmarks_rejects_duplicate_names() -> None:
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
        SubmitBookmarkCollisionError,
        match="multiple review units to the same bookmark",
    ):
        _ensure_unique_bookmarks(resolutions)
