"""Close the GitHub pull requests for the selected stack.

Without `--apply`, this command shows what would be closed. With `--apply`, it
closes those pull requests, and `--cleanup` also removes jj-review's GitHub
branches and any local bookmarks for them.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review.cache import ReviewStateStore
from jj_review.command_ui import resolve_selected_revset
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github_resolution import _build_github_client
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
from jj_review.review_inspection import PreparedStatus, prepare_status, stream_status
from jj_review.stack_comments import is_stack_summary_comment

HELP = "Stop reviewing a jj stack on GitHub"

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


def close(
    *,
    apply: bool,
    cleanup: bool,
    config_path: Path | None,
    current: bool,
    debug: bool,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `close`."""

    from jj_review.bootstrap import bootstrap_context

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    prepared_close = prepare_close(
        apply=apply,
        cleanup=cleanup,
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=resolve_selected_revset(
            command_label=(
                "close --cleanup --apply"
                if apply and cleanup
                else (
                    "close --cleanup" if cleanup else "close --apply" if apply else "close"
                )
            ),
            current=current,
            require_explicit=True,
            revset=revset,
        ),
    )
    result = stream_close(prepared_close=prepared_close)
    print(f"Selected revset: {result.selected_revset}")
    if result.remote is None:
        if result.remote_error is None:
            print("Selected remote: unavailable")
        else:
            print(f"Selected remote: unavailable ({result.remote_error})")
    else:
        print(f"Selected remote: {result.remote.name}")

    if result.github_repository is None:
        if result.github_error is not None:
            print(f"GitHub target: unavailable ({result.github_error})")
    else:
        print(f"GitHub: {result.github_repository}")

    if result.actions:
        if result.blocked:
            header = "Close blocked:"
        elif result.applied:
            header = "Applied close actions:"
        else:
            header = "Planned close actions:"
        print(header)
        for action in result.actions:
            print(f"- [{action.status}] {action.kind}: {action.message}")
    else:
        if result.applied:
            print("No close actions were needed for the selected stack.")
        else:
            print("No open pull requests tracked by jj-review on the selected stack.")

    if not result.applied and not result.blocked and result.actions:
        print(
            f"Re-run with `{format_close_apply_command(result)}` "
            "to close the selected stack."
        )
    return 1 if result.blocked else 0


def format_close_apply_command(result: CloseResult) -> str:
    """Render the follow-up `close --apply` command for preview output."""

    parts = ["close", "--apply"]
    if result.cleanup:
        parts.append("--cleanup")
    if result.selected_revset:
        parts.append(result.selected_revset)
    return " ".join(parts)


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
        return _close_result(
            actions=(),
            blocked=False,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )

    if status_result.github_error is not None or github_repository is None:
        record_action(
            CloseAction(
                kind="close",
                message=(
                    "cannot close pull requests tracked by jj-review without live "
                    "GitHub state; "
                    "fix GitHub access and retry"
                ),
                status="blocked",
            )
        )
        return _close_result(
            actions=tuple(actions),
            applied=False,
            blocked=True,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )

    state_store = prepared.state_store
    current_state = state_store.load() if prepared_close.apply else prepared.state
    next_changes = dict(current_state.changes)
    commit_ids_by_change_id = {
        prepared_revision.revision.change_id: prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    }

    def save_progress() -> None:
        if prepared_close.apply and next_changes != current_state.changes:
            state_store.save(current_state.model_copy(update={"changes": next_changes}))

    completed = False

    def complete_result(result: CloseResult) -> CloseResult:
        nonlocal completed
        save_progress()
        completed = True
        return result

    intent: CloseIntent | None = None
    intent_path: Path | None = None
    stale_intents = []
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
                should_stop = await _process_close_revision(
                    commit_id=commit_ids_by_change_id.get(revision.change_id),
                    current_state=current_state,
                    github_client=github_client,
                    github_repository=github_repository,
                    next_changes=next_changes,
                    prepared_close=prepared_close,
                    record_action=record_action,
                    revision=revision,
                )
                if should_stop:
                    return complete_result(
                        _close_result(
                            actions=tuple(actions),
                            blocked=True,
                            github_error=status_result.github_error,
                            github_repository=github_repository,
                            prepared_close=prepared_close,
                        )
                    )

        return complete_result(
            _close_result(
                actions=tuple(actions),
                blocked=blocked,
                github_error=status_result.github_error,
                github_repository=github_repository,
                prepared_close=prepared_close,
            )
        )
    finally:
        if completed and intent_path is not None and intent is not None:
            retire_superseded_intents(stale_intents, intent)
            delete_intent(intent_path)


