from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

from jj_review.commands.cleanup import (
    PreparedCleanup,
    PreparedRestack,
    _should_inspect_stack_comment_cleanup,
    _stream_cleanup_async,
    stream_restack,
)
from jj_review.commands.submit import ResolvedGithubRepository
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, ReviewState


def test_should_skip_stack_comment_inspection_for_stale_open_change_without_comment_hint(
) -> None:
    bookmark_state = BookmarkState(
        name="review/feature-aaaaaaaa",
        remote_targets=(
            RemoteBookmarkState(remote="origin", targets=("commit-1",)),
        ),
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
        state_dir=None,
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
        state_dir=None,
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
        state_dir=None,
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

    result = stream_restack(prepared_restack=prepared_restack)

    assert result.blocked is False
    assert len(result.actions) == 1
    assert result.actions[0].kind == "restack"
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "planned"


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
        apply=True,
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
        state_dir=None,
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

    result = stream_restack(prepared_restack=prepared_restack)

    assert result.blocked is False
    assert rebase_calls == [("survivor-change", "trunk-commit")]
    assert len(result.actions) == 1
    assert result.actions[0].kind == "restack"
    assert result.actions[0].message == "rebase survivor onto trunk()"
    assert result.actions[0].status == "applied"
