from __future__ import annotations

import pytest

from jj_review.bookmarks import ResolvedBookmark
from jj_review.commands.submit import (
    SubmitBookmarkCollisionError,
    SubmitBookmarkConflictError,
    SubmitRemoteResolutionError,
    _ensure_unique_bookmarks,
    _remote_is_up_to_date,
    _resolve_local_action,
    select_submit_remote,
)
from jj_review.config import RepoConfig
from jj_review.models.bookmarks import GitRemote, RemoteBookmarkState


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
