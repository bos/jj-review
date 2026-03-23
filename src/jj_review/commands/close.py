"""Close managed review state for a selected local path."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review.cache import ReviewStateStore
from jj_review.commands.review_state import PreparedStatus, prepare_status, stream_status
from jj_review.commands.submit import _STACK_COMMENT_MARKER, _build_github_client
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.intent import (
    check_same_kind_intent,
    delete_intent,
    retire_superseded_intents,
    write_intent,
)
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.cache import CachedChange
from jj_review.models.github import GithubIssueComment
from jj_review.models.intent import CloseIntent

CloseActionStatus = Literal["applied", "blocked", "planned"]


@dataclass(frozen=True, slots=True)
class CloseAction:
    """One close action that was planned, applied, or blocked."""

    kind: str
    message: str
    status: CloseActionStatus


@dataclass(frozen=True, slots=True)
class CloseResult:
    """Rendered close result for the selected repository."""

    actions: tuple[CloseAction, ...]
    applied: bool
    blocked: bool
    cleanup: bool
    github_error: str | None
    github_repository: str | None
    remote: GitRemote | None
    remote_error: str | None
    selected_revset: str


@dataclass(frozen=True, slots=True)
class PreparedClose:
    """Locally prepared close inputs before any GitHub mutation."""

    apply: bool
    cleanup: bool
    prepared_status: PreparedStatus
    state_dir: Path | None


def run_close(
    *,
    apply: bool,
    cleanup: bool,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> CloseResult:
    """Preview or apply close actions for the selected local review path."""

    prepared_close = prepare_close(
        apply=apply,
        change_overrides=change_overrides,
        cleanup=cleanup,
        config=config,
        repo_root=repo_root,
        revset=revset,
    )
    return stream_close(prepared_close=prepared_close)


def prepare_close(
    *,
    apply: bool,
    change_overrides: dict[str, ChangeConfig],
    cleanup: bool,
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> PreparedClose:
    """Resolve local close inputs before any GitHub inspection."""

    state_store = ReviewStateStore.for_repo(repo_root)
    state_dir = state_store.require_writable() if apply else state_store.state_dir
    return PreparedClose(
        apply=apply,
        cleanup=cleanup,
        prepared_status=prepare_status(
            change_overrides=change_overrides,
            config=config,
            fetch_remote_state=apply,
            persist_bookmarks=False,
            repo_root=repo_root,
            revset=revset,
        ),
        state_dir=state_dir,
    )


def stream_close(
    *,
    prepared_close: PreparedClose,
    on_action: Callable[[CloseAction], None] | None = None,
) -> CloseResult:
    """Inspect GitHub state for prepared close inputs and optionally stream actions."""

    status_result = stream_status(
        persist_cache_updates=False,
        prepared_status=prepared_close.prepared_status,
    )
    return asyncio.run(
        _stream_close_async(
            on_action=on_action,
            prepared_close=prepared_close,
            status_result=status_result,
        )
    )


async def _stream_close_async(
    *,
    on_action: Callable[[CloseAction], None] | None,
    prepared_close: PreparedClose,
    status_result,
) -> CloseResult:
    prepared_status = prepared_close.prepared_status
    prepared = prepared_status.prepared
    remote = prepared.remote
    github_repository = prepared_status.github_repository

    actions: list[CloseAction] = []
    blocked = False

    def record_action(action: CloseAction) -> None:
        nonlocal blocked
        if action.status == "blocked":
            blocked = True
        actions.append(action)
        if on_action is not None:
            on_action(action)

    if not status_result.revisions:
        return CloseResult(
            actions=(),
            applied=prepared_close.apply,
            blocked=False,
            cleanup=prepared_close.cleanup,
            github_error=status_result.github_error,
            github_repository=github_repository.full_name if github_repository else None,
            remote=remote,
            remote_error=prepared.remote_error,
            selected_revset=prepared_status.selected_revset,
        )

    if status_result.github_error is not None or github_repository is None:
        record_action(
            CloseAction(
                kind="close",
                message=(
                    "cannot close managed pull requests without live GitHub state; "
                    "fix GitHub access and retry"
                ),
                status="blocked",
            )
        )
        return CloseResult(
            actions=tuple(actions),
            applied=False,
            blocked=True,
            cleanup=prepared_close.cleanup,
            github_error=status_result.github_error,
            github_repository=github_repository.full_name if github_repository else None,
            remote=remote,
            remote_error=prepared.remote_error,
            selected_revset=prepared_status.selected_revset,
        )

    state_store = prepared.state_store
    current_state = state_store.load() if prepared_close.apply else prepared.state
    next_changes = dict(current_state.changes)
    commit_ids_by_change_id = {
        prepared_revision.revision.change_id: prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    }

    intent: CloseIntent | None = None
    intent_path: Path | None = None
    stale_intents = []
    succeeded = False
    try:
        if prepared_close.apply and prepared_close.state_dir is not None:
            ordered_change_ids = tuple(
                revision.change_id for revision in status_result.revisions
            )
            intent = CloseIntent(
                kind="close",
                pid=os.getpid(),
                label=(
                    "close --cleanup on " if prepared_close.cleanup else "close on "
                )
                + prepared_status.selected_revset,
                display_revset=prepared_status.selected_revset,
                ordered_change_ids=ordered_change_ids,
                cleanup=prepared_close.cleanup,
                started_at=datetime.now(UTC).isoformat(),
            )
            stale_intents = check_same_kind_intent(prepared_close.state_dir, intent)
            for loaded in stale_intents:
                print(f"Note: a previous close was interrupted ({loaded.intent.label})")
            intent_path = write_intent(prepared_close.state_dir, intent)

        async with _build_github_client(
            base_url=github_repository.api_base_url
        ) as github_client:
            for revision in status_result.revisions:
                cached_change = (
                    revision.cached_change
                    or current_state.changes.get(revision.change_id)
                )
                lookup = revision.pull_request_lookup
                if lookup is None:
                    continue
                if lookup.state in {"ambiguous", "error"}:
                    record_action(
                        CloseAction(
                            kind="close",
                            message=lookup.message
                            or "cannot safely determine the pull request for this path",
                            status="blocked",
                        )
                    )
                    return CloseResult(
                        actions=tuple(actions),
                        applied=prepared_close.apply,
                        blocked=True,
                        cleanup=prepared_close.cleanup,
                        github_error=status_result.github_error,
                        github_repository=github_repository.full_name,
                        remote=remote,
                        remote_error=prepared.remote_error,
                        selected_revset=prepared_status.selected_revset,
                    )
                revision_label = _revision_label(revision)
                if lookup.state == "missing":
                    if _has_active_cached_linkage(cached_change):
                        record_action(
                            CloseAction(
                                kind="close",
                                message=(
                                    f"cannot close {revision_label} because "
                                    "GitHub no longer reports a pull request for its "
                                    "review branch; run `status --fetch` or `relink` "
                                    "before retrying"
                                ),
                                status="blocked",
                            )
                        )
                        return CloseResult(
                            actions=tuple(actions),
                            applied=prepared_close.apply,
                            blocked=True,
                            cleanup=prepared_close.cleanup,
                            github_error=status_result.github_error,
                            github_repository=github_repository.full_name,
                            remote=remote,
                            remote_error=prepared.remote_error,
                            selected_revset=prepared_status.selected_revset,
                        )
                    if not prepared_close.cleanup or cached_change is None:
                        continue

                    updated_change = _retire_cached_change(
                        cached_change,
                        cleanup=True,
                        pr_state=cached_change.pr_state or "closed",
                    )
                    if updated_change != cached_change:
                        next_changes[revision.change_id] = updated_change
                        record_action(
                            CloseAction(
                                kind="cache",
                                message=f"retire active review state for {revision_label}",
                                status="applied" if prepared_close.apply else "planned",
                            )
                        )
                    await _cleanup_revision(
                        apply=prepared_close.apply,
                        bookmark_state=prepared.client.get_bookmark_state(revision.bookmark),
                        cached_change=updated_change,
                        github_client=github_client,
                        github_repository=github_repository,
                        next_changes=next_changes,
                        record_action=record_action,
                        jj_client=prepared.client,
                        remote_name=remote.name if remote is not None else None,
                        commit_id=commit_ids_by_change_id.get(revision.change_id),
                        revision=revision,
                        revision_label=revision_label,
                    )
                    continue

                cached_change = _close_cached_change(
                    cached_change=cached_change,
                    revision=revision,
                )
                if cached_change is None:
                    continue
                if lookup.state == "open" and lookup.pull_request is not None:
                    record_action(
                        CloseAction(
                            kind="pull request",
                            message=(
                                f"close PR #{lookup.pull_request.number} for "
                                f"{revision_label}"
                            ),
                            status="applied" if prepared_close.apply else "planned",
                        )
                    )
                    if prepared_close.apply:
                        await github_client.close_pull_request(
                            github_repository.owner,
                            github_repository.repo,
                            pull_number=lookup.pull_request.number,
                        )

                    updated_change = _retire_cached_change(
                        cached_change,
                        cleanup=prepared_close.cleanup,
                        pr_state="closed",
                    )
                    if updated_change != cached_change:
                        next_changes[revision.change_id] = updated_change
                        record_action(
                            CloseAction(
                                kind="cache",
                                message=(
                                    f"retire active review state for {revision_label}"
                                ),
                                status="applied" if prepared_close.apply else "planned",
                            )
                        )

                    if prepared_close.cleanup:
                        await _cleanup_revision(
                            apply=prepared_close.apply,
                            bookmark_state=prepared.client.get_bookmark_state(
                                revision.bookmark
                            ),
                            cached_change=updated_change,
                            github_client=github_client,
                            github_repository=github_repository,
                            next_changes=next_changes,
                            record_action=record_action,
                            jj_client=prepared.client,
                            remote_name=remote.name if remote is not None else None,
                            commit_id=commit_ids_by_change_id.get(revision.change_id),
                            revision=revision,
                            revision_label=revision_label,
                        )
                    continue

                if lookup.state != "closed":
                    continue

                pr_state = "merged" if (
                    lookup.pull_request is not None and lookup.pull_request.merged_at is not None
                ) else "closed"
                if cached_change.pr_state == "merged":
                    pr_state = "merged"

                updated_change = _retire_cached_change(
                    cached_change,
                    cleanup=prepared_close.cleanup,
                    pr_state=pr_state,
                )
                if updated_change != cached_change:
                    next_changes[revision.change_id] = updated_change
                    record_action(
                        CloseAction(
                            kind="cache",
                            message=f"retire active review state for {revision_label}",
                            status="applied" if prepared_close.apply else "planned",
                        )
                    )

                if prepared_close.cleanup:
                    await _cleanup_revision(
                        apply=prepared_close.apply,
                        bookmark_state=prepared.client.get_bookmark_state(
                            revision.bookmark
                        ),
                        cached_change=updated_change,
                        github_client=github_client,
                        github_repository=github_repository,
                        next_changes=next_changes,
                        record_action=record_action,
                        jj_client=prepared.client,
                        remote_name=remote.name if remote is not None else None,
                        commit_id=commit_ids_by_change_id.get(revision.change_id),
                        revision=revision,
                        revision_label=revision_label,
                    )

        if prepared_close.apply and next_changes != current_state.changes:
            state_store.save(current_state.model_copy(update={"changes": next_changes}))

        succeeded = True
        return CloseResult(
            actions=tuple(actions),
            applied=prepared_close.apply,
            blocked=blocked,
            cleanup=prepared_close.cleanup,
            github_error=status_result.github_error,
            github_repository=github_repository.full_name,
            remote=remote,
            remote_error=prepared.remote_error,
            selected_revset=prepared_status.selected_revset,
        )
    finally:
        if succeeded and intent_path is not None and intent is not None:
            retire_superseded_intents(stale_intents, intent)
            delete_intent(intent_path)


async def _cleanup_revision(
    *,
    apply: bool,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
    jj_client: JjClient,
    remote_name: str | None,
    commit_id: str | None,
    next_changes: dict[str, CachedChange],
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: str,
) -> None:
    bookmark = cached_change.bookmark
    local_forget_planned = False
    remote_delete_planned = False
    local_conflict = False
    remote_conflict = False

    if bookmark is not None and bookmark.startswith("review/"):
        local_target = bookmark_state.local_target
        if len(bookmark_state.local_targets) > 1:
            record_action(
                CloseAction(
                    kind="local bookmark",
                    message=(
                        f"cannot forget local review bookmark {bookmark!r} because it is "
                        "conflicted"
                    ),
                    status="blocked",
                )
            )
            local_conflict = True
        elif commit_id is not None and local_target is not None and local_target != commit_id:
            record_action(
                CloseAction(
                    kind="local bookmark",
                    message=(
                        f"cannot forget local review bookmark {bookmark!r} because it "
                        "already points to a different revision"
                    ),
                    status="blocked",
                )
            )
            local_conflict = True
        elif commit_id is not None and local_target == commit_id:
            local_forget_planned = True

        remote_state = (
            bookmark_state.remote_target(remote_name)
            if remote_name is not None
            else None
        )
        if remote_state is not None and remote_name is not None and commit_id is not None:
            if len(remote_state.targets) > 1:
                record_action(
                    CloseAction(
                        kind="remote branch",
                        message=(
                            f"cannot delete remote review branch {bookmark}@{remote_name} "
                            "because the remote bookmark is conflicted"
                        ),
                        status="blocked",
                    )
                )
                remote_conflict = True
            elif remote_state.target != commit_id:
                record_action(
                    CloseAction(
                        kind="remote branch",
                        message=(
                            f"cannot delete remote review branch {bookmark}@{remote_name} "
                            "because it already points to a different revision"
                        ),
                        status="blocked",
                    )
                )
                remote_conflict = True
            else:
                remote_delete_planned = True

        if local_conflict:
            remote_delete_planned = False
        if remote_conflict:
            local_forget_planned = False

        if remote_delete_planned:
            record_action(
                CloseAction(
                    kind="remote branch",
                    message=f"delete remote review branch {bookmark}@{remote_name}",
                    status="applied" if apply else "planned",
                )
            )
            if apply:
                if remote_name is None or commit_id is None:
                    raise AssertionError("Planned remote branch deletion requires a target.")
                jj_client.delete_remote_bookmark(
                    remote=remote_name,
                    bookmark=bookmark,
                    expected_remote_target=commit_id,
                )

        if local_forget_planned:
            record_action(
                CloseAction(
                    kind="local bookmark",
                    message=f"forget local review bookmark {bookmark}",
                    status="applied" if apply else "planned",
                )
            )
            if apply:
                jj_client.forget_bookmark(bookmark)

    if cached_change.pr_number is None:
        return

    comment, comment_error = await _find_managed_stack_comment(
        github_client=github_client,
        github_repository=github_repository,
        pull_request_number=cached_change.pr_number,
        cached_stack_comment_id=cached_change.stack_comment_id,
    )
    if comment_error is not None:
        record_action(comment_error)
        return
    if comment is not None:
        record_action(
            CloseAction(
                kind="stack comment",
                message=(
                    f"delete managed stack comment #{comment.id} from PR "
                    f"#{cached_change.pr_number}"
                ),
                status="applied" if apply else "planned",
            )
        )
        if apply:
            await github_client.delete_issue_comment(
                github_repository.owner,
                github_repository.repo,
                comment_id=comment.id,
            )

    if cached_change.stack_comment_id is not None or comment is not None:
        next_changes[revision.change_id] = cached_change.model_copy(
            update={"stack_comment_id": None}
        )


async def _find_managed_stack_comment(
    *,
    cached_stack_comment_id: int | None,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> tuple[GithubIssueComment | None, CloseAction | None]:
    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404 and cached_stack_comment_id is not None:
            return (
                GithubIssueComment(
                    body="",
                    html_url="",
                    id=cached_stack_comment_id,
                ),
                None,
            )
        return (
            None,
            CloseAction(
                kind="stack comment",
                message=(
                    f"cannot inspect managed stack comments for PR #{pull_request_number}: "
                    f"{error}"
                ),
                status="blocked",
            ),
        )

    if cached_stack_comment_id is not None:
        cached_comment = next(
            (comment for comment in comments if comment.id == cached_stack_comment_id),
            None,
        )
        if cached_comment is not None:
            if _STACK_COMMENT_MARKER not in cached_comment.body:
                return (
                    None,
                    CloseAction(
                        kind="stack comment",
                        message=(
                            f"cannot delete cached stack comment #{cached_stack_comment_id} "
                            f"because it is not managed by `jj-review`"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None

    managed_comments = [
        comment for comment in comments if _STACK_COMMENT_MARKER in comment.body
    ]
    if len(managed_comments) > 1:
        return (
            None,
            CloseAction(
                kind="stack comment",
                message=(
                    "cannot delete managed stack comments because GitHub reports "
                    f"multiple candidates on PR #{pull_request_number}"
                ),
                status="blocked",
            ),
        )
    if not managed_comments:
        return None, None
    return managed_comments[0], None


def _retire_cached_change(
    cached_change: CachedChange,
    *,
    cleanup: bool,
    pr_state: str,
) -> CachedChange:
    updates: dict[str, object] = {
        "pr_review_decision": None,
        "pr_state": pr_state,
    }
    return cached_change.model_copy(update=updates)


def _close_cached_change(
    *,
    cached_change: CachedChange | None,
    revision,
) -> CachedChange | None:
    if cached_change is not None:
        return cached_change

    lookup = revision.pull_request_lookup
    if lookup is None or lookup.pull_request is None:
        return None

    return CachedChange(
        bookmark=revision.bookmark,
        pr_number=lookup.pull_request.number,
        pr_review_decision=getattr(lookup, "review_decision", None),
        pr_state=lookup.pull_request.state,
        pr_url=lookup.pull_request.html_url,
        stack_comment_id=(
            revision.stack_comment_lookup.comment.id
            if revision.stack_comment_lookup is not None
            and revision.stack_comment_lookup.state == "present"
            and revision.stack_comment_lookup.comment is not None
            else None
        ),
    )


def _has_active_cached_linkage(cached_change: CachedChange | None) -> bool:
    if cached_change is None:
        return False
    return cached_change.pr_state == "open"


def _revision_label(revision) -> str:
    return f"{revision.subject} [{revision.change_id[:8]}]"
