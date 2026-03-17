from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

from jj_review.commands.review_state import (
    PreparedStatus,
    ReviewStatusRevision,
    _PreparedStack,
    _stream_status_async,
)
from jj_review.commands.submit import ResolvedGithubRepository
from jj_review.errors import CliError
from jj_review.models.bookmarks import GitRemote


def test_stream_status_streams_local_fallback_revisions_after_github_abort(
    monkeypatch,
) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ResolvedGithubRepository(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        prepared=cast(
            _PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(SimpleNamespace(change_id="aaaaaaaaaaaa"),),
            ),
        ),
        selected_revset="@",
        trunk_subject="base",
    )
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            pull_request_lookup=None,
            remote_state=None,
            stack_comment_lookup=None,
            subject="feature 1",
        ),
        ReviewStatusRevision(
            bookmark="review/feature-2-bbbbbbbb",
            bookmark_source="generated",
            cached_change=None,
            change_id="bbbbbbbbbbbb",
            pull_request_lookup=None,
            remote_state=None,
            stack_comment_lookup=None,
            subject="feature 2",
        ),
    )
    github_status_calls: list[tuple[str | None, str | None]] = []
    streamed_revisions: list[tuple[str, bool]] = []

    async def fake_iter_status_revisions_with_github(**kwargs):
        if False:
            yield None
        raise CliError("jj bookmark list failed")

    monkeypatch.setattr(
        "jj_review.commands.review_state._iter_status_revisions_with_github",
        fake_iter_status_revisions_with_github,
    )
    monkeypatch.setattr(
        "jj_review.commands.review_state._build_status_revisions_without_github",
        lambda prepared: local_only_revisions,
    )

    def on_github_status(
        github_repository: str | None,
        github_error: str | None,
    ) -> None:
        github_status_calls.append((github_repository, github_error))

    result = asyncio.run(
        _stream_status_async(
            on_github_status=on_github_status,
            on_revision=lambda revision, github_available: streamed_revisions.append(
                (revision.change_id, github_available)
            ),
            prepared_status=prepared_status,
        )
    )

    assert github_status_calls == [("octo-org/stacked-review", None)]
    assert streamed_revisions == [
        ("bbbbbbbbbbbb", False),
        ("aaaaaaaaaaaa", False),
    ]
    assert result.github_error == "jj bookmark list failed"
    assert result.github_repository == "octo-org/stacked-review"
    assert result.incomplete is True
    assert result.revisions == (
        local_only_revisions[1],
        local_only_revisions[0],
    )


def test_stream_status_reports_uninspected_github_target_for_empty_stack() -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ResolvedGithubRepository(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        prepared=cast(
            _PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(),
            ),
        ),
        selected_revset="main",
        trunk_subject="base",
    )
    github_status_calls: list[tuple[str | None, str | None]] = []

    result = asyncio.run(
        _stream_status_async(
            on_github_status=lambda github_repository, github_error: github_status_calls.append(
                (github_repository, github_error)
            ),
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert github_status_calls == [
        ("octo-org/stacked-review", "not inspected; no reviewable commits")
    ]
    assert result.github_error is None
    assert result.github_repository == "octo-org/stacked-review"
    assert result.incomplete is False
    assert result.revisions == ()


def test_stream_status_marks_missing_remote_as_incomplete() -> None:
    prepared_status = PreparedStatus(
        github_repository=None,
        github_repository_error=None,
        prepared=cast(
            _PreparedStack,
            SimpleNamespace(
                remote=None,
                remote_error="no git remote configured",
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/feature-1-aaaaaaaa",
                        bookmark_source="generated",
                        cached_change=None,
                        revision=SimpleNamespace(
                            change_id="aaaaaaaaaaaa",
                            subject="feature 1",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        trunk_subject="base",
    )
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            pull_request_lookup=None,
            remote_state=None,
            stack_comment_lookup=None,
            subject="feature 1",
        ),
    )

    result = asyncio.run(
        _stream_status_async(
            on_github_status=None,
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert result.github_error is None
    assert result.github_repository is None
    assert result.incomplete is True
    assert result.remote_error == "no git remote configured"
    assert result.revisions == local_only_revisions


def test_stream_status_marks_missing_github_target_as_incomplete(monkeypatch) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=None,
        github_repository_error="repo not found or inaccessible",
        prepared=cast(
            _PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/feature-1-aaaaaaaa",
                        bookmark_source="generated",
                        cached_change=None,
                        revision=SimpleNamespace(
                            change_id="aaaaaaaaaaaa",
                            subject="feature 1",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        trunk_subject="base",
    )
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            pull_request_lookup=None,
            remote_state=None,
            stack_comment_lookup=None,
            subject="feature 1",
        ),
    )
    monkeypatch.setattr(
        "jj_review.commands.review_state._build_status_revisions_without_github",
        lambda prepared: local_only_revisions,
    )

    result = asyncio.run(
        _stream_status_async(
            on_github_status=None,
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert result.github_error == "repo not found or inaccessible"
    assert result.github_repository is None
    assert result.incomplete is True
    assert result.remote == remote
    assert result.revisions == local_only_revisions
