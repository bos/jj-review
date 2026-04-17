from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import cast

from jj_review.config import RepoConfig
from jj_review.errors import CliError, ErrorMessage
from jj_review.github.client import GithubClientError
from jj_review.github.error_messages import (
    summarize_github_lookup_error,
    summarize_github_repository_error,
)
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
)
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import GithubBranchRef, GithubPullRequest
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.status import (
    PreparedStack,
    PreparedStatus,
    PullRequestLookup,
    ReviewStatusRevision,
    _classify_status_intents,
    _status_is_incomplete,
    stream_status_async,
)


def test_stream_status_streams_local_fallback_revisions_after_github_abort(
    monkeypatch,
) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        outstanding_intents=(),
        prepared=cast(
            PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(
                    SimpleNamespace(
                        cached_change=CachedChange(pr_number=1),
                        change_id="aaaaaaaaaaaa",
                    ),
                ),
            ),
        ),
        selected_revset="@",
        stale_intents=(),
        base_parent_subject="base",
    )
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            commit_id="commit-1",
            link_state="active",
            local_divergent=False,
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
            commit_id="commit-2",
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            remote_state=None,
            stack_comment_lookup=None,
            subject="feature 2",
        ),
    )
    github_status_calls: list[tuple[str | None, ErrorMessage | None]] = []
    streamed_revisions: list[tuple[str, bool]] = []

    async def fake_iter_status_revisions_with_github(**kwargs):
        if False:
            yield None
        raise CliError("jj bookmark list failed")

    monkeypatch.setattr(
        "jj_review.review.status._iter_status_revisions_with_github",
        fake_iter_status_revisions_with_github,
    )
    monkeypatch.setattr(
        "jj_review.review.status._build_status_revisions_without_github",
        lambda prepared: local_only_revisions,
    )

    def on_github_status(
        github_repository: str | None,
        github_error: ErrorMessage | None,
    ) -> None:
        github_status_calls.append((github_repository, github_error))

    result = asyncio.run(
        stream_status_async(
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


def test_resolve_status_github_repository_returns_resolution_error() -> None:
    assert (
        parse_github_repo(
            GitRemote(name="origin", url="ssh://example.com/not-github.git"),
        )
        is None
    )


def test_classify_status_intents_separates_stale_intents_from_live_ones(
    monkeypatch,
) -> None:
    fresh = cast(object, SimpleNamespace(intent=SimpleNamespace(label="fresh")))
    stale = cast(object, SimpleNamespace(intent=SimpleNamespace(label="stale")))
    prepared = cast(
        PreparedStack,
        SimpleNamespace(
            client=object(),
            state_store=SimpleNamespace(list_intents=lambda: [fresh, stale]),
        ),
    )
    monkeypatch.setattr(
        "jj_review.review.status.intent_is_stale",
        lambda intent, resolver, now: intent.label == "stale",
    )

    outstanding, stale_intents = _classify_status_intents(prepared)

    assert outstanding == (fresh,)
    assert stale_intents == (stale,)


def test_stream_status_reports_uninspected_github_target_for_empty_stack() -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        outstanding_intents=(),
        prepared=cast(
            PreparedStack,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(),
            ),
        ),
        selected_revset="main",
        stale_intents=(),
        base_parent_subject="base",
    )
    github_status_calls: list[tuple[str | None, ErrorMessage | None]] = []

    result = asyncio.run(
        stream_status_async(
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


def test_stream_status_skips_github_discovery_for_untracked_stack(monkeypatch) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            commit_id="commit-1",
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            remote_state=None,
            stack_comment_lookup=None,
            subject="feature 1",
        ),
    )
    prepared_status = PreparedStatus(
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        outstanding_intents=(),
        prepared=cast(
            PreparedStack,
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
                            commit_id="commit-1",
                            subject="feature 1",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        stale_intents=(),
        base_parent_subject="base",
    )
    monkeypatch.setattr(
        "jj_review.review.status._build_status_revisions_without_github",
        lambda prepared: local_only_revisions,
    )

    async def fail_iter_status_revisions_with_github(**kwargs):
        if False:
            yield None
        raise AssertionError("unexpected GitHub inspection for untracked stack")

    monkeypatch.setattr(
        "jj_review.review.status._iter_status_revisions_with_github",
        fail_iter_status_revisions_with_github,
    )

    result = asyncio.run(
        stream_status_async(
            on_github_status=None,
            on_revision=None,
            prepared_status=prepared_status,
        )
    )

    assert result.github_error is None
    assert result.github_repository == "octo-org/stacked-review"
    assert result.incomplete is False
    assert result.revisions == local_only_revisions


def test_summarize_github_repository_error_detects_graphql_repo_not_found(
    monkeypatch,
) -> None:
    monkeypatch.setattr("jj_review.github.error_messages.github_token_from_env", lambda: None)

    error = GithubClientError(
        "GitHub pull request head lookup failed: "
        "[{'type': 'NOT_FOUND', 'message': "
        "\"Could not resolve to a Repository with the name 'voxel-ai/voxel'.\"}]"
    )

    assert (
        summarize_github_repository_error(error)
        == "repo not found or inaccessible - check GITHUB_TOKEN or gh auth"
    )


def test_summarize_github_lookup_error_preserves_transport_detail() -> None:
    error = GithubClientError("GitHub request failed: Connection refused")

    assert (
        summarize_github_lookup_error(action="pull request lookup", error=error)
        == "pull request lookup failed (Connection refused)"
    )


def test_status_does_not_mark_merged_pr_needing_cleanup_as_incomplete() -> None:
    revision = ReviewStatusRevision(
        bookmark="review/feature-1-aaaaaaaa",
        bookmark_source="generated",
        cached_change=None,
        change_id="aaaaaaaaaaaa",
        commit_id="commit-1",
        link_state="active",
        local_divergent=True,
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=GithubPullRequest(
                base=GithubBranchRef(ref="main"),
                head=GithubBranchRef(ref="review/feature-1-aaaaaaaa"),
                html_url="https://github.test/octo-org/stacked-review/pull/1",
                number=1,
                state="merged",
                title="feature 1",
            ),
            repository_error=None,
            review_decision=None,
            review_decision_error=None,
            state="closed",
        ),
        remote_state=None,
        stack_comment_lookup=None,
        subject="feature 1",
    )

    assert _status_is_incomplete((revision,)) is False


