from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from jj_review.commands import cleanup as cleanup_module
from jj_review.commands.cleanup import (
    CleanupAction,
    PreparedCleanup,
    PreparedCleanupChange,
    PreparedRestack,
    StackCommentCleanupPlan,
    _plan_remote_branch_cleanup,
    _plan_restack_operations,
    _resolve_stack_summary_comment,
    _resolve_unlinked_pull_request_number,
    _run_cleanup_async,
    _should_inspect_stack_comment_cleanup,
    _stream_restack,
)
from jj_review.github.client import GithubClient
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import GithubBranchRef, GithubIssueComment, GithubPullRequest
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision
from jj_review.review.status import PreparedStatus, ReviewStatusRevision
from jj_review.state.store import ReviewStateStore


def _local_revision(
    *,
    change_id: str,
    commit_id: str,
    parents: tuple[str, ...],
    divergent: bool = False,
    hidden: bool = False,
    immutable: bool = False,
) -> LocalRevision:
    return LocalRevision(
        change_id=change_id,
        commit_id=commit_id,
        current_working_copy=False,
        description=f"{change_id}\n",
        divergent=divergent,
        empty=False,
        hidden=hidden,
        immutable=immutable,
        parents=parents,
    )


def test_cleanup_skips_stack_comment_lookup_when_open_pr_still_has_remote_branch() -> None:
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


def test_cleanup_inspects_stack_comment_when_cached_comment_id_is_present() -> None:
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


def test_cleanup_inspects_stack_comment_when_stale_change_lost_remote_branch() -> None:
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


def test_cleanup_skips_stack_comment_lookup_for_closed_pull_request() -> None:
    should_inspect = _should_inspect_stack_comment_cleanup(
        bookmark_state=BookmarkState(name="review/feature-aaaaaaaa"),
        cached_change=CachedChange(
            bookmark="review/feature-aaaaaaaa",
            pr_number=7,
            pr_state="closed",
            stack_comment_id=12,
        ),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        stale_reason="local change is no longer reviewable",
    )

    assert should_inspect is False


def test_cleanup_skips_stack_comment_lookup_when_no_bookmark_or_cached_comment_remains() -> None:
    should_inspect = _should_inspect_stack_comment_cleanup(
        bookmark_state=BookmarkState(name=""),
        cached_change=CachedChange(
            pr_number=7,
            pr_state="open",
        ),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        stale_reason="local change is no longer reviewable",
    )

    assert should_inspect is False


def test_stale_change_reasons_classifies_cached_changes_in_bulk() -> None:
    live = _local_revision(
        change_id="live-change",
        commit_id="live",
        parents=("trunk",),
    )
    duplicate_1 = _local_revision(
        change_id="duplicate-change",
        commit_id="dup-1",
        parents=("trunk",),
    )
    duplicate_2 = _local_revision(
        change_id="duplicate-change",
        commit_id="dup-2",
        parents=("trunk",),
    )
    not_reviewable = _local_revision(
        change_id="merge-change",
        commit_id="merge",
        parents=("left", "right"),
    )
    branched = _local_revision(
        change_id="branched-change",
        commit_id="branched",
        parents=("branch-parent",),
    )

    class FakeClient:
        def query_revisions_by_change_ids(self, change_ids: tuple[str, ...]):
            assert change_ids == (
                "live-change",
                "missing-change",
                "duplicate-change",
                "merge-change",
                "branched-change",
            )
            return {
                "live-change": (live,),
                "missing-change": (),
                "duplicate-change": (duplicate_1, duplicate_2),
                "merge-change": (not_reviewable,),
                "branched-change": (branched,),
            }

        def supported_review_stack_change_ids(self, candidate_revisions):
            assert tuple(revision.commit_id for revision in candidate_revisions) == (
                "live",
                "branched",
            )
            return {"live-change", "branched-change"}

    reasons = cleanup_module._stale_change_reasons(
        change_ids=(
            "live-change",
            "missing-change",
            "duplicate-change",
            "merge-change",
            "branched-change",
        ),
        jj_client=cast(JjClient, FakeClient()),
    )

    assert reasons == {
        "live-change": None,
        "missing-change": "no visible local change matches that cached change ID",
        "duplicate-change": "multiple visible revisions still share that change ID",
        "merge-change": "local change is no longer reviewable",
        "branched-change": None,
    }


