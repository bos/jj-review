from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from jj_review.commands.cleanup import (
    PreparedCleanup,
    PreparedRestack,
    _inspect_restack,
    _inspect_restack_pull_request,
    _should_inspect_stack_comment_cleanup,
    _stream_cleanup_async,
    prepare_restack,
    stream_restack,
)
from jj_review.commands.review_state import PreparedStatus
from jj_review.commands.submit import ResolvedGithubRepository
from jj_review.config import RepoConfig
from jj_review.github.client import GithubClientError
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubBranchRef, GithubPullRequest


def test_should_skip_stack_comment_inspection_for_stale_open_change_without_comment_hint() -> (
    None
):
    bookmark_state = BookmarkState(
        name="review/feature-aaaaaaaa",
        remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
    )

    should_inspect = _should_inspect_stack_comment_cleanup(
        bookmark_state=bookmark_state,
        cached_change=CachedChange(
            bookmark="review/feature-aaaaaaaa",
            pr_number=7,
            pr_state="open",
            stack_comment_id=None,
        ),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        stale_reason="local change is no longer reviewable",
    )

    assert should_inspect is False


def test_should_inspect_stack_comment_for_stale_change_with_cached_comment_id() -> None:
    should_inspect = _should_inspect_stack_comment_cleanup(
        bookmark_state=BookmarkState(name="review/feature-aaaaaaaa"),
        cached_change=CachedChange(
            bookmark="review/feature-aaaaaaaa",
            pr_number=7,
            pr_state="open",
            stack_comment_id=12,
        ),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        stale_reason="local change is no longer reviewable",
    )

    assert should_inspect is True


def test_should_inspect_stack_comment_for_stale_change_with_missing_remote_branch() -> None:
    should_inspect = _should_inspect_stack_comment_cleanup(
        bookmark_state=BookmarkState(name="review/feature-aaaaaaaa"),
        cached_change=CachedChange(
            bookmark="review/feature-aaaaaaaa",
            pr_number=7,
            pr_state="open",
            stack_comment_id=None,
        ),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        stale_reason="local change is no longer reviewable",
    )

    assert should_inspect is True