def _close_result(
    *,
    actions: tuple[CloseAction, ...],
    applied: bool | None = None,
    blocked: bool,
    github_error: str | None,
    github_repository,
    prepared_close: PreparedClose,
) -> CloseResult:
    prepared = prepared_close.prepared_status.prepared
    return CloseResult(
        actions=actions,
        applied=prepared_close.apply if applied is None else applied,
        blocked=blocked,
        cleanup=prepared_close.cleanup,
        github_error=github_error,
        github_repository=github_repository.full_name if github_repository else None,
        remote=prepared.remote,
        remote_error=prepared.remote_error,
        selected_revset=prepared_close.prepared_status.selected_revset,
    )


async def _process_close_revision(
    *,
    commit_id: str | None,
    current_state,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
) -> bool:
    lookup = revision.pull_request_lookup
    if lookup is None:
        return False
    if lookup.state in {"ambiguous", "error"}:
        record_action(
            CloseAction(
                kind="close",
                message=(
                    lookup.message
                    or "cannot safely determine the pull request for this path"
                ),
                status="blocked",
            )
        )
        return True

    cached_change = revision.cached_change or current_state.changes.get(revision.change_id)
    revision_label = _revision_label(revision)
    if lookup.state == "missing":
        return await _process_missing_close_revision(
            cached_change=cached_change,
            commit_id=commit_id,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=next_changes,
            prepared_close=prepared_close,
            record_action=record_action,
            revision=revision,
            revision_label=revision_label,
        )

    cached_change = _close_cached_change(
        cached_change=cached_change,
        revision=revision,
    )
    if cached_change is None:
        return False
    if lookup.state == "open" and lookup.pull_request is not None:
        await _process_open_close_revision(
            cached_change=cached_change,
            commit_id=commit_id,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=next_changes,
            prepared_close=prepared_close,
            pull_request_number=lookup.pull_request.number,
            record_action=record_action,
            revision=revision,
            revision_label=revision_label,
        )
        return False
    if lookup.state != "closed":
        return False

    await _process_closed_close_revision(
        cached_change=cached_change,
        commit_id=commit_id,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    return False


async def _process_missing_close_revision(
    *,
    cached_change: CachedChange | None,
    commit_id: str | None,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: str,
) -> bool:
    if _has_active_cached_link(cached_change):
        record_action(
            CloseAction(
                kind="close",
                message=(
                    f"cannot close {revision_label} because GitHub no longer reports a pull "
                    "request for its branch; run `status --fetch` or `relink` before "
                    "retrying"
                ),
                status="blocked",
            )
        )
        return True
    if not prepared_close.cleanup or cached_change is None:
        return False

    updated_change = _record_retired_cached_change(
        cached_change=cached_change,
        next_changes=next_changes,
        pr_state=cached_change.pr_state or "closed",
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    await _cleanup_if_requested(
        cached_change=updated_change,
        commit_id=commit_id,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    return False


async def _process_open_close_revision(
    *,
    cached_change: CachedChange,
    commit_id: str | None,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    pull_request_number: int,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: str,
) -> None:
    record_action(
        CloseAction(
            kind="pull request",
            message=f"close PR #{pull_request_number} for {revision_label}",
            status="applied" if prepared_close.apply else "planned",
        )
    )
    if prepared_close.apply:
        await github_client.close_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )

    updated_change = _record_retired_cached_change(
        cached_change=cached_change,
        next_changes=next_changes,
        pr_state="closed",
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    await _cleanup_if_requested(
        cached_change=updated_change,
        commit_id=commit_id,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )


async def _process_closed_close_revision(
    *,
    cached_change: CachedChange,
    commit_id: str | None,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: str,
) -> None:
    lookup = revision.pull_request_lookup
    pr_state = "merged" if (
        lookup is not None
        and lookup.pull_request is not None
        and lookup.pull_request.merged_at is not None
    ) else "closed"
    if cached_change.pr_state == "merged":
        pr_state = "merged"

    updated_change = _record_retired_cached_change(
        cached_change=cached_change,
        next_changes=next_changes,
        pr_state=pr_state,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )
    await _cleanup_if_requested(
        cached_change=updated_change,
        commit_id=commit_id,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        prepared_close=prepared_close,
        record_action=record_action,
        revision=revision,
        revision_label=revision_label,
    )


def _record_retired_cached_change(
    *,
    cached_change: CachedChange,
    next_changes: dict[str, CachedChange],
    pr_state: str,
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: str,
) -> CachedChange:
    updated_change = _retire_cached_change(cached_change, pr_state=pr_state)
    if updated_change != cached_change:
        next_changes[revision.change_id] = updated_change
        record_action(
            CloseAction(
                kind="tracking",
                message=f"stop saved jj-review tracking for {revision_label}",
                status="applied" if prepared_close.apply else "planned",
            )
        )
    return updated_change


async def _cleanup_if_requested(
    *,
    cached_change: CachedChange,
    commit_id: str | None,
    github_client: GithubClient,
    github_repository,
    next_changes: dict[str, CachedChange],
    prepared_close: PreparedClose,
    record_action: Callable[[CloseAction], None],
    revision,
    revision_label: str,
) -> None:
    if not prepared_close.cleanup:
        return
    prepared = prepared_close.prepared_status.prepared
    remote = prepared.remote
    await _cleanup_revision(
        apply=prepared_close.apply,
        bookmark_state=prepared.client.get_bookmark_state(revision.bookmark),
        cached_change=cached_change,
        github_client=github_client,
        github_repository=github_repository,
        next_changes=next_changes,
        record_action=record_action,
        jj_client=prepared.client,
        remote_name=remote.name if remote is not None else None,
        commit_id=commit_id,
        revision=revision,
        revision_label=revision_label,
    )


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
                        f"cannot forget local bookmark {bookmark!r} because it is "
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
                        f"cannot forget local bookmark {bookmark!r} because it "
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
                            f"cannot delete remote branch {bookmark}@{remote_name} "
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
                            f"cannot delete remote branch {bookmark}@{remote_name} "
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
                    message=f"delete remote branch {bookmark}@{remote_name}",
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
                    message=f"forget local bookmark {bookmark}",
                    status="applied" if apply else "planned",
                )
            )
            if apply:
                jj_client.forget_bookmark(bookmark)

    if cached_change.pr_number is None:
        return

    comment, comment_error = await _find_stack_summary_comment(
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
                kind="stack summary comment",
                message=(
                    f"delete stack summary comment #{comment.id} from PR "
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


async def _find_stack_summary_comment(
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
        if error.status_code == 404:
            if cached_stack_comment_id is None:
                return None, None
            try:
                cached_comment = await github_client.get_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=cached_stack_comment_id,
                )
            except GithubClientError as cached_comment_error:
                if cached_comment_error.status_code == 404:
                    return None, None
                return (
                    None,
                    CloseAction(
                        kind="stack summary comment",
                        message=(
                            "cannot inspect saved stack summary comment "
                            f"#{cached_stack_comment_id}: {cached_comment_error}"
                        ),
                        status="blocked",
                    ),
                )
            if not is_stack_summary_comment(cached_comment.body):
                return (
                    None,
                    CloseAction(
                        kind="stack summary comment",
                        message=(
                            f"cannot delete saved stack summary comment "
                            f"#{cached_stack_comment_id} because it does not belong to "
                            "`jj-review`"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None
        return (
            None,
            CloseAction(
                kind="stack summary comment",
                message=(
                    f"cannot inspect stack summary comments for PR #{pull_request_number}: "
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
            if not is_stack_summary_comment(cached_comment.body):
                return (
                    None,
                    CloseAction(
                        kind="stack summary comment",
                        message=(
                            f"cannot delete saved stack summary comment "
                            f"#{cached_stack_comment_id} because it does not belong to "
                            "`jj-review`"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None

    stack_summary_comments = [
        comment for comment in comments if is_stack_summary_comment(comment.body)
    ]
    if len(stack_summary_comments) > 1:
        return (
            None,
            CloseAction(
                kind="stack summary comment",
                message=(
                    "cannot delete stack summary comments because GitHub reports "
                    f"multiple candidates on PR #{pull_request_number}"
                ),
                status="blocked",
            ),
        )
    if not stack_summary_comments:
        return None, None
    return stack_summary_comments[0], None


def _retire_cached_change(
    cached_change: CachedChange,
    *,
    pr_state: str,
) -> CachedChange:
    # Closed changes remain "active" unless they were explicitly unlinked. The saved
    # jj-review data still needs the last known review identity so later cleanup or
    # status refresh can reason about the already-closed stack without reattaching it.
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


def _has_active_cached_link(cached_change: CachedChange | None) -> bool:
    if cached_change is None:
        return False
    return cached_change.pr_state == "open"


def _revision_label(revision) -> str:
    return f"{revision.subject} [{revision.change_id[:8]}]"