def test_resolve_stack_summary_comment_blocks_multiple_candidates() -> None:
    async def fake_list_issue_comments(owner, repo, issue_number):
        return (_stack_comment(comment_id=11), _stack_comment(comment_id=12))

    result = asyncio.run(
        _resolve_stack_summary_comment(
            cached_change=CachedChange(stack_comment_id=None),
            github_client=cast(
                GithubClient,
                SimpleNamespace(list_issue_comments=fake_list_issue_comments),
            ),
            github_repository=SimpleNamespace(owner="octo-org", repo="stacked-review"),
            pull_request_number=7,
        )
    )

    assert result == CleanupAction(
        kind="stack summary comment",
        body=(
            "cannot delete stack summary comments because GitHub reports multiple "
            "candidates on PR #7"
        ),
        status="blocked",
    )


def test_resolve_unlinked_pull_request_number_blocks_multiple_pull_requests() -> None:
    result = asyncio.run(
        _resolve_unlinked_pull_request_number(
            bookmark_state=BookmarkState(name="review/feature-aaaaaaaa"),
            github_client=cast(
                GithubClient,
                SimpleNamespace(list_pull_requests=_fake_list_pull_requests),
            ),
            github_repository=SimpleNamespace(owner="octo-org", repo="stacked-review"),
        )
    )

    assert isinstance(result, CleanupAction)
    assert result.kind == "stack summary comment"
    assert result.status == "blocked"
    assert (
        result.message
        == "cannot delete stack summary comment because GitHub reports multiple pull "
        "requests for unlinked bookmark review/feature-aaaaaaaa"
    )


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
            "changes": state_changes,
        }
    )
    prepared_cleanup = PreparedCleanup(
        dry_run=True,
        bookmark_states={},
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        jj_client=cast(JjClient, SimpleNamespace()),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        remote_context_loaded=True,
        state=state,
        state_store=cast(ReviewStateStore, SimpleNamespace(save=lambda state: None)),
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
        "jj_review.commands.cleanup.build_github_client",
        lambda **kwargs: FakeGithubClientContext(),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._plan_stack_comment_cleanup",
        fake_plan_stack_comment_cleanup,
    )

    result = asyncio.run(
        _run_cleanup_async(
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
            "changes": {
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
        dry_run=True,
        bookmark_states={},
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        jj_client=cast(JjClient, SimpleNamespace()),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        remote_context_loaded=True,
        state=state,
        state_store=cast(ReviewStateStore, SimpleNamespace(save=lambda state: None)),
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
            _run_cleanup_async(
                on_action=lambda action: streamed_actions.append(action.message),
                prepared_cleanup=prepared_cleanup,
            )
        )
        for _ in range(5):
            if len(streamed_actions) == 2:
                break
            await asyncio.sleep(0)

        assert streamed_actions == [
            "remove tracking for change-1 (local change is no longer reviewable)",
            "remove tracking for change-2 (local change is no longer reviewable)",
        ]
        release_comment_checks.set()
        await task

    monkeypatch.setattr(
        "jj_review.commands.cleanup.build_github_client",
        lambda **kwargs: FakeGithubClientContext(),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: "local change is no longer reviewable"
            for change_id in kwargs["change_ids"]
        },
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._plan_stack_comment_cleanup",
        fake_plan_stack_comment_cleanup,
    )

    asyncio.run(exercise_cleanup())


def test_stream_cleanup_apply_clears_cached_stack_comment_after_deletion(
    monkeypatch,
) -> None:
    state = ReviewState.model_validate(
        {
            "changes": {
                "change-1": CachedChange(
                    bookmark="review/feature-1",
                    pr_number=1,
                    pr_state="closed",
                    stack_comment_id=12,
                ).model_dump(exclude_none=True),
            }
        }
    )
    saved_states: list[ReviewState] = []
    deleted_comment_ids: list[int] = []
    state_store = cast(
        ReviewStateStore,
        SimpleNamespace(
            require_writable=lambda: Path("/tmp"),
            save=saved_states.append,
        ),
    )
    prepared_cleanup = PreparedCleanup(
        dry_run=False,
        bookmark_states={},
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        jj_client=cast(JjClient, SimpleNamespace()),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        remote_context_loaded=True,
        state=state,
        state_store=state_store,
    )

    class FakeGithubClientContext:
        async def __aenter__(self):
            return SimpleNamespace(
                delete_issue_comment=lambda owner, repo, *, comment_id: _record_deleted_comment(
                    comment_id
                )
            )

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def _record_deleted_comment(comment_id: int) -> None:
        deleted_comment_ids.append(comment_id)

    monkeypatch.setattr(
        "jj_review.commands.cleanup.build_github_client",
        lambda **kwargs: FakeGithubClientContext(),
    )
    async def fake_plan_stack_comment_cleanup(**kwargs):
        return StackCommentCleanupPlan(
            action=CleanupAction(
                kind="stack summary comment",
                body="delete stack summary comment #12 from PR #1",
                status="planned",
            ),
            comment_id=12,
        )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {change_id: None for change_id in kwargs["change_ids"]},
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup._plan_stack_comment_cleanup",
        fake_plan_stack_comment_cleanup,
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.check_same_kind_intent",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.write_new_intent",
        lambda *args, **kwargs: None,
    )

    result = asyncio.run(
        _run_cleanup_async(
            on_action=None,
            prepared_cleanup=prepared_cleanup,
        )
    )

    assert deleted_comment_ids == [12]
    assert result.actions == (
        CleanupAction(
            kind="stack summary comment",
            body="delete stack summary comment #12 from PR #1",
            status="applied",
        ),
    )
    assert [saved_state.changes["change-1"].stack_comment_id for saved_state in saved_states] == [
        None,
        None,
    ]