def test_stream_cleanup_limits_stack_comment_github_inspection_concurrency(
    monkeypatch,
) -> None:
    state_changes = {
        f"change-{index}": CachedChange(
            bookmark=f"review/feature-{index}",
            pr_number=index,
            pr_state="open",
        ).model_dump(exclude_none=True)
        for index in range(6)
    }
    state = ReviewState.model_validate(
        {
            "change": state_changes,
        }
    )
    prepared_cleanup = PreparedCleanup(
        apply=False,
        bookmark_states={},
        github_repository=ResolvedGithubRepository(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        jj_client=cast(Any, SimpleNamespace()),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        state=state,
        state_store=cast(Any, SimpleNamespace(save=lambda state: None)),
    )
    active = 0
    max_active = 0

    class FakeGithubClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fake_plan_stack_comment_cleanup(**kwargs):
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return None

    monkeypatch.setattr(
        "jj_review.commands.cleanup._build_github_client",
        lambda **kwargs: FakeGithubClientContext(),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reason",
        lambda **kwargs: "local change is no longer reviewable",
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._plan_stack_comment_cleanup",
        fake_plan_stack_comment_cleanup,
    )

    result = asyncio.run(
        _stream_cleanup_async(
            on_action=None,
            prepared_cleanup=prepared_cleanup,
        )
    )

    assert len(result.actions) == 6
    assert 1 < max_active <= 4


def test_stream_cleanup_emits_cache_actions_before_waiting_for_comment_inspection(
    monkeypatch,
) -> None:
    state = ReviewState.model_validate(
        {
            "change": {
                "change-1": CachedChange(
                    bookmark="review/feature-1",
                    pr_number=1,
                    pr_state="open",
                ).model_dump(exclude_none=True),
                "change-2": CachedChange(
                    bookmark="review/feature-2",
                    pr_number=2,
                    pr_state="open",
                ).model_dump(exclude_none=True),
            }
        }
    )
    prepared_cleanup = PreparedCleanup(
        apply=False,
        bookmark_states={},
        github_repository=ResolvedGithubRepository(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        jj_client=cast(Any, SimpleNamespace()),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        state=state,
        state_store=cast(Any, SimpleNamespace(save=lambda state: None)),
    )
    streamed_actions: list[str] = []
    release_comment_checks = asyncio.Event()

    class FakeGithubClientContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fake_plan_stack_comment_cleanup(**kwargs):
        await release_comment_checks.wait()
        return None

    async def exercise_cleanup() -> None:
        task = asyncio.create_task(
            _stream_cleanup_async(
                on_action=lambda action: streamed_actions.append(action.message),
                prepared_cleanup=prepared_cleanup,
            )
        )
        for _ in range(5):
            if len(streamed_actions) == 2:
                break
            await asyncio.sleep(0)

        assert streamed_actions == [
            "remove cached review state for change-1 (local change is no longer reviewable)",
            "remove cached review state for change-2 (local change is no longer reviewable)",
        ]
        release_comment_checks.set()
        await task

    monkeypatch.setattr(
        "jj_review.commands.cleanup._build_github_client",
        lambda **kwargs: FakeGithubClientContext(),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reason",
        lambda **kwargs: "local change is no longer reviewable",
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._plan_stack_comment_cleanup",
        fake_plan_stack_comment_cleanup,
    )

    asyncio.run(exercise_cleanup())


def test_stream_restack_plans_rebase_for_survivor_above_merged_path_revision(
    monkeypatch,
) -> None:
    inspect_calls: list[PreparedRestack] = []
    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="merged-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=1,
                state="merged",
            ),
            state="closed",
        ),
        subject="merged feature",
    )
    survivor_revision = SimpleNamespace(
        change_id="survivor-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=2,
                state="open",
            ),
            state="open",
        ),
        subject="survivor feature",
    )
    prepared_restack = PreparedRestack(
        apply=False,
        allow_nontrunk_rebase=False,
        prepared_status=cast(
            Any,
            SimpleNamespace(
                prepared=SimpleNamespace(
                    client=SimpleNamespace(),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            )
                        ),
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="survivor-change",
                                commit_id="survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            )
                        ),
                    ),
                ),
            ),
        ),
    )

    async def fake_inspect_restack(*, prepared_restack):
        inspect_calls.append(prepared_restack)
        return SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(merged_revision, survivor_revision),
            selected_revset="@",
        )

    monkeypatch.setattr("jj_review.commands.cleanup._inspect_restack", fake_inspect_restack)

    result = stream_restack(prepared_restack=prepared_restack)

    assert inspect_calls == [prepared_restack]
    assert result.blocked is False
    assert len(result.actions) == 1
    assert result.actions[0].kind == "restack"
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "planned"


def test_stream_restack_applies_rebase_for_survivor_above_merged_path_revision(
    monkeypatch,
) -> None:
    rebase_calls: list[tuple[str, str]] = []
    inspect_calls: list[PreparedRestack] = []

    class FakeClient:
        def resolve_revision(self, revset: str):
            if revset == "survivor-change":
                return SimpleNamespace(only_parent_commit_id=lambda: "merged-commit")
            raise AssertionError(f"unexpected revset: {revset}")

        def rebase_revision(self, *, source: str, destination: str) -> None:
            rebase_calls.append((source, destination))

    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="merged-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=1,
                state="merged",
            ),
            state="closed",
        ),
        subject="merged feature",
    )
    survivor_revision = SimpleNamespace(
        change_id="survivor-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=2,
                state="open",
            ),
            state="open",
        ),
        subject="survivor feature",
    )
    prepared_restack = PreparedRestack(
        apply=True,
        allow_nontrunk_rebase=False,
        prepared_status=cast(
            Any,
            SimpleNamespace(
                prepared=SimpleNamespace(
                    client=FakeClient(),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            )
                        ),
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="survivor-change",
                                commit_id="survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            )
                        ),
                    ),
                ),
            ),
        ),
    )

    async def fake_inspect_restack(*, prepared_restack):
        inspect_calls.append(prepared_restack)
        return SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(merged_revision, survivor_revision),
            selected_revset="@",
        )

    monkeypatch.setattr("jj_review.commands.cleanup._inspect_restack", fake_inspect_restack)

    result = stream_restack(prepared_restack=prepared_restack)

    assert inspect_calls == [prepared_restack]
    assert result.blocked is False
    assert rebase_calls == [("survivor-change", "trunk-commit")]
    assert len(result.actions) == 1
    assert result.actions[0].kind == "restack"
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "applied"


