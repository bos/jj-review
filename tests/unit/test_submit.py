from __future__ import annotations

from types import SimpleNamespace

import pytest

from jj_review.bookmarks import ResolvedBookmark
from jj_review.commands.submit import (
    SubmitBookmarkCollisionError,
    SubmitBookmarkConflictError,
    SubmitBookmarkResolutionError,
    SubmitGithubResolutionError,
    SubmitPullRequestResolutionError,
    SubmitRemoteBookmarkConflictError,
    SubmitRemoteBookmarkOwnershipError,
    SubmitRemoteResolutionError,
    _bookmark_linkage_is_proven,
    _discover_bookmarks_for_revisions,
    _ensure_pull_request_linkage_is_consistent,
    _ensure_remote_can_be_updated,
    _ensure_unique_bookmarks,
    _remote_is_up_to_date,
    _resolve_local_action,
    _should_update_untracked_remote_with_git,
    resolve_github_repository,
    resolve_trunk_branch,
    select_submit_remote,
)
from jj_review.config import RepoConfig
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubBranchRef, GithubPullRequest, GithubRepository


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


def test_resolve_github_repository_prefers_configured_values() -> None:
    repository = resolve_github_repository(
        RepoConfig(
            github_host="github.test",
            github_owner="octo-org",
            github_repo="stacked-review",
        ),
        GitRemote(name="origin", url="/tmp/remote.git"),
    )

    assert repository.host == "github.test"
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_resolve_github_repository_parses_https_remote_url() -> None:
    repository = resolve_github_repository(
        RepoConfig(),
        GitRemote(
            name="origin",
            url="https://github.test/octo-org/stacked-review.git",
        ),
    )

    assert repository.host == "github.test"
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_resolve_github_repository_rejects_unparseable_remote_without_config() -> None:
    with pytest.raises(
        SubmitGithubResolutionError,
        match="Could not determine the GitHub repository",
    ):
        resolve_github_repository(
            RepoConfig(),
            GitRemote(name="origin", url="/tmp/remote.git"),
        )


def test_resolve_trunk_branch_prefers_configured_branch() -> None:
    branch = resolve_trunk_branch(
        client=_FakeJjClient({}),
        config=RepoConfig(trunk_branch="release"),
        github_repository_state=_github_repository(default_branch="main"),
        remote=GitRemote(name="origin", url="git@example.com:org/repo.git"),
        stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk123")),
    )

    assert branch == "release"


def test_resolve_trunk_branch_uses_repository_default_branch() -> None:
    branch = resolve_trunk_branch(
        client=_FakeJjClient({}),
        config=RepoConfig(),
        github_repository_state=_github_repository(default_branch="main"),
        remote=GitRemote(name="origin", url="git@example.com:org/repo.git"),
        stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk123")),
    )

    assert branch == "main"


def test_resolve_trunk_branch_falls_back_to_unique_remote_bookmark() -> None:
    branch = resolve_trunk_branch(
        client=_FakeJjClient(
            {
                "main": BookmarkState(
                    name="main",
                    remote_targets=(RemoteBookmarkState(remote="origin", targets=("trunk123",)),),
                )
            }
        ),
        config=RepoConfig(),
        github_repository_state=_github_repository(default_branch=""),
        remote=GitRemote(name="origin", url="git@example.com:org/repo.git"),
        stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk123")),
    )

    assert branch == "main"


def test_resolve_trunk_branch_rejects_ambiguous_remote_bookmarks() -> None:
    with pytest.raises(
        SubmitGithubResolutionError,
        match="multiple remote bookmarks",
    ):
        resolve_trunk_branch(
            client=_FakeJjClient(
                {
                    "main": BookmarkState(
                        name="main",
                        remote_targets=(
                            RemoteBookmarkState(remote="origin", targets=("trunk123",)),
                        ),
                    ),
                    "stable": BookmarkState(
                        name="stable",
                        remote_targets=(
                            RemoteBookmarkState(remote="origin", targets=("trunk123",)),
                        ),
                    ),
                }
            ),
            config=RepoConfig(),
            github_repository_state=_github_repository(default_branch=""),
            remote=GitRemote(name="origin", url="git@example.com:org/repo.git"),
            stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk123")),
        )


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


def test_bookmark_linkage_is_proven_by_discovered_bookmark() -> None:
    assert _bookmark_linkage_is_proven(
        bookmark="review/foo",
        bookmark_source="discovered",
        bookmark_state=BookmarkState(name="review/foo"),
        change_id="change-a",
        state=ReviewState(),
    )


def test_bookmark_linkage_is_not_proven_by_newly_generated_name() -> None:
    assert not _bookmark_linkage_is_proven(
        bookmark="review/foo",
        bookmark_source="generated",
        bookmark_state=BookmarkState(name="review/foo"),
        change_id="change-a",
        state=ReviewState(change={"change-a": CachedChange(bookmark="review/foo")}),
    )


def test_discover_bookmarks_for_revisions_reuses_unique_matching_remote_bookmark() -> None:
    bookmarks = _discover_bookmarks_for_revisions(
        bookmark_states={
            "review/original-title-zvlywqkx": BookmarkState(
                name="review/original-title-zvlywqkx",
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("abc123",)),),
            ),
        },
        remote_name="origin",
        revisions=(
            SimpleNamespace(change_id="zvlywqkxtmnpqrstu"),
        ),
    )

    assert bookmarks == {"zvlywqkxtmnpqrstu": "review/original-title-zvlywqkx"}


def test_discover_bookmarks_for_revisions_rejects_ambiguous_matches() -> None:
    with pytest.raises(
        SubmitBookmarkResolutionError,
        match="multiple existing bookmarks match",
    ):
        _discover_bookmarks_for_revisions(
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
                SimpleNamespace(change_id="zvlywqkxtmnpqrstu"),
            ),
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


def test_pull_request_linkage_rejects_missing_discovered_pull_request() -> None:
    with pytest.raises(
        SubmitPullRequestResolutionError,
        match="Cached pull request linkage exists",
    ):
        _ensure_pull_request_linkage_is_consistent(
            bookmark="review/foo",
            cached_change=CachedChange(
                bookmark="review/foo",
                pr_number=17,
                pr_url="https://github.test/octo-org/repo/pull/17",
            ),
            discovered_pull_request=None,
        )


def test_pull_request_linkage_rejects_mismatched_pull_request_number() -> None:
    with pytest.raises(
        SubmitPullRequestResolutionError,
        match="Cached pull request #17 does not match",
    ):
        _ensure_pull_request_linkage_is_consistent(
            bookmark="review/foo",
            cached_change=CachedChange(bookmark="review/foo", pr_number=17),
            discovered_pull_request=_github_pull_request(number=21),
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


class _FakeJjClient:
    def __init__(self, states: dict[str, BookmarkState]) -> None:
        self._states = states

    def list_bookmark_states(self) -> dict[str, BookmarkState]:
        return self._states


def _github_pull_request(number: int) -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        body="",
        head=GithubBranchRef(ref="review/foo"),
        html_url=f"https://github.test/octo-org/repo/pull/{number}",
        number=number,
        state="open",
        title="feature",
    )


def _github_repository(default_branch: str) -> GithubRepository:
    return GithubRepository(
        clone_url="https://github.test/octo-org/repo.git",
        default_branch=default_branch,
        full_name="octo-org/repo",
        html_url="https://github.test/octo-org/repo",
        name="repo",
        private=True,
        url="https://api.github.test/repos/octo-org/repo",
    )
