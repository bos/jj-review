"""Close the GitHub pull requests for the selected stack.

By default, this closes those pull requests, and `--cleanup` also removes
jj-review's GitHub branches and any local bookmarks for them. Use `--dry-run`
to preview the close plan without mutating jj-review or GitHub state.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from string.templatelib import Template
from typing import Any, Literal

from jj_review import ui
from jj_review.bootstrap import bootstrap_context
from jj_review.cache import ReviewStateStore
from jj_review.command_ui import resolve_selected_revset
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import ErrorMessage
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.intent import (
    check_same_kind_intent,
    close_intent_mode_relation,
    describe_intent,
    match_close_intent,
    retire_superseded_intents,
    write_new_intent,
)
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubIssueComment
from jj_review.models.intent import CloseIntent, LoadedIntent
from jj_review.review_inspection import PreparedStatus, prepare_status, stream_status
from jj_review.stack_comments import is_stack_summary_comment

HELP = "Stop reviewing a jj stack on GitHub"

CloseActionStatus = Literal["applied", "blocked", "planned"]
type CloseActionBody = str | Template | ui.SemanticText | tuple[object, ...]


@dataclass(frozen=True, slots=True)
class CloseAction:
    """One close action that was planned, applied, or blocked."""

    kind: str
    status: CloseActionStatus
    body: CloseActionBody

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return ui.plain_text(self.body)


@dataclass(frozen=True, slots=True)
class CloseResult:
    """Rendered close result for the selected repository."""

    actions: tuple[CloseAction, ...]
    applied: bool
    blocked: bool
    cleanup: bool
    github_error: ErrorMessage | None
    github_repository: str | None
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    selected_revset: str


@dataclass(frozen=True, slots=True)
class PreparedClose:
    """Locally prepared close inputs before any GitHub mutation."""

    dry_run: bool
    cleanup: bool
    prepared_status: PreparedStatus


@dataclass(slots=True)
class _CloseActionRecorder:
    """Collect close actions and track whether any step blocked progress."""

    on_action: Callable[[CloseAction], None] | None
    actions: list[CloseAction] = field(default_factory=list)
    blocked: bool = False

    def record(self, action: CloseAction) -> None:
        if action.status == "blocked":
            self.blocked = True
        self.actions.append(action)
        if self.on_action is not None:
            self.on_action(action)

    def as_tuple(self) -> tuple[CloseAction, ...]:
        return tuple(self.actions)


@dataclass(frozen=True, slots=True)
class _CloseExecutionState:
    """Local saved state and commit lookup used during close execution."""

    current_state: ReviewState
    next_changes: dict[str, CachedChange]
    commit_ids_by_change_id: dict[str, str]


@dataclass(frozen=True, slots=True)
class _CloseIntentState:
    """Prepared close intent bookkeeping for resumable live runs."""

    intent: CloseIntent | None
    intent_path: Path | None
    stale_intents: list[LoadedIntent]


@dataclass(frozen=True, slots=True)
class _CloseCleanupContext:
    """Shared dependencies for bookmark and stack-comment cleanup."""

    dry_run: bool
    github_client: GithubClient
    github_repository: Any
    jj_client: JjClient
    next_changes: dict[str, CachedChange]
    record_action: Callable[[CloseAction], None]
    remote_name: str | None
    revision: Any
    revision_label: CloseActionBody


@dataclass(frozen=True, slots=True)
class _BookmarkCleanupPlan:
    """Resolved bookmark cleanup actions for one cached change."""

    local_forget: bool
    remote_delete: bool


def close(
    *,
    cleanup: bool,
    config_path: Path | None,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `close`."""

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    prepared_close = prepare_close(
        dry_run=dry_run,
        cleanup=cleanup,
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=resolve_selected_revset(
            command_label=(
                "close --cleanup --dry-run"
                if dry_run and cleanup
                else (
                    "close --cleanup"
                    if cleanup
                    else "close"
                    if not dry_run
                    else "close --dry-run"
                )
            ),
            default_revset="@-",
            require_explicit=False,
            revset=revset,
        ),
    )
    result = stream_close(prepared_close=prepared_close)
    ui.output(ui.rich_text(t"Selected revset: {ui.revset(result.selected_revset)}"))
    if result.remote is None:
        if result.remote_error is None:
            ui.note("Selected remote: unavailable")
        else:
            ui.warning(f"Selected remote: unavailable ({result.remote_error})")
    else:
        ui.output(f"Selected remote: {result.remote.name}")

    if result.github_repository is None:
        if result.github_error is not None:
            ui.warning(f"GitHub target: unavailable ({result.github_error})")
    else:
        ui.output(f"GitHub: {result.github_repository}")

    if result.actions:
        if result.blocked:
            header = "Close blocked:"
        elif result.applied:
            header = "Applied close actions:"
        else:
            header = "Planned close actions:"
        ui.output(header)
        for action in result.actions:
            prefix, prefix_style, body_style = _close_action_presentation(action.status)
            ui.output(
                ui.prefixed_message(
                    f"{prefix} ",
                    (ui.semantic_text(action.kind, "prefix"), ": ", action.body),
                    prefix_style=prefix_style,
                    message_style=body_style,
                )
            )
    else:
        if result.applied:
            ui.note("No close actions were needed for the selected stack.")
        else:
            ui.output("No open pull requests tracked by jj-review on the selected stack.")
    return 1 if result.blocked else 0