def test_stream_restack_blocks_when_merged_path_change_has_unpublished_edits(
    monkeypatch,
) -> None:
    merged_revision = SimpleNamespace(
        cached_change=CachedChange(last_submitted_commit_id="old-commit"),
        change_id="merged-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=1,
                state="merged",
            ),
            state="closed",
        ),
        subject="merged feature",
    )
    survivor_revision = SimpleNamespace(
        change_id="survivor-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=2,
                state="open",
            ),
            state="open",
        ),
        subject="survivor feature",
    )
    prepared_restack = PreparedRestack(
        apply=False,
        allow_nontrunk_rebase=False,
        prepared_status=cast(
            Any,
            SimpleNamespace(
                prepared=SimpleNamespace(
                    client=SimpleNamespace(),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="new-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            )
                        ),
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="survivor-change",
                                commit_id="survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            )
                        ),
                    ),
                ),
            ),
        ),
    )

    async def fake_inspect_restack(*, prepared_restack):
        return SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(merged_revision, survivor_revision),
            selected_revset="@",
        )

    monkeypatch.setattr("jj_review.commands.cleanup._inspect_restack", fake_inspect_restack)

    result = stream_restack(prepared_restack=prepared_restack)

    assert result.blocked is True
    assert result.requires_nontrunk_rebase is False
    assert len(result.actions) == 1
    assert result.actions[0].kind == "restack"
    assert "local edits since last submit" in result.actions[0].message
    assert "merged feature" in result.actions[0].message
    assert result.actions[0].status == "blocked"


def test_stream_restack_does_not_block_when_merged_change_commit_matches_last_submitted(
    monkeypatch,
) -> None:
    merged_revision = SimpleNamespace(
        cached_change=CachedChange(last_submitted_commit_id="merged-commit"),
        change_id="merged-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=1,
                state="merged",
            ),
            state="closed",
        ),
        subject="merged feature",
    )
    survivor_revision = SimpleNamespace(
        change_id="survivor-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=2,
                state="open",
            ),
            state="open",
        ),
        subject="survivor feature",
    )
    prepared_restack = PreparedRestack(
        apply=False,
        allow_nontrunk_rebase=False,
        prepared_status=cast(
            Any,
            SimpleNamespace(
                prepared=SimpleNamespace(
                    client=SimpleNamespace(),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            )
                        ),
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="survivor-change",
                                commit_id="survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            )
                        ),
                    ),
                ),
            ),
        ),
    )

    async def fake_inspect_restack(*, prepared_restack):
        return SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(merged_revision, survivor_revision),
            selected_revset="@",
        )

    monkeypatch.setattr("jj_review.commands.cleanup._inspect_restack", fake_inspect_restack)

    result = stream_restack(prepared_restack=prepared_restack)

    assert result.blocked is False
    assert len(result.actions) == 1
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "planned"