def test_status_marks_non_merged_divergent_revision_incomplete() -> None:
    revision = ReviewStatusRevision(
        bookmark="review/feature-1-aaaaaaaa",
        bookmark_source="generated",
        cached_change=None,
        change_id="aaaaaaaaaaaa",
        commit_id="commit-1",
        link_state="active",
        local_divergent=True,
        pull_request_lookup=PullRequestLookup(
            message=None,
            pull_request=GithubPullRequest(
                base=GithubBranchRef(ref="main"),
                head=GithubBranchRef(ref="review/feature-1-aaaaaaaa"),
                html_url="https://github.test/octo-org/stacked-review/pull/1",
                number=1,
                state="open",
                title="feature 1",
            ),
            repository_error=None,
            review_decision=None,
            review_decision_error=None,
            state="open",
        ),
        remote_state=None,
        stack_comment_lookup=None,
        subject="feature 1",
    )

    assert _status_is_incomplete((revision,)) is True


def test_stream_status_marks_missing_remote_as_incomplete() -> None:
    prepared_status = PreparedStatus(
        github_repository=None,
        github_repository_error=None,
        outstanding_intents=(),
        prepared=cast(
            PreparedStack,
            SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda bookmarks=None: {}),
                remote=None,
                remote_error="no git remote configured",
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/feature-1-aaaaaaaa",
                        bookmark_source="generated",
                        cached_change=None,
                        revision=SimpleNamespace(
                            change_id="aaaaaaaaaaaa",
                            commit_id="commit-1",
                            divergent=False,
                            subject="feature 1",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        stale_intents=(),
        base_parent_subject="base",
    )
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            commit_id="commit-1",
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            remote_state=None,
            stack_comment_lookup=None,
            subject="feature 1",
        ),
    )

    result = asyncio.run(
        stream_status_async(
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
        outstanding_intents=(),
        prepared=cast(
            PreparedStack,
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
                            commit_id="commit-1",
                            subject="feature 1",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        stale_intents=(),
        base_parent_subject="base",
    )
    local_only_revisions = (
        ReviewStatusRevision(
            bookmark="review/feature-1-aaaaaaaa",
            bookmark_source="generated",
            cached_change=None,
            change_id="aaaaaaaaaaaa",
            commit_id="commit-1",
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            remote_state=None,
            stack_comment_lookup=None,
            subject="feature 1",
        ),
    )
    monkeypatch.setattr(
        "jj_review.review.status._build_status_revisions_without_github",
        lambda prepared: local_only_revisions,
    )

    result = asyncio.run(
        stream_status_async(
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


def test_prepare_status_fetches_before_remote_bookmark_discovery(
    tmp_path,
    monkeypatch,
) -> None:
    revision = LocalRevision(
        change_id="aaaaaaaa1234",
        commit_id="commit-1",
        current_working_copy=False,
        description="feature 1",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("trunk-commit",),
    )
    trunk = LocalRevision(
        change_id="trunkchangeid",
        commit_id="trunk-commit",
        current_working_copy=False,
        description="base",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=True,
        parents=("root",),
    )
    stack = LocalStack(
        base_parent=trunk,
        head=revision,
        revisions=(revision,),
        selected_revset="@",
        trunk=trunk,
    )
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    discovered_bookmark = "review/manual-feature-aaaaaaaa"

    class FakeClient:
        def __init__(self) -> None:
            self.fetched = False

        def discover_review_stack(
            self,
            revset,
            *,
            allow_divergent=False,
            allow_immutable=False,
        ):
            assert revset is None
            assert allow_divergent is True
            assert allow_immutable is True
            return stack

        def list_git_remotes(self):
            return (remote,)

        def fetch_remote(self, *, remote: str) -> None:
            assert remote == "origin"
            self.fetched = True

        def list_bookmark_states(self, bookmarks=None):
            if not self.fetched:
                return {}
            return {
                discovered_bookmark: BookmarkState(
                    name=discovered_bookmark,
                    remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
                )
            }

    class FakeStateStore:
        def __init__(self) -> None:
            self.state = ReviewState()

        def load(self) -> ReviewState:
            return self.state

        def save(self, state: ReviewState) -> None:
            self.state = state

        def list_intents(self) -> list[object]:
            return []

    def build_status(*, fetch_remote_state: bool):
        client = FakeClient()
        state_store = FakeStateStore()
        monkeypatch.setattr("jj_review.review.status.JjClient", lambda _: client)
        monkeypatch.setattr(
            "jj_review.review.status.ReviewStateStore.for_repo",
            lambda _: state_store,
        )
        return _prepare_status_for_test(
            config=RepoConfig(),
            fetch_remote_state=fetch_remote_state,
            repo_root=tmp_path,
        )

    prepared_without_fetch = build_status(fetch_remote_state=False)
    prepared_with_fetch = build_status(fetch_remote_state=True)

    assert (
        prepared_without_fetch.prepared.status_revisions[0].bookmark
        == "review/feature-1-aaaaaaaa"
    )
    assert prepared_without_fetch.prepared.status_revisions[0].bookmark_source == "generated"
    assert prepared_with_fetch.prepared.status_revisions[0].bookmark == discovered_bookmark
    assert prepared_with_fetch.prepared.status_revisions[0].bookmark_source == "discovered"


def _prepare_status_for_test(
    *,
    config: RepoConfig,
    fetch_remote_state: bool,
    repo_root,
) -> PreparedStatus:
    from jj_review.review.status import prepare_status

    return prepare_status(
        change_overrides={},
        config=config,
        fetch_remote_state=fetch_remote_state,
        repo_root=repo_root,
        revset=None,
    )