def test_stream_cleanup_without_github_repository_reuses_local_cleanup_pass(
    monkeypatch,
) -> None:
    state = ReviewState.model_validate(
        {
            "changes": {
                "stale-change": CachedChange(
                    bookmark="review/feature-stale",
                ).model_dump(exclude_none=True),
                "live-change": CachedChange(
                    bookmark="review/feature-live",
                ).model_dump(exclude_none=True),
            }
        }
    )
    saved_states: list[ReviewState] = []
    state_store = cast(
        ReviewStateStore,
        SimpleNamespace(
            require_writable=lambda: Path("/tmp"),
            save=saved_states.append,
        ),
    )
    prepared_cleanup = PreparedCleanup(
        dry_run=False,
        bookmark_states={},
        github_repository=None,
        github_repository_error="GitHub unavailable",
        jj_client=cast(JjClient, SimpleNamespace()),
        remote=None,
        remote_error=None,
        remote_context_loaded=False,
        state=state,
        state_store=state_store,
    )

    monkeypatch.setattr(
        "jj_review.commands.cleanup._stale_change_reasons",
        lambda **kwargs: {
            change_id: (
                "local change is no longer reviewable" if change_id == "stale-change" else None
            )
            for change_id in kwargs["change_ids"]
        },
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.check_same_kind_intent",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.write_new_intent",
        lambda *args, **kwargs: None,
    )

    result = asyncio.run(
        _run_cleanup_async(
            on_action=None,
            prepared_cleanup=prepared_cleanup,
        )
    )

    assert [action.message for action in result.actions] == [
        "remove tracking for stale-ch (local change is no longer reviewable)"
    ]
    assert [sorted(saved_state.changes) for saved_state in saved_states] == [
        ["live-change"],
        ["live-change"],
    ]


def test_stream_cleanup_skips_github_client_when_no_comment_inspection_is_needed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    state_store = cast(
        ReviewStateStore,
        SimpleNamespace(
            require_writable=lambda: tmp_path,
            save=lambda _state: None,
        ),
    )
    prepared_cleanup = PreparedCleanup(
        dry_run=False,
        bookmark_states={},
        github_repository=ParsedGithubRepo(
            host="github.com",
            owner="octo-org",
            repo="stacked-review",
        ),
        github_repository_error=None,
        jj_client=cast(JjClient, SimpleNamespace()),
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
        remote_error=None,
        remote_context_loaded=True,
        state=ReviewState(),
        state_store=state_store,
    )

    monkeypatch.setattr(
        "jj_review.commands.cleanup._run_local_cleanup_pass",
        lambda **kwargs: (
            PreparedCleanupChange(
                bookmark_state=BookmarkState(name="review/feature-aaaaaaaa"),
                cached_change=CachedChange(bookmark="review/feature-aaaaaaaa"),
                change_id="change-1",
                inspect_stack_comment=False,
                stale_reason=None,
            ),
        ),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.build_github_client",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(
                "cleanup should not build a GitHub client when no comment inspection is needed"
            )
        ),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.check_same_kind_intent",
        lambda *args, **kwargs: (),
    )
    monkeypatch.setattr(
        "jj_review.commands.cleanup.write_new_intent",
        lambda *args, **kwargs: tmp_path / "incomplete-cleanup.json",
    )

    result = asyncio.run(
        _run_cleanup_async(
            on_action=None,
            prepared_cleanup=prepared_cleanup,
            stale_reasons={},
        )
    )

    assert result.actions == ()