def test_prepare_restack_skips_fetch_remote_state(monkeypatch) -> None:
    prepare_calls: list[dict[str, object]] = []

    def fake_prepare_status(**kwargs):
        prepare_calls.append(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("jj_review.commands.cleanup.prepare_status", fake_prepare_status)

    result = prepare_restack(
        apply=False,
        allow_nontrunk_rebase=False,
        change_overrides={},
        config=RepoConfig(),
        repo_root=cast(Any, "/repo"),
        revset="@-",
    )

    assert result.apply is False
    assert result.allow_nontrunk_rebase is False
    assert prepare_calls == [
        {
            "change_overrides": {},
            "config": RepoConfig(),
            "fetch_remote_state": False,
            "repo_root": "/repo",
            "revset": "@-",
        }
    ]


def test_stream_restack_blocks_nontrunk_rebase_without_override(
    monkeypatch,
) -> None:
    inspect_calls: list[PreparedRestack] = []
    open_base_revision = SimpleNamespace(
        change_id="open-base",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=1,
                state="open",
            ),
            state="open",
        ),
        subject="open base",
    )
    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="merged-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="review/open-base"),
                number=2,
                state="merged",
            ),
            state="closed",
        ),
        subject="merged middle",
    )
    top_revision = SimpleNamespace(
        change_id="top-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="review/merged-middle"),
                number=3,
                state="open",
            ),
            state="open",
        ),
        subject="top survivor",
    )
    prepared_restack = PreparedRestack(
        apply=False,
        allow_nontrunk_rebase=False,
        prepared_status=cast(
            Any,
            SimpleNamespace(
                prepared=SimpleNamespace(
                    client=SimpleNamespace(),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="open-base",
                                commit_id="open-base-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            )
                        ),
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "open-base-commit",
                            )
                        ),
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="top-change",
                                commit_id="top-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            )
                        ),
                    ),
                ),
            ),
        ),
    )

    async def fake_inspect_restack(*, prepared_restack):
        inspect_calls.append(prepared_restack)
        return SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(open_base_revision, merged_revision, top_revision),
            selected_revset="@",
        )

    monkeypatch.setattr("jj_review.commands.cleanup._inspect_restack", fake_inspect_restack)

    result = stream_restack(prepared_restack=prepared_restack)

    assert inspect_calls == [prepared_restack]
    assert result.blocked is True
    assert result.requires_nontrunk_rebase is True
    assert len(result.actions) == 2
    assert result.actions[0].message == (
        "rebase top-chan onto open-bas requires --allow-nontrunk-rebase"
    )
    assert result.actions[0].status == "blocked"
    assert result.actions[1].message == (
        "PR #2 merged into review branch review/open-base; configure GitHub to block "
        "merges of PRs targeting `review/*`"
    )


def test_stream_restack_applies_nontrunk_rebase_with_override(
    monkeypatch,
) -> None:
    rebase_calls: list[tuple[str, str]] = []
    inspect_calls: list[PreparedRestack] = []

    class FakeClient:
        def resolve_revision(self, revset: str):
            parents = {
                "open-base": "trunk-commit",
                "top-change": "merged-commit",
            }
            commit_ids = {
                "open-base": "open-base-commit",
                "top-change": "top-commit",
            }
            if revset not in commit_ids:
                raise AssertionError(f"unexpected revset: {revset}")
            return SimpleNamespace(
                commit_id=commit_ids[revset],
                only_parent_commit_id=lambda: parents[revset],
            )

        def rebase_revision(self, *, source: str, destination: str) -> None:
            rebase_calls.append((source, destination))

    open_base_revision = SimpleNamespace(
        change_id="open-base",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=1,
                state="open",
            ),
            state="open",
        ),
        subject="open base",
    )
    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="merged-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="review/open-base"),
                number=2,
                state="merged",
            ),
            state="closed",
        ),
        subject="merged middle",
    )
    top_revision = SimpleNamespace(
        change_id="top-change",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="review/merged-middle"),
                number=3,
                state="open",
            ),
            state="open",
        ),
        subject="top survivor",
    )
    prepared_restack = PreparedRestack(
        apply=True,
        allow_nontrunk_rebase=True,
        prepared_status=cast(
            Any,
            SimpleNamespace(
                prepared=SimpleNamespace(
                    client=FakeClient(),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="open-base",
                                commit_id="open-base-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            )
                        ),
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "open-base-commit",
                            )
                        ),
                        SimpleNamespace(
                            revision=SimpleNamespace(
                                change_id="top-change",
                                commit_id="top-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            )
                        ),
                    ),
                ),
            ),
        ),
    )

    async def fake_inspect_restack(*, prepared_restack):
        inspect_calls.append(prepared_restack)
        return SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(open_base_revision, merged_revision, top_revision),
            selected_revset="@",
        )

    monkeypatch.setattr("jj_review.commands.cleanup._inspect_restack", fake_inspect_restack)

    result = stream_restack(prepared_restack=prepared_restack)

    assert inspect_calls == [prepared_restack]
    assert result.blocked is False
    assert result.requires_nontrunk_rebase is False
    assert rebase_calls == [("top-change", "open-base-commit")]
    assert len(result.actions) == 2
    assert result.actions[0].message == "rebase top-chan onto open-bas"
    assert result.actions[0].status == "applied"
    assert result.actions[1].message == (
        "PR #2 merged into review branch review/open-base; configure GitHub to block "
        "merges of PRs targeting `review/*`"
    )


