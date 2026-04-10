from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from jj_review.commands.close import CloseAction, _cleanup_revision, _CloseCleanupContext
from jj_review.github.client import GithubClient
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_review.models.cache import CachedChange


@pytest.mark.parametrize(
    ("bookmark_state", "expected_action"),
    [
        pytest.param(
            BookmarkState(
                name="review/feature-aaaaaaaa",
                local_targets=("commit-1", "commit-2"),
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("commit-1",)),
                ),
            ),
            CloseAction(
                kind="local bookmark",
                message=(
                    "cannot forget local bookmark 'review/feature-aaaaaaaa' because it is "
                    "conflicted"
                ),
                status="blocked",
            ),
            id="conflicted-local",
        ),
        pytest.param(
            BookmarkState(
                name="review/feature-aaaaaaaa",
                local_targets=("other-commit",),
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("commit-1",)),
                ),
            ),
            CloseAction(
                kind="local bookmark",
                message=(
                    "cannot forget local bookmark 'review/feature-aaaaaaaa' because it "
                    "already points to a different revision"
                ),
                status="blocked",
            ),
            id="moved-local",
        ),
        pytest.param(
            BookmarkState(
                name="review/feature-aaaaaaaa",
                local_targets=("commit-1",),
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("commit-1", "commit-2")),
                ),
            ),
            CloseAction(
                kind="remote branch",
                message=(
                    "cannot delete remote branch review/feature-aaaaaaaa@origin because "
                    "the remote bookmark is conflicted"
                ),
                status="blocked",
            ),
            id="conflicted-remote",
        ),
        pytest.param(
            BookmarkState(
                name="review/feature-aaaaaaaa",
                local_targets=("commit-1",),
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("other-commit",)),
                ),
            ),
            CloseAction(
                kind="remote branch",
                message=(
                    "cannot delete remote branch review/feature-aaaaaaaa@origin because "
                    "it already points to a different revision"
                ),
                status="blocked",
            ),
            id="moved-remote",
        ),
    ],
)
def test_cleanup_revision_blocks_unsafe_bookmarks(
    bookmark_state: BookmarkState,
    expected_action: CloseAction,
) -> None:
    result = asyncio.run(
        _run_cleanup_revision(bookmark_state=bookmark_state)
    )

    assert result.actions == [expected_action]
    assert result.jj_client.delete_calls == []
    assert result.jj_client.forget_calls == []


class _CleanupResult:
    def __init__(self, actions: list[CloseAction], jj_client: _JjClientStub) -> None:
        self.actions = actions
        self.jj_client = jj_client


class _JjClientStub:
    def __init__(self) -> None:
        self.delete_calls: list[tuple[str, str, str]] = []
        self.forget_calls: list[str] = []

    def delete_remote_bookmark(
        self,
        *,
        remote: str,
        bookmark: str,
        expected_remote_target: str,
    ) -> None:
        self.delete_calls.append((remote, bookmark, expected_remote_target))

    def forget_bookmark(self, bookmark: str) -> None:
        self.forget_calls.append(bookmark)


async def _run_cleanup_revision(*, bookmark_state: BookmarkState) -> _CleanupResult:
    actions: list[CloseAction] = []
    jj_client = _JjClientStub()
    await _cleanup_revision(
        bookmark_state=bookmark_state,
        cached_change=CachedChange(bookmark="review/feature-aaaaaaaa"),
        commit_id="commit-1",
        context=_CloseCleanupContext(
            apply=True,
            github_client=cast(GithubClient, SimpleNamespace()),
            github_repository=SimpleNamespace(owner="octo-org", repo="stacked-review"),
            jj_client=cast(JjClient, jj_client),
            next_changes={},
            record_action=actions.append,
            remote_name="origin",
            revision=SimpleNamespace(change_id="aaaaaaaaaaaaaaaa"),
            revision_label="feature 1 [aaaaaaaa]",
        ),
    )
    return _CleanupResult(actions=actions, jj_client=jj_client)