def test_stream_restack_plans_rebase_for_survivor_above_merged_path_revision(
    monkeypatch,
) -> None:
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
        dry_run=True,
        prepared_status=cast(
            PreparedStatus,
            SimpleNamespace(
                github_repository=None,
                prepared=SimpleNamespace(
                    client=SimpleNamespace(),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=1, pr_state="merged"),
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            ),
                        ),
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=2, pr_state="open"),
                            revision=SimpleNamespace(
                                change_id="survivor-change",
                                commit_id="survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    monkeypatch.setattr(
        "jj_review.commands.cleanup.stream_status",
        lambda **kwargs: SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            incomplete=False,
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(merged_revision, survivor_revision),
            selected_revset="@",
        ),
    )

    result = _stream_restack(prepared_restack=prepared_restack)

    assert len(result.actions) == 1
    assert result.actions[0].kind == "restack"
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "planned"


async def _fake_list_pull_requests(owner, repo, *, head, state="all"):
    return (
        _pull_request(number=7, head_ref="review/feature-aaaaaaaa"),
        _pull_request(number=8, head_ref="review/feature-aaaaaaaa"),
    )


def _pull_request(*, number: int, head_ref: str) -> GithubPullRequest:
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(ref=head_ref, label=f"octo-org:{head_ref}"),
        html_url=f"https://github.test/octo-org/stacked-review/pull/{number}",
        number=number,
        state="open",
        title=f"feature {number}",
    )


def _stack_comment(*, comment_id: int) -> GithubIssueComment:
    return GithubIssueComment(
        body="intro\n<!-- jj-review-stack -->\nsummary",
        html_url=f"https://github.test/comments/{comment_id}",
        id=comment_id,
    )


def test_stream_restack_applies_rebase_for_survivor_above_merged_path_revision(
    monkeypatch,
) -> None:
    rebase_calls: list[tuple[str, str]] = []

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
        dry_run=False,
        prepared_status=cast(
            PreparedStatus,
            SimpleNamespace(
                github_repository=None,
                prepared=SimpleNamespace(
                    client=FakeClient(),
                    state_store=SimpleNamespace(require_writable=lambda: Path("/tmp")),
                    stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                    status_revisions=(
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=1, pr_state="merged"),
                            revision=SimpleNamespace(
                                change_id="merged-change",
                                commit_id="merged-commit",
                                only_parent_commit_id=lambda: "trunk-commit",
                            ),
                        ),
                        SimpleNamespace(
                            cached_change=CachedChange(pr_number=2, pr_state="open"),
                            revision=SimpleNamespace(
                                change_id="survivor-change",
                                commit_id="survivor-commit",
                                only_parent_commit_id=lambda: "merged-commit",
                            ),
                        ),
                    ),
                ),
            ),
        ),
    )

    monkeypatch.setattr(
        "jj_review.commands.cleanup.stream_status",
        lambda **kwargs: SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            incomplete=False,
            remote=GitRemote(
                name="origin",
                url="git@github.com:octo-org/stacked-review.git",
            ),
            remote_error=None,
            revisions=(merged_revision, survivor_revision),
            selected_revset="@",
        ),
    )

    result = _stream_restack(prepared_restack=prepared_restack)

    assert rebase_calls == [("survivor-change", "trunk-commit")]
    assert len(result.actions) == 1
    assert result.actions[0].kind == "restack"
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "applied"


def test_plan_restack_operations_blocks_divergent_survivor() -> None:
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
    divergent_revision = SimpleNamespace(
        cached_change=None,
        change_id="divergent-change",
        local_divergent=True,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="main"),
                number=2,
                state="open",
            ),
            state="open",
        ),
        subject="divergent feature",
    )
    prepared_status = cast(
        PreparedStatus,
        SimpleNamespace(
            prepared=SimpleNamespace(
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
                            change_id="divergent-change",
                            commit_id="divergent-commit",
                            only_parent_commit_id=lambda: "merged-commit",
                        )
                    ),
                )
            )
        ),
    )

    plan = _plan_restack_operations(
        path_revisions=cast(
            tuple[ReviewStatusRevision, ...],
            (merged_revision, divergent_revision),
        ),
        prepared_status=prepared_status,
    )

    assert plan.blocked is True
    assert plan.rebase_plans == ()
    assert any(
        "multiple visible revisions still share that change ID" in action.message
        for action in plan.pre_actions
    )


def test_plan_remote_branch_cleanup_blocks_when_local_bookmark_still_exists() -> None:
    plan = _plan_remote_branch_cleanup(
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1",),
            remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
        ),
        cached_change=CachedChange(bookmark="review/feature-aaaaaaaa"),
        local_bookmark_forget_planned=False,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
    )

    assert plan is not None
    assert plan.action.status == "blocked"
    assert "local bookmark" in plan.action.message