def prepare_close(
    *,
    dry_run: bool,
    change_overrides: dict[str, ChangeConfig],
    cleanup: bool,
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> PreparedClose:
    """Resolve local close inputs before any GitHub inspection."""

    state_store = ReviewStateStore.for_repo(repo_root)
    if not dry_run:
        state_store.require_writable()
    return PreparedClose(
        dry_run=dry_run,
        cleanup=cleanup,
        prepared_status=prepare_status(
            change_overrides=change_overrides,
            config=config,
            fetch_remote_state=not dry_run,
            persist_bookmarks=False,
            repo_root=repo_root,
            revset=revset,
        ),
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
    github_repository = prepared_status.github_repository

    recorder = _CloseActionRecorder(on_action=on_action)

    if not status_result.revisions:
        return _close_result(
            actions=(),
            blocked=False,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )

    if status_result.github_error is not None or github_repository is None:
        recorder.record(
            CloseAction(
                kind="close",
                body=(
                    "cannot close pull requests tracked by jj-review without live "
                    "GitHub state; "
                    "fix GitHub access and retry"
                ),
                status="blocked",
            )
        )
        return _close_result(
            actions=recorder.as_tuple(),
            applied=False,
            blocked=True,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )

    execution_state = _prepare_close_execution_state(prepared_close=prepared_close)
    completed = False
    intent_state = _CloseIntentState(
        intent=None,
        intent_path=None,
        stale_intents=[],
    )
    try:
        intent_state = _start_close_intent(
            prepared_close=prepared_close,
        )

        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            blocked = await _process_close_revisions(
                execution_state=execution_state,
                github_client=github_client,
                github_repository=github_repository,
                prepared_close=prepared_close,
                recorder=recorder,
                revisions=status_result.revisions,
            )

        _save_close_progress(
            execution_state=execution_state,
            prepared_close=prepared_close,
        )
        completed = True
        return _close_result(
            actions=recorder.as_tuple(),
            blocked=blocked or recorder.blocked,
            github_error=status_result.github_error,
            github_repository=github_repository,
            prepared_close=prepared_close,
        )
    finally:
        if completed and intent_state.intent_path is not None and intent_state.intent is not None:
            retire_superseded_intents(intent_state.stale_intents, intent_state.intent)
            intent_state.intent_path.unlink(missing_ok=True)


def _prepare_close_execution_state(*, prepared_close: PreparedClose) -> _CloseExecutionState:
    """Load local saved state and commit IDs once before close execution."""

    prepared_status = prepared_close.prepared_status
    prepared = prepared_status.prepared
    current_state = prepared.state_store.load() if not prepared_close.dry_run else prepared.state
    return _CloseExecutionState(
        current_state=current_state,
        next_changes=dict(current_state.changes),
        commit_ids_by_change_id={
            prepared_revision.revision.change_id: prepared_revision.revision.commit_id
            for prepared_revision in prepared_status.prepared.status_revisions
        },
    )


def _save_close_progress(
    *,
    execution_state: _CloseExecutionState,
    prepared_close: PreparedClose,
) -> None:
    """Persist saved close state when a live run changed tracked metadata."""

    prepared = prepared_close.prepared_status.prepared
    current_state = execution_state.current_state
    if not prepared_close.dry_run and execution_state.next_changes != current_state.changes:
        prepared.state_store.save(
            current_state.model_copy(update={"changes": execution_state.next_changes})
        )


def _start_close_intent(
    *,
    prepared_close: PreparedClose,
) -> _CloseIntentState:
    """Write close intent metadata for resumable live runs."""

    if prepared_close.dry_run:
        return _CloseIntentState(intent=None, intent_path=None, stale_intents=[])

    prepared_status = prepared_close.prepared_status
    state_dir = prepared_status.prepared.state_store.require_writable()
    ordered_revisions = tuple(
        prepared_revision.revision
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    ordered_change_ids = tuple(revision.change_id for revision in ordered_revisions)
    ordered_commit_ids = tuple(revision.commit_id for revision in ordered_revisions)
    intent = CloseIntent(
        kind="close",
        pid=os.getpid(),
        label=(
            ("close --cleanup" if prepared_close.cleanup else "close")
            + f" for {short_change_id(ordered_change_ids[-1])} "
            f"(from {prepared_status.selected_revset})"
        ),
        display_revset=prepared_status.selected_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        cleanup=prepared_close.cleanup,
        started_at=datetime.now(UTC).isoformat(),
    )
    stale_intents = check_same_kind_intent(state_dir, intent)
    _report_stale_close_intents(
        current_change_ids=ordered_change_ids,
        current_commit_ids=ordered_commit_ids,
        current_cleanup=prepared_close.cleanup,
        stale_intents=stale_intents,
    )
    return _CloseIntentState(
        intent=intent,
        intent_path=write_new_intent(state_dir, intent),
        stale_intents=stale_intents,
    )


def _report_stale_close_intents(
    *,
    current_change_ids: tuple[str, ...],
    current_commit_ids: tuple[str, ...],
    current_cleanup: bool,
    stale_intents: list[LoadedIntent],
) -> None:
    """Render interrupted close diagnostics for live execution."""

    for loaded in stale_intents:
        if not isinstance(loaded.intent, CloseIntent):
            continue
        mode_relation = close_intent_mode_relation(
            recorded_cleanup=loaded.intent.cleanup,
            current_cleanup=current_cleanup,
        )
        # mode-aware match: a recorded cleanup run is "disjoint" from a plain close
        match = match_close_intent(
            intent=loaded.intent,
            current_change_ids=current_change_ids,
            current_commit_ids=current_commit_ids,
            current_cleanup=current_cleanup,
        )
        # mode-blind stack match: used below to detect an incompatible-mode intent
        # whose stack still matches, so we can warn "plain close does not finish cleanup"
        stack_match = match_close_intent(
            intent=loaded.intent,
            current_change_ids=current_change_ids,
            current_commit_ids=current_commit_ids,
        )
        description = describe_intent(loaded.intent)
        if mode_relation == "same" and match == "exact":
            ui.note(f"Continuing interrupted {description}")
        elif mode_relation == "expanded" and match == "exact":
            ui.note(
                f"Interrupted {description} is covered by this close --cleanup run."
            )
        elif (
            mode_relation == "incompatible"
            and loaded.intent.cleanup
            and not current_cleanup
            and stack_match in {"exact", "same-logical", "covered"}
        ):
            ui.warning(
                f"Interrupted {description} is still outstanding; plain close "
                "does not finish cleanup. Run `close --cleanup` to complete it."
            )
        elif match == "same-logical":
            ui.note(
                f"Interrupted {description} targeted the same logical stack, "
                "but it has been rewritten. This close"
                f"{' --cleanup' if current_cleanup else ''} will use the current stack."
            )
        elif match == "covered":
            ui.note(
                f"Interrupted {description} targeted changes that are all included "
                "in the current stack. This close"
                f"{' --cleanup' if current_cleanup else ''} will use the current stack."
            )
        elif match == "overlap":
            ui.warning(
                f"This close overlaps an incomplete earlier operation "
                f"({description})"
            )
        else:
            ui.note(f"Incomplete operation outstanding: {description}")


async def _process_close_revisions(
    *,
    execution_state: _CloseExecutionState,
    github_client: GithubClient,
    github_repository,
    prepared_close: PreparedClose,
    recorder: _CloseActionRecorder,
    revisions,
) -> bool:
    """Process each revision in order, stopping on the first fail-closed block."""

    for revision in revisions:
        should_stop = await _process_close_revision(
            commit_id=execution_state.commit_ids_by_change_id.get(revision.change_id),
            current_state=execution_state.current_state,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=execution_state.next_changes,
            prepared_close=prepared_close,
            record_action=recorder.record,
            revision=revision,
        )
        if should_stop:
            return True
    return False


def _close_result(
    *,
    actions: tuple[CloseAction, ...],
    applied: bool | None = None,
    blocked: bool,
    github_error: ErrorMessage | None,
    github_repository,
    prepared_close: PreparedClose,
) -> CloseResult:
    prepared = prepared_close.prepared_status.prepared
    return CloseResult(
        actions=actions,
        applied=(not prepared_close.dry_run) if applied is None else applied,
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
                body=(
                    lookup.message or "cannot safely determine the pull request for this path"
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
    revision_label: CloseActionBody,
) -> bool:
    if _has_active_cached_link(cached_change):
        record_action(
            CloseAction(
                kind="close",
                body=(
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
    revision_label: CloseActionBody,
) -> None:
    record_action(
        CloseAction(
            kind="pull request",
            body=t"close PR #{pull_request_number} for {revision_label}",
            status="planned" if prepared_close.dry_run else "applied",
        )
    )
    if not prepared_close.dry_run:
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
    revision_label: CloseActionBody,
) -> None:
    lookup = revision.pull_request_lookup
    pr_state = (
        "merged"
        if (
            lookup is not None
            and lookup.pull_request is not None
            and lookup.pull_request.merged_at is not None
        )
        else "closed"
    )
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
    revision_label: CloseActionBody,
) -> CachedChange:
    updated_change = _retire_cached_change(cached_change, pr_state=pr_state)
    if updated_change != cached_change:
        next_changes[revision.change_id] = updated_change
        record_action(
            CloseAction(
                kind="tracking",
                body=t"stop saved jj-review tracking for {revision_label}",
                status="planned" if prepared_close.dry_run else "applied",
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
    revision_label: CloseActionBody,
) -> None:
    if not prepared_close.cleanup:
        return
    prepared = prepared_close.prepared_status.prepared
    remote = prepared.remote
    cleanup_context = _CloseCleanupContext(
        dry_run=prepared_close.dry_run,
        github_client=github_client,
        github_repository=github_repository,
        jj_client=prepared.client,
        next_changes=next_changes,
        record_action=record_action,
        remote_name=remote.name if remote is not None else None,
        revision=revision,
        revision_label=revision_label,
    )
    await _cleanup_revision(
        bookmark_state=prepared.client.get_bookmark_state(revision.bookmark),
        cached_change=cached_change,
        commit_id=commit_id,
        context=cleanup_context,
    )


async def _cleanup_revision(
    *,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    commit_id: str | None,
    context: _CloseCleanupContext,
) -> None:
    bookmark = cached_change.bookmark
    cleanup_plan = _plan_review_bookmark_cleanup(
        bookmark=bookmark,
        bookmark_state=bookmark_state,
        commit_id=commit_id,
        context=context,
    )
    _apply_review_bookmark_cleanup(
        bookmark=bookmark,
        commit_id=commit_id,
        context=context,
        cleanup_plan=cleanup_plan,
    )

    if cached_change.pr_number is None:
        return

    comment, comment_error = await _find_stack_summary_comment(
        github_client=context.github_client,
        github_repository=context.github_repository,
        pull_request_number=cached_change.pr_number,
        cached_stack_comment_id=cached_change.stack_comment_id,
    )
    if comment_error is not None:
        context.record_action(comment_error)
        return
    if comment is not None:
        context.record_action(
            CloseAction(
                kind="stack summary comment",
                body=(
                    f"delete stack summary comment #{comment.id} from PR "
                    f"#{cached_change.pr_number}"
                ),
                status="planned" if context.dry_run else "applied",
            )
        )
        if not context.dry_run:
            await context.github_client.delete_issue_comment(
                context.github_repository.owner,
                context.github_repository.repo,
                comment_id=comment.id,
            )

    if cached_change.stack_comment_id is not None or comment is not None:
        context.next_changes[context.revision.change_id] = cached_change.model_copy(
            update={"stack_comment_id": None}
        )


def _plan_review_bookmark_cleanup(
    *,
    bookmark: str | None,
    bookmark_state: BookmarkState,
    commit_id: str | None,
    context: _CloseCleanupContext,
) -> _BookmarkCleanupPlan:
    """Validate bookmark ownership and decide which bookmark mutations are safe."""

    if bookmark is None or not bookmark.startswith("review/"):
        return _BookmarkCleanupPlan(local_forget=False, remote_delete=False)

    local_forget = False
    remote_delete = False
    local_conflict = False
    remote_conflict = False
    local_target = bookmark_state.local_target
    branch_label = (
        f"{bookmark}@{context.remote_name}"
        if context.remote_name is not None
        else bookmark
    )

    if len(bookmark_state.local_targets) > 1:
        context.record_action(
            CloseAction(
                kind="local bookmark",
                body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
                status="blocked",
            )
        )
        local_conflict = True
    elif commit_id is not None and local_target is not None and local_target != commit_id:
        context.record_action(
            CloseAction(
                kind="local bookmark",
                body=t"cannot forget {ui.bookmark(bookmark)} because it already points "
                t"to a different revision",
                status="blocked",
            )
        )
        local_conflict = True
    elif commit_id is not None and local_target == commit_id:
        local_forget = True

    remote_state = (
        bookmark_state.remote_target(context.remote_name)
        if context.remote_name is not None
        else None
    )
    if remote_state is not None and context.remote_name is not None and commit_id is not None:
        if len(remote_state.targets) > 1:
            context.record_action(
                CloseAction(
                    kind="remote branch",
                    body=t"cannot delete {ui.bookmark(branch_label)} because the remote "
                    t"bookmark is conflicted",
                    status="blocked",
                )
            )
            remote_conflict = True
        elif remote_state.target != commit_id:
            context.record_action(
                CloseAction(
                    kind="remote branch",
                    body=t"cannot delete {ui.bookmark(branch_label)} because it already "
                    t"points to a different revision",
                    status="blocked",
                )
            )
            remote_conflict = True
        else:
            remote_delete = True

    if local_conflict:
        remote_delete = False
    if remote_conflict:
        local_forget = False
    return _BookmarkCleanupPlan(
        local_forget=local_forget,
        remote_delete=remote_delete,
    )


def _apply_review_bookmark_cleanup(
    *,
    bookmark: str | None,
    commit_id: str | None,
    context: _CloseCleanupContext,
    cleanup_plan: _BookmarkCleanupPlan,
) -> None:
    """Record and optionally execute validated bookmark cleanup mutations."""

    if bookmark is None:
        return

    if cleanup_plan.remote_delete:
        branch_label = (
            f"{bookmark}@{context.remote_name}"
            if context.remote_name is not None
            else bookmark
        )
        context.record_action(
            CloseAction(
                kind="remote branch",
                body=t"delete {ui.bookmark(branch_label)}",
                status="planned" if context.dry_run else "applied",
            )
        )
        if not context.dry_run:
            if context.remote_name is None or commit_id is None:
                raise AssertionError("Planned remote branch deletion requires a target.")
            context.jj_client.delete_remote_bookmarks(
                remote=context.remote_name,
                deletions=((bookmark, commit_id),),
            )

    if cleanup_plan.local_forget:
        context.record_action(
            CloseAction(
                kind="local bookmark",
                body=t"forget {ui.bookmark(bookmark)}",
                status="planned" if context.dry_run else "applied",
            )
        )
        if not context.dry_run:
            context.jj_client.forget_bookmarks((bookmark,))


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
                        body=(
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
                        body=(
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
                body=(
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
                        body=(
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
                body=(
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


def _revision_label(revision) -> Template:
    return t"{revision.subject} ({ui.change_id(revision.change_id)})"


def _close_action_presentation(
    status: CloseActionStatus,
) -> tuple[str, object | None, object | None]:
    if status == "applied":
        return (
            "  ✓",
            ui.semantic_style("signature status good"),
            ui.semantic_style("signature status good"),
        )
    if status == "planned":
        return (
            "  ~",
            ui.semantic_style("hint heading"),
            None,
        )
    if status == "blocked":
        return (
            "  ✗",
            ui.semantic_style("error heading"),
            ui.semantic_style("warning heading"),
        )
    return ("  ?", None, None)