def test_inspect_restack_pull_request_uses_cached_pull_request_before_head_lookup() -> None:
    class FakeGithubClient:
        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
        ):
            raise AssertionError("head lookup should not run when cached pull request matches")

    lookup = asyncio.run(
        _inspect_restack_pull_request(
            bookmark="review/feature-aaaaaaaa",
            cached_change=CachedChange(
                bookmark="review/feature-aaaaaaaa",
                pr_number=7,
            ),
            cached_pull_requests={
                7: GithubPullRequest(
                    base=GithubBranchRef(ref="review/base-branch"),
                    head=GithubBranchRef(ref="review/feature-aaaaaaaa"),
                    html_url="https://github.test/octo-org/stacked-review/pull/7",
                    merged_at="2026-03-16T12:00:00Z",
                    number=7,
                    state="closed",
                    title="feature 1",
                )
            },
            github_client=cast(Any, FakeGithubClient()),
            github_repository=ResolvedGithubRepository(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
            pull_requests_by_head_ref={},
        )
    )

    assert lookup.state == "closed"
    assert lookup.pull_request is not None
    assert lookup.pull_request.state == "merged"


def test_inspect_restack_falls_back_to_head_lookup_when_cached_pull_request_mismatches() -> None:
    list_calls: list[str] = []

    class FakeGithubClient:
        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
        ):
            assert owner == "octo-org"
            assert repo == "stacked-review"
            assert state == "all"
            list_calls.append(head)
            return (
                GithubPullRequest(
                    base=GithubBranchRef(ref="main"),
                    head=GithubBranchRef(ref="review/feature-aaaaaaaa"),
                    html_url="https://github.test/octo-org/stacked-review/pull/7",
                    number=7,
                    state="open",
                    title="feature 1",
                ),
            )

    lookup = asyncio.run(
        _inspect_restack_pull_request(
            bookmark="review/feature-aaaaaaaa",
            cached_change=CachedChange(
                bookmark="review/feature-aaaaaaaa",
                pr_number=7,
            ),
            cached_pull_requests={
                7: GithubPullRequest(
                    base=GithubBranchRef(ref="main"),
                    head=GithubBranchRef(ref="review/other-branch"),
                    html_url="https://github.test/octo-org/stacked-review/pull/7",
                    number=7,
                    state="open",
                    title="feature 1",
                )
            },
            github_client=cast(Any, FakeGithubClient()),
            github_repository=ResolvedGithubRepository(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
            pull_requests_by_head_ref={},
        )
    )

    assert list_calls == ["octo-org:review/feature-aaaaaaaa"]
    assert lookup.state == "open"
    assert lookup.pull_request is not None
    assert lookup.pull_request.number == 7


def test_inspect_restack_pull_request_uses_batched_head_lookup_before_rest_lookup() -> None:
    class FakeGithubClient:
        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
        ):
            raise AssertionError("REST head lookup should not run when head batch matches")

    lookup = asyncio.run(
        _inspect_restack_pull_request(
            bookmark="review/feature-aaaaaaaa",
            cached_change=None,
            cached_pull_requests={},
            github_client=cast(Any, FakeGithubClient()),
            github_repository=ResolvedGithubRepository(
                host="github.com",
                owner="octo-org",
                repo="stacked-review",
            ),
            pull_requests_by_head_ref={
                "review/feature-aaaaaaaa": (
                    GithubPullRequest(
                        base=GithubBranchRef(ref="main"),
                        head=GithubBranchRef(ref="review/feature-aaaaaaaa"),
                        html_url="https://github.test/octo-org/stacked-review/pull/7",
                        number=7,
                        state="open",
                        title="feature 1",
                    ),
                )
            },
        )
    )

    assert lookup.state == "open"
    assert lookup.pull_request is not None
    assert lookup.pull_request.number == 7