def test_plan_remote_branch_cleanup_allows_delete_when_local_forget_is_planned() -> None:
    plan = _plan_remote_branch_cleanup(
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1",),
            remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
        ),
        cached_change=CachedChange(bookmark="review/feature-aaaaaaaa"),
        local_bookmark_forget_planned=True,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
    )

    assert plan is not None
    assert plan.action.status == "planned"
    assert plan.expected_remote_target == "commit-1"


def test_plan_local_bookmark_cleanup_forgets_safe_review_bookmark() -> None:
    plan = cleanup_module._plan_local_bookmark_cleanup(
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        cached_change=CachedChange(
            bookmark="review/feature-aaaaaaaa",
            last_submitted_commit_id="commit-1",
        ),
        stale_reason="local change is no longer reviewable",
    )

    assert plan is not None
    assert plan.kind == "local bookmark"
    assert plan.status == "planned"
    assert (
        plan.message
        == "forget review/feature-aaaaaaaa (local change is no longer reviewable)"
    )


def test_plan_local_bookmark_cleanup_blocks_moved_review_bookmark() -> None:
    plan = cleanup_module._plan_local_bookmark_cleanup(
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-2",),
        ),
        cached_change=CachedChange(
            bookmark="review/feature-aaaaaaaa",
            last_submitted_commit_id="commit-1",
        ),
        stale_reason="local change is no longer reviewable",
    )

    assert plan is not None
    assert plan.status == "blocked"
    assert "different revision" in plan.message


def test_apply_stale_cleanup_mutation_plans_batches_remote_and_local_work() -> None:
    calls: list[tuple[str, object]] = []
    recorded_actions: list[CleanupAction] = []

    class FakeClient:
        def delete_remote_bookmarks(
            self,
            *,
            remote: str,
            deletions: tuple[tuple[str, str], ...],
            fetch: bool = True,
        ) -> None:
            calls.append(("delete_remote_bookmarks", (remote, deletions, fetch)))

        def forget_bookmarks(self, bookmarks: tuple[str, ...]) -> None:
            calls.append(("forget_bookmarks", bookmarks))

        def fetch_remote(self, *, remote: str, branches=None) -> None:
            calls.append(("fetch_remote", (remote, branches)))

    cleanup_module._apply_stale_cleanup_mutation_plans(
        jj_client=cast(JjClient, FakeClient()),
        mutation_plans=(
            cleanup_module._StaleCleanupMutationPlan(
                cached_change=CachedChange(bookmark="review/feature-aaaaaaaa"),
                local_bookmark_action=CleanupAction(
                    kind="local bookmark",
                    body="forget review/feature-aaaaaaaa (stale)",
                    status="planned",
                ),
                remote_plan=cleanup_module.RemoteBranchCleanupPlan(
                    action=CleanupAction(
                        kind="remote branch",
                        body="delete review/feature-aaaaaaaa@origin",
                        status="planned",
                    ),
                    expected_remote_target="commit-1",
                ),
            ),
            cleanup_module._StaleCleanupMutationPlan(
                cached_change=CachedChange(bookmark="review/feature-bbbbbbbb"),
                local_bookmark_action=CleanupAction(
                    kind="local bookmark",
                    body="forget review/feature-bbbbbbbb (stale)",
                    status="planned",
                ),
                remote_plan=cleanup_module.RemoteBranchCleanupPlan(
                    action=CleanupAction(
                        kind="remote branch",
                        body="delete review/feature-bbbbbbbb@origin",
                        status="planned",
                    ),
                    expected_remote_target="commit-2",
                ),
            ),
        ),
        record_action=recorded_actions.append,
        remote=GitRemote(name="origin", url="git@github.com:octo-org/stacked-review.git"),
    )

    assert calls == [
        (
            "delete_remote_bookmarks",
            (
                "origin",
                (
                    ("review/feature-aaaaaaaa", "commit-1"),
                    ("review/feature-bbbbbbbb", "commit-2"),
                ),
                False,
            ),
        ),
        ("forget_bookmarks", ("review/feature-aaaaaaaa", "review/feature-bbbbbbbb")),
        ("fetch_remote", ("origin", None)),
    ]
    assert recorded_actions == [
        CleanupAction(
            kind="remote branch",
            body="delete review/feature-aaaaaaaa@origin",
            status="applied",
        ),
        CleanupAction(
            kind="remote branch",
            body="delete review/feature-bbbbbbbb@origin",
            status="applied",
        ),
        CleanupAction(
            kind="local bookmark",
            body="forget review/feature-aaaaaaaa (stale)",
            status="applied",
        ),
        CleanupAction(
            kind="local bookmark",
            body="forget review/feature-bbbbbbbb (stale)",
            status="applied",
        ),
    ]