def test_inspect_restack_batches_cached_pull_request_numbers(monkeypatch) -> None:
    batch_calls: list[tuple[str, str, tuple[int, ...]]] = []
    head_calls: list[str] = []
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ResolvedGithubRepository(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        prepared=cast(
            Any,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/one",
                        bookmark_source="generated",
                        cached_change=CachedChange(bookmark="review/one", pr_number=1),
                        revision=SimpleNamespace(
                            change_id="change-1",
                            description="one\n",
                            divergent=False,
                            subject="one",
                        ),
                    ),
                    SimpleNamespace(
                        bookmark="review/two",
                        bookmark_source="generated",
                        cached_change=CachedChange(bookmark="review/two", pr_number=2),
                        revision=SimpleNamespace(
                            change_id="change-2",
                            description="two\n",
                            divergent=False,
                            subject="two",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        trunk_subject="base",
    )

    class FakeGithubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get_pull_requests_by_numbers(
            self,
            owner: str,
            repo: str,
            *,
            pull_numbers,
        ):
            batch_calls.append((owner, repo, tuple(pull_numbers)))
            return {
                1: GithubPullRequest(
                    base=GithubBranchRef(ref="main"),
                    head=GithubBranchRef(ref="review/one"),
                    html_url="https://github.test/octo-org/stacked-review/pull/1",
                    number=1,
                    state="open",
                    title="one",
                ),
                2: GithubPullRequest(
                    base=GithubBranchRef(ref="review/base"),
                    head=GithubBranchRef(ref="review/two"),
                    html_url="https://github.test/octo-org/stacked-review/pull/2",
                    merged_at="2026-03-16T12:00:00Z",
                    number=2,
                    state="closed",
                    title="two",
                ),
            }

        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
        ):
            head_calls.append(head)
            raise AssertionError("head lookup should not run for cached-number revisions")

    monkeypatch.setattr(
        "jj_review.commands.cleanup._build_github_client",
        lambda **kwargs: FakeGithubClient(),
    )

    inspection = asyncio.run(
        _inspect_restack(
            prepared_restack=PreparedRestack(
                apply=False,
                allow_nontrunk_rebase=False,
                prepared_status=prepared_status,
            )
        )
    )

    assert batch_calls == [("octo-org", "stacked-review", (1, 2))]
    assert head_calls == []
    assert inspection.revisions[0].pull_request_lookup is not None
    assert inspection.revisions[1].pull_request_lookup is not None
    assert inspection.revisions[0].pull_request_lookup.state == "open"
    assert inspection.revisions[1].pull_request_lookup.state == "closed"


def test_inspect_restack_batches_uncached_head_refs(monkeypatch) -> None:
    batch_head_calls: list[tuple[str, str, tuple[str, ...]]] = []
    head_calls: list[str] = []
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ResolvedGithubRepository(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        prepared=cast(
            Any,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/one",
                        bookmark_source="generated",
                        cached_change=None,
                        revision=SimpleNamespace(
                            change_id="change-1",
                            description="one\n",
                            divergent=False,
                            subject="one",
                        ),
                    ),
                    SimpleNamespace(
                        bookmark="review/two",
                        bookmark_source="generated",
                        cached_change=None,
                        revision=SimpleNamespace(
                            change_id="change-2",
                            description="two\n",
                            divergent=False,
                            subject="two",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        trunk_subject="base",
    )

    class FakeGithubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get_pull_requests_by_numbers(
            self,
            owner: str,
            repo: str,
            *,
            pull_numbers,
        ):
            return {}

        async def get_pull_requests_by_head_refs(
            self,
            owner: str,
            repo: str,
            *,
            head_refs,
        ):
            batch_head_calls.append((owner, repo, tuple(head_refs)))
            return {
                "review/one": (
                    GithubPullRequest(
                        base=GithubBranchRef(ref="main"),
                        head=GithubBranchRef(ref="review/one"),
                        html_url="https://github.test/octo-org/stacked-review/pull/1",
                        number=1,
                        state="open",
                        title="one",
                    ),
                ),
                "review/two": (
                    GithubPullRequest(
                        base=GithubBranchRef(ref="review/base"),
                        head=GithubBranchRef(ref="review/two"),
                        html_url="https://github.test/octo-org/stacked-review/pull/2",
                        merged_at="2026-03-16T12:00:00Z",
                        number=2,
                        state="closed",
                        title="two",
                    ),
                ),
            }

        async def list_pull_requests(
            self,
            owner: str,
            repo: str,
            *,
            head: str,
            state: str = "all",
        ):
            head_calls.append(head)
            raise AssertionError("REST head lookup should not run for batched head refs")

    monkeypatch.setattr(
        "jj_review.commands.cleanup._build_github_client",
        lambda **kwargs: FakeGithubClient(),
    )

    inspection = asyncio.run(
        _inspect_restack(
            prepared_restack=PreparedRestack(
                apply=False,
                allow_nontrunk_rebase=False,
                prepared_status=prepared_status,
            )
        )
    )

    assert batch_head_calls == [("octo-org", "stacked-review", ("review/one", "review/two"))]
    assert head_calls == []
    assert inspection.revisions[0].pull_request_lookup is not None
    assert inspection.revisions[1].pull_request_lookup is not None
    assert inspection.revisions[0].pull_request_lookup.state == "open"
    assert inspection.revisions[1].pull_request_lookup.state == "closed"


def test_inspect_restack_blocks_when_cached_pr_batch_lookup_fails(monkeypatch) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ResolvedGithubRepository(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        prepared=cast(
            Any,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/one",
                        bookmark_source="generated",
                        cached_change=CachedChange(bookmark="review/one", pr_number=1),
                        revision=SimpleNamespace(
                            change_id="change-1",
                            description="one\n",
                            divergent=False,
                            subject="one",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        trunk_subject="base",
    )

    class FakeGithubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get_pull_requests_by_numbers(
            self,
            owner: str,
            repo: str,
            *,
            pull_numbers,
        ):
            raise GithubClientError("Connection refused")

    monkeypatch.setattr(
        "jj_review.commands.cleanup._build_github_client",
        lambda **kwargs: FakeGithubClient(),
    )

    inspection = asyncio.run(
        _inspect_restack(
            prepared_restack=PreparedRestack(
                apply=False,
                allow_nontrunk_rebase=False,
                prepared_status=prepared_status,
            )
        )
    )

    assert inspection.github_error == "unavailable - check network connectivity"
    assert inspection.github_repository == "octo-org/stacked-review"
    assert inspection.revisions == ()


def test_inspect_restack_blocks_when_head_ref_batch_lookup_fails(monkeypatch) -> None:
    remote = GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git")
    prepared_status = PreparedStatus(
        github_repository=ResolvedGithubRepository(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        prepared=cast(
            Any,
            SimpleNamespace(
                remote=remote,
                remote_error=None,
                status_revisions=(
                    SimpleNamespace(
                        bookmark="review/one",
                        bookmark_source="generated",
                        cached_change=None,
                        revision=SimpleNamespace(
                            change_id="change-1",
                            description="one\n",
                            divergent=False,
                            subject="one",
                        ),
                    ),
                ),
            ),
        ),
        selected_revset="@",
        trunk_subject="base",
    )

    class FakeGithubClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

        async def get_pull_requests_by_numbers(
            self,
            owner: str,
            repo: str,
            *,
            pull_numbers,
        ):
            return {}

        async def get_pull_requests_by_head_refs(
            self,
            owner: str,
            repo: str,
            *,
            head_refs,
        ):
            raise GithubClientError("Connection refused")

    monkeypatch.setattr(
        "jj_review.commands.cleanup._build_github_client",
        lambda **kwargs: FakeGithubClient(),
    )

    inspection = asyncio.run(
        _inspect_restack(
            prepared_restack=PreparedRestack(
                apply=False,
                allow_nontrunk_rebase=False,
                prepared_status=prepared_status,
            )
        )
    )

    assert inspection.github_error == "unavailable - check network connectivity"
    assert inspection.github_repository == "octo-org/stacked-review"
    assert inspection.revisions == ()
