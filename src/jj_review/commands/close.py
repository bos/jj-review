"""Close the GitHub pull requests for the selected stack.

Passing `--cleanup` also removes `jj-review`'s own review branches, forgets any local bookmarks
that still point at those branches, and clears saved tracking data for the selected stack.

If you asked `jj-review` to use your own bookmarks with `submit --use-bookmarks`, those are
preserved unless `cleanup_user_bookmarks = true`. Use `--pull-request` to close by PR number or
URL.

Use `close --cleanup --pull-request <pr>` to retire an orphaned PR shown by `list`.

To preview the close plan without changing anything, use `--dry-run`.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.config import RepoConfig
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.error_messages import (
    github_unavailable_message,
    remote_unavailable_message,
    summarize_github_error_reason,
)
from jj_review.github.resolution import parse_github_repo, select_submit_remote
from jj_review.github.stack_comments import (
    StackCommentKind,
    is_navigation_comment,
    is_overview_comment,
    stack_comment_label,
)
from jj_review.jj import JjCliArgs, JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.github import GithubIssueComment, GithubPullRequest
from jj_review.models.intent import CloseIntent, LoadedIntent, SubmitIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.bookmarks import (
    bookmark_ownership_for_source,
    find_changes_by_bookmark,
    is_review_bookmark,
)
from jj_review.review.intents import (
    close_intent_mode_relation,
    describe_intent,
    match_close_intent,
    retire_superseded_intents,
)
from jj_review.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_orphaned_pull_request,
    resolve_selected_revset,
)
from jj_review.review.status import (
    PreparedRevision,
    PreparedStack,
    PreparedStatus,
    prepare_status,
    prepared_status_github_inspection_count,
    stream_status,
)
from jj_review.review.submit_recovery import (
    SubmitArtifactObservation,
    SubmitRecoveryIdentity,
    SubmitTargetRelation,
    observe_submit_artifacts,
    should_retire_submit_after_cleanup,
)
from jj_review.state.intents import check_same_kind_intent, write_new_intent
from jj_review.state.store import ReviewStateStore
from jj_review.system import pid_is_alive
from jj_review.ui import Message, plain_text

HELP = "Stop reviewing a jj stack on GitHub"

CloseActionStatus = Literal["applied", "blocked", "planned"]
OrphanedPullRequestState = Literal["closed", "open"]
type CloseActionBody = Message


@dataclass(frozen=True, slots=True)
class CloseAction:
    """One close action that was planned, applied, or blocked."""

    kind: str
    status: CloseActionStatus
    body: CloseActionBody

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


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

    config: RepoConfig
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
    stale_close_intents: list[LoadedIntent]
    stale_submit_intents: list[LoadedIntent]


@dataclass(frozen=True, slots=True)
class _CloseCleanupContext:
    """Shared dependencies for bookmark and stack-comment cleanup."""

    bookmark_prefix: str
    cleanup_user_bookmarks: bool
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


@dataclass(frozen=True, slots=True)
class _OrphanedPullRequestInspection:
    """Resolved GitHub view of one orphaned tracked pull request."""

    pull_request: GithubPullRequest
    state: OrphanedPullRequestState


@dataclass(frozen=True, slots=True)
class _ResolvedOrphanedComment:
    """One managed stack comment proven safe to delete during orphan cleanup."""

    comment: GithubIssueComment
    kind: StackCommentKind


def close(
    *,
    cleanup: bool,
    cli_args: JjCliArgs,
    debug: bool,
    dry_run: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `close`."""

    context = bootstrap_context(
        repository=repository,
        cli_args=cli_args,
        debug=debug,
    )
    command_label = (
        "close --cleanup --dry-run"
        if dry_run and cleanup
        else ("close --cleanup" if cleanup else "close" if not dry_run else "close --dry-run")
    )
    if pull_request is not None:
        if cleanup and revset is None:
            state_store = ReviewStateStore.for_repo(context.jj_client.repo_root)
            if not dry_run:
                state_store.require_writable()
            state = state_store.load()
            orphan_target = resolve_orphaned_pull_request(
                jj_client=context.jj_client,
                pull_request_reference=pull_request,
                state=state,
            )
            if orphan_target is not None:
                pull_request_number, change_id = orphan_target
                return asyncio.run(
                    _run_orphan_close(
                        change_id=change_id,
                        config=context.config,
                        dry_run=dry_run,
                        jj_client=context.jj_client,
                        pull_request_number=pull_request_number,
                        state=state,
                        state_store=state_store,
                    )
                )
        pull_request_number, resolved_revset = resolve_linked_change_for_pull_request(
            action_name="close",
            jj_client=context.jj_client,
            pull_request_reference=pull_request,
            revset=revset,
        )
        console.note(
            t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}"
        )
    else:
        resolved_revset = resolve_selected_revset(
            command_label=command_label,
            default_revset="@-",
            require_explicit=False,
            revset=revset,
        )

    with console.spinner(description="Inspecting jj stack"):
        prepared_close = prepare_close(
            dry_run=dry_run,
            cleanup=cleanup,
            config=context.config,
            jj_client=context.jj_client,
            revset=resolved_revset,
        )
    result = stream_close(prepared_close=prepared_close)
    if result.remote is None:
        console.warning(remote_unavailable_message(remote_error=result.remote_error))
    github_message = github_unavailable_message(
        github_error=result.github_error,
        github_repository=result.github_repository,
    )
    if github_message is not None:
        console.warning(github_message)
    if result.actions:
        if result.blocked:
            header = "Close blocked:"
        elif result.applied:
            header = "Applied close actions:"
        else:
            header = "Planned close actions:"
        console.output(header)
        for action in result.actions:
            prefix, prefix_style, body_style = _close_action_presentation(action.status)
            console.output(
                ui.prefixed_line(
                    f"{prefix} ",
                    _render_close_action_message(action),
                    prefix_labels=prefix_style,
                    message_labels=body_style,
                )
            )
    else:
        if result.applied:
            console.note("No close actions were needed for the selected stack.")
        else:
            console.output("Nothing to close on the selected stack.")
    return 1 if result.blocked else 0


def _orphan_should_cleanup_bookmark(
    *,
    bookmark: str,
    cached_change: CachedChange,
    cleanup_user_bookmarks: bool,
    prefix: str,
) -> bool:
    """Mirror _plan_review_bookmark_cleanup's ownership rule for the orphan path.

    A managed bookmark is only eligible when its name matches the configured
    review prefix. External bookmarks need an explicit user opt-in.
    """

    if cached_change.manages_bookmark:
        return is_review_bookmark(bookmark, prefix=prefix)
    return cleanup_user_bookmarks


async def _run_orphan_close(
    *,
    change_id: str,
    config: RepoConfig,
    dry_run: bool,
    jj_client: JjClient,
    pull_request_number: int,
    state: ReviewState,
    state_store: ReviewStateStore,
) -> int:
    """Close an orphaned PR, deleting its review artifacts via saved data.

    Saved tracking is the only available identity, so this path acts from the
    exact saved PR and bookmark fields. It fails closed if either is missing,
    if the saved bookmark is now claimed by another tracked record, or if the
    bookmark is locally or remotely conflicted.
    """

    cached_change = state.changes.get(change_id)
    if cached_change is None:
        raise CliError(
            t"PR #{pull_request_number} is no longer tracked locally."
        )
    bookmark = cached_change.bookmark
    if bookmark is None:
        raise CliError(
            t"PR #{pull_request_number} has no saved bookmark; cannot clean up orphaned branch.",
            hint=t"Run {ui.cmd('unlink')} to detach the saved record manually.",
        )
    other_claimants = tuple(
        other_change_id
        for other_change_id in find_changes_by_bookmark(state, bookmark)
        if other_change_id != change_id
    )
    if other_claimants:
        rendered_others = ", ".join(other[:8] for other in other_claimants)
        raise CliError(
            t"Bookmark {ui.bookmark(bookmark)} is now claimed by another tracked change "
            t"({rendered_others}); refusing to delete the branch from under a live review.",
            hint=t"Run {ui.cmd('unlink')} on the orphan record instead.",
        )

    remotes = jj_client.list_git_remotes()
    remote_error: ErrorMessage | None = None
    remote: GitRemote | None = None
    try:
        remote = select_submit_remote(remotes) if remotes else None
    except CliError as error:
        remote_error = error_message(error)
    github_repository = parse_github_repo(remote) if remote is not None else None
    github_error: ErrorMessage | None = None
    if remote is not None and github_repository is None:
        github_error = (
            f"Could not determine the GitHub repository for remote {remote.name}."
        )
    if remote is None or github_repository is None:
        if remote is None:
            console.warning(remote_unavailable_message(remote_error=remote_error))
        github_message = github_unavailable_message(
            github_error=github_error,
            github_repository=None,
        )
        if github_message is not None:
            console.warning(github_message)
        return 1

    label = ui.change_id(change_id)
    revision_label = t"orphaned change {label}"
    last_target = cached_change.last_submitted_commit_id
    cleanup_bookmark = _orphan_should_cleanup_bookmark(
        bookmark=bookmark,
        cached_change=cached_change,
        cleanup_user_bookmarks=config.cleanup_user_bookmarks,
        prefix=config.bookmark_prefix,
    )
    if cleanup_bookmark:
        jj_client.fetch_remote(remote=remote.name, branches=(bookmark,))
    bookmark_state = jj_client.get_bookmark_state(bookmark)
    recorder = _CloseActionRecorder(on_action=None)

    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        cleanup_context = _CloseCleanupContext(
            bookmark_prefix=config.bookmark_prefix,
            cleanup_user_bookmarks=config.cleanup_user_bookmarks,
            dry_run=dry_run,
            github_client=github_client,
            github_repository=github_repository,
            jj_client=jj_client,
            next_changes=dict(state.changes),
            record_action=recorder.record,
            remote_name=remote.name,
            revision=SimpleNamespace(change_id=change_id, bookmark=bookmark),
            revision_label=revision_label,
        )
        inspection, blocked_action = await _lookup_orphaned_pull_request(
            cached_change=cached_change,
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pull_request_number,
        )
        if blocked_action is not None:
            recorder.record(blocked_action)

        cleanup_plan = _BookmarkCleanupPlan(local_forget=False, remote_delete=False)
        resolved_comments: tuple[_ResolvedOrphanedComment, ...] = ()
        if not recorder.blocked and cleanup_bookmark:
            cleanup_plan = _preflight_orphan_bookmark_cleanup(
                bookmark=bookmark,
                bookmark_state=bookmark_state,
                cached_change=cached_change,
                cleanup_context=cleanup_context,
                recorder=recorder,
                saved_commit_id=last_target,
            )
        if not recorder.blocked:
            resolved_comments = await _preflight_orphaned_comment_cleanup(
                cached_change=cached_change,
                github_client=github_client,
                github_repository=github_repository,
                pull_request_number=pull_request_number,
                recorder=recorder,
            )
        if recorder.blocked:
            _retire_blocked_orphan_close_tracking(
                cached_change=cached_change,
                change_id=change_id,
                dry_run=dry_run,
                inspection=inspection,
                recorder=recorder,
                revision_label=revision_label,
                state=state,
                state_store=state_store,
            )
            return _render_orphan_close_actions(
                actions=recorder.as_tuple(),
                blocked=True,
                dry_run=dry_run,
            )

        if inspection is None:
            raise AssertionError("Orphan close inspection must resolve a pull request state.")
        if inspection.state == "open":
            recorder.record(
                CloseAction(
                    kind="pull request",
                    body=t"close PR #{pull_request_number} for orphaned change {label}",
                    status="planned" if dry_run else "applied",
                )
            )
            if not dry_run:
                try:
                    await github_client.close_pull_request(
                        github_repository.owner,
                        github_repository.repo,
                        pull_number=pull_request_number,
                    )
                except GithubClientError as error:
                    raise CliError(
                        t"Could not close PR #{pull_request_number}."
                    ) from error

        await _apply_orphaned_comment_cleanup(
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pull_request_number,
            recorder=recorder,
            resolved_comments=resolved_comments,
            dry_run=dry_run,
        )
        if cleanup_bookmark:
            _apply_review_bookmark_cleanup(
                bookmark=bookmark,
                commit_id=last_target,
                context=cleanup_context,
                cleanup_plan=cleanup_plan,
            )

    recorder.record(
        CloseAction(
            kind="saved data",
            body=t"prune orphan record for {label}",
            status="planned" if dry_run else "applied",
        )
    )
    if not dry_run:
        next_changes = dict(state.changes)
        next_changes.pop(change_id, None)
        state_store.save(state.model_copy(update={"changes": next_changes}))

    return _render_orphan_close_actions(
        actions=recorder.as_tuple(),
        blocked=recorder.blocked,
        dry_run=dry_run,
    )


def _render_orphan_close_actions(
    *,
    actions: tuple[CloseAction, ...],
    blocked: bool,
    dry_run: bool,
) -> int:
    header = (
        "Close blocked:"
        if blocked
        else ("Applied close actions:" if not dry_run else "Planned close actions:")
    )
    console.output(header)
    for action in actions:
        prefix, prefix_style, body_style = _close_action_presentation(action.status)
        console.output(
            ui.prefixed_line(
                f"{prefix} ",
                _render_close_action_message(action),
                prefix_labels=prefix_style,
                message_labels=body_style,
            )
        )
    return 1 if blocked else 0


def _retire_blocked_orphan_close_tracking(
    *,
    cached_change: CachedChange,
    change_id: str,
    dry_run: bool,
    inspection: _OrphanedPullRequestInspection | None,
    recorder: _CloseActionRecorder,
    revision_label: Message,
    state: ReviewState,
    state_store: ReviewStateStore,
) -> None:
    if inspection is None or inspection.state != "closed":
        return

    updated_change = _retire_cached_change(
        cached_change,
        pr_state=inspection.pull_request.state,
    )
    if updated_change == cached_change:
        return

    recorder.record(
        CloseAction(
            kind="tracking",
            body=t"mark {revision_label} as already {inspection.pull_request.state} on GitHub",
            status="planned" if dry_run else "applied",
        )
    )
    if not dry_run:
        next_changes = dict(state.changes)
        next_changes[change_id] = updated_change
        state_store.save(state.model_copy(update={"changes": next_changes}))


def _preflight_orphan_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    cleanup_context: _CloseCleanupContext,
    recorder: _CloseActionRecorder,
    saved_commit_id: str | None,
) -> _BookmarkCleanupPlan:
    remote_state = (
        bookmark_state.remote_target(cleanup_context.remote_name)
        if cleanup_context.remote_name is not None
        else None
    )
    if remote_state is None or not remote_state.targets:
        branch_label = (
            f"{bookmark}@{cleanup_context.remote_name}"
            if cleanup_context.remote_name is not None
            else bookmark
        )
        recorder.record(
            CloseAction(
                kind="remote branch",
                body=t"{ui.bookmark(branch_label)} already absent",
                status="planned" if cleanup_context.dry_run else "applied",
            )
        )
    if saved_commit_id is None:
        if bookmark_state.local_target is not None or bookmark_state.local_targets or (
            remote_state is not None and remote_state.targets
        ):
            recorder.record(
                CloseAction(
                    kind="close",
                    body=(
                        t"cannot clean up saved bookmark {ui.bookmark(bookmark)} "
                        t"without a saved submitted target"
                    ),
                    status="blocked",
                )
            )
        return _BookmarkCleanupPlan(local_forget=False, remote_delete=False)
    return _plan_review_bookmark_cleanup(
        bookmark=bookmark,
        cached_change=cached_change,
        cleanup_user_bookmarks=cleanup_context.cleanup_user_bookmarks,
        bookmark_state=bookmark_state,
        commit_id=saved_commit_id,
        context=cleanup_context,
    )


async def _preflight_orphaned_comment_cleanup(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
    recorder: _CloseActionRecorder,
) -> tuple[_ResolvedOrphanedComment, ...]:
    resolved_comments: list[_ResolvedOrphanedComment] = []
    for kind, cached_comment_id in (
        ("navigation", cached_change.navigation_comment_id),
        ("overview", cached_change.overview_comment_id),
    ):
        comment, comment_error = await _find_managed_comment(
            cached_comment_id=cached_comment_id,
            github_client=github_client,
            github_repository=github_repository,
            kind=kind,
            pull_request_number=pull_request_number,
        )
        if comment_error is not None:
            recorder.record(comment_error)
            return ()
        if comment is not None:
            resolved_comments.append(_ResolvedOrphanedComment(comment=comment, kind=kind))
    return tuple(resolved_comments)


async def _apply_orphaned_comment_cleanup(
    *,
    dry_run: bool,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
    recorder: _CloseActionRecorder,
    resolved_comments: tuple[_ResolvedOrphanedComment, ...],
) -> None:
    for resolved in resolved_comments:
        recorder.record(
            CloseAction(
                kind=stack_comment_label(resolved.kind),
                body=(
                    f"delete {stack_comment_label(resolved.kind)} #{resolved.comment.id} from "
                    f"PR #{pull_request_number}"
                ),
                status="planned" if dry_run else "applied",
            )
        )
        if not dry_run:
            try:
                await github_client.delete_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=resolved.comment.id,
                )
            except GithubClientError as error:
                if error.status_code != 404:
                    raise CliError(
                        t"Could not delete {stack_comment_label(resolved.kind)} "
                        t"#{resolved.comment.id}."
                    ) from error


async def _lookup_orphaned_pull_request(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> tuple[_OrphanedPullRequestInspection | None, CloseAction | None]:
    """Verify the saved PR identity and look for live duplicate branch claims.

    The saved PR number is the identity being retired, so REST lookup is the
    authority for whether that exact PR still exists and still names the saved
    bookmark. The head-ref lookup is only used to detect another open or closed
    PR claiming the same branch; merged PRs are intentionally ignored there
    because they are not live reviews the branch deletion would disturb.
    """

    bookmark = cached_change.bookmark
    if bookmark is None:
        return (
            None,
            CloseAction(
                kind="close",
                body="cannot inspect orphaned pull request without a saved bookmark identity",
                status="blocked",
            ),
        )

    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return (
                None,
                CloseAction(
                    kind="close",
                    body=t"PR #{pull_request_number} is no longer on GitHub",
                    status="blocked",
                ),
            )
        return None, _blocked_orphaned_close_github_action()
    inspection = _inspect_orphaned_pull_request_state(pull_request)
    if pull_request.head.ref != bookmark:
        return (
            inspection,
            CloseAction(
                kind="close",
                body=(
                    t"cannot close orphaned PR #{pull_request_number} because it no longer "
                    t"has saved bookmark {ui.bookmark(bookmark)} as its head ref"
                ),
                status="blocked",
            ),
        )

    try:
        branch_matches = await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=(bookmark,),
        )
    except GithubClientError:
        return None, _blocked_orphaned_close_github_action()

    other_live_matches = tuple(
        candidate
        for candidate in branch_matches.get(bookmark, ())
        if candidate.number != pull_request_number
    )
    if other_live_matches:
        return (
            inspection,
            CloseAction(
                kind="close",
                body=(
                    t"cannot close orphaned PR #{pull_request_number} because saved bookmark "
                    t"{ui.bookmark(bookmark)} now has multiple pull requests"
                ),
                status="blocked",
            ),
        )
    return (
        inspection,
        None,
    )


def _inspect_orphaned_pull_request_state(
    pull_request: GithubPullRequest,
) -> _OrphanedPullRequestInspection:
    if pull_request.state != "closed" or pull_request.merged_at is None:
        normalized_pull_request = pull_request
    else:
        normalized_pull_request = pull_request.model_copy(update={"state": "merged"})
    state: OrphanedPullRequestState = (
        "open" if normalized_pull_request.state == "open" else "closed"
    )
    return _OrphanedPullRequestInspection(
        pull_request=normalized_pull_request,
        state=state,
    )


def _blocked_orphaned_close_github_action() -> CloseAction:
    return CloseAction(
        kind="close",
        body=(
            "cannot close pull requests tracked by jj-review without live GitHub state; "
            "fix GitHub access and retry"
        ),
        status="blocked",
    )


def prepare_close(
    *,
    dry_run: bool,
    cleanup: bool,
    config: RepoConfig,
    jj_client: JjClient,
    revset: str | None,
) -> PreparedClose:
    """Resolve local close inputs before any GitHub inspection."""

    state_store = ReviewStateStore.for_repo(jj_client.repo_root)
    if not dry_run:
        state_store.require_writable()
    fast_path = _prepare_untracked_close_fast_path(jj_client=jj_client, revset=revset)
    if fast_path is not None:
        return PreparedClose(
            config=config,
            dry_run=dry_run,
            cleanup=cleanup,
            prepared_status=fast_path,
        )
    return PreparedClose(
        config=config,
        dry_run=dry_run,
        cleanup=cleanup,
        prepared_status=prepare_status(
            config=config,
            fetch_remote_state=cleanup,
            fetch_only_when_tracked=True,
            jj_client=jj_client,
            persist_bookmarks=False,
            revset=revset,
        ),
    )


def _prepare_untracked_close_fast_path(
    *,
    jj_client: JjClient,
    revset: str | None,
) -> PreparedStatus | None:
    """Build the no-op close path without bookmark discovery.

    Both plain `close` and `close --cleanup` are true no-ops when the selected
    stack has no saved review identity at all. In that case we can skip
    bookmark-state discovery and GitHub preparation while still preserving the
    normal remote diagnostics and stale-intent retirement behavior.
    """

    client = jj_client
    stack = client.discover_review_stack(
        revset,
        allow_divergent=True,
        allow_immutable=True,
    )
    state_store = ReviewStateStore.for_repo(jj_client.repo_root)
    state = state_store.load()

    status_revisions: list[PreparedRevision] = []
    for revision in stack.revisions:
        cached_change = state.changes.get(revision.change_id)
        if cached_change is not None and cached_change.has_review_identity:
            return None
        status_revisions.append(
            PreparedRevision(
                bookmark=(cached_change.bookmark or "") if cached_change is not None else "",
                bookmark_source="generated",
                cached_change=cached_change,
                revision=revision,
            )
        )

    remotes = client.list_git_remotes()
    remote: GitRemote | None = None
    remote_error: ErrorMessage | None = None
    if remotes:
        try:
            remote = select_submit_remote(remotes)
        except CliError as error:
            remote_error = error_message(error)

    github_repository = None
    github_repository_error = None
    if remote is not None:
        github_repository = parse_github_repo(remote)
        if github_repository is None:
            github_repository_error = (
                t"Could not determine the GitHub repository for remote "
                t"{ui.bookmark(remote.name)}. Use a GitHub remote URL."
            )

    prepared = PreparedStack(
        bookmark_states={},
        bookmark_result_changed=False,
        client=client,
        remote=remote,
        remote_error=remote_error,
        stack=stack,
        state=state,
        state_changes=dict(state.changes),
        state_store=state_store,
        status_revisions=tuple(status_revisions),
    )
    return PreparedStatus(
        github_repository=github_repository,
        github_repository_error=github_repository_error,
        outstanding_intents=(),
        prepared=prepared,
        selected_revset=stack.selected_revset,
        stale_intents=(),
        base_parent_subject=stack.base_parent.subject,
    )


def stream_close(
    *,
    prepared_close: PreparedClose,
    on_action: Callable[[CloseAction], None] | None = None,
) -> CloseResult:
    """Inspect GitHub state for prepared close inputs and optionally stream actions."""

    prepared_status = prepared_close.prepared_status
    progress_total = prepared_status_github_inspection_count(
        prepared_status=prepared_status,
    )
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = stream_status(
            persist_cache_updates=False,
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
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

    no_work = _inspected_close_has_no_work(
        prepared_close=prepared_close,
        revisions=status_result.revisions,
    )

    if not no_work and (status_result.github_error is not None or github_repository is None):
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
        stale_close_intents=[],
        stale_submit_intents=[],
    )
    try:
        intent_state = _start_close_intent(
            prepared_close=prepared_close,
        )

        if no_work:
            completed = True
            return _close_result(
                actions=(),
                applied=False,
                blocked=False,
                github_error=status_result.github_error,
                github_repository=github_repository,
                prepared_close=prepared_close,
            )

        assert github_repository is not None
        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            progress_total = len(status_result.revisions) if on_action is None else 0
            with console.progress(
                description="Processing close actions",
                total=progress_total,
            ) as progress:
                blocked = await _process_close_revisions(
                    execution_state=execution_state,
                    github_client=github_client,
                    github_repository=github_repository,
                    on_revision_complete=progress.advance,
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
            retire_superseded_intents(intent_state.stale_close_intents, intent_state.intent)
            if prepared_close.cleanup and not recorder.blocked:
                _retire_submit_intents_cleared_by_cleanup(
                    current_state=execution_state.current_state.model_copy(
                        update={"changes": execution_state.next_changes}
                    ),
                    jj_client=prepared_status.prepared.client,
                    stale_submit_intents=intent_state.stale_submit_intents,
                )
            intent_state.intent_path.unlink(missing_ok=True)


def _retire_submit_intents_cleared_by_cleanup(
    *,
    current_state: ReviewState,
    jj_client: JjClient,
    stale_submit_intents: list[LoadedIntent],
) -> None:
    """Retire interrupted submits whose review artifacts were fully cleared."""

    for loaded in stale_submit_intents:
        if not isinstance(loaded.intent, SubmitIntent):
            continue
        if should_retire_submit_after_cleanup(
            observation=_observe_submit_artifacts(
                current_state=current_state,
                intent=loaded.intent,
                jj_client=jj_client,
            )
        ):
            loaded.path.unlink(missing_ok=True)


def _observe_submit_artifacts(
    *,
    current_state: ReviewState,
    intent: SubmitIntent,
    jj_client: JjClient,
) -> SubmitArtifactObservation:
    """Collect the live artifact state for a recorded submit intent."""

    remotes_by_name = {remote.name: remote for remote in jj_client.list_git_remotes()}
    recorded_remote = remotes_by_name.get(intent.remote_name)
    if recorded_remote is None:
        target_relation = SubmitTargetRelation.UNKNOWN
    else:
        current_github_repository = parse_github_repo(recorded_remote)
        target_relation = (
            SubmitTargetRelation.MATCH
            if SubmitRecoveryIdentity.from_intent(intent)
            == SubmitRecoveryIdentity.from_github_repository(
                remote_name=intent.remote_name,
                github_repository=current_github_repository,
            )
            else SubmitTargetRelation.MISMATCH
        )

    return observe_submit_artifacts(
        current_changes=current_state.changes,
        intent=intent,
        bookmark_states={
            bookmark: jj_client.get_bookmark_state(bookmark)
            for bookmark in intent.bookmarks.values()
        },
        target_relation=target_relation,
    )


def _inspected_close_has_no_work(
    *,
    prepared_close: PreparedClose,
    revisions,
) -> bool:
    """Whether close has nothing to do for the inspected revisions.

    Both plain close and cleanup only act on changes jj-review tracks: closing
    a linked pull request, forgetting a bookmark we saved, deleting a remote
    branch we pushed. None of those exist for a change without review
    identity, so either variant is a true no-op on such a stack. A
    config-pinned bookmark without review identity is intentionally ignored --
    we never pushed that branch and must not delete it.
    """

    del prepared_close  # unused; same predicate for plain and cleanup
    for revision in revisions:
        cached = revision.cached_change
        if cached is not None and cached.has_review_identity:
            return False
    return True


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
        return _CloseIntentState(
            intent=None,
            intent_path=None,
            stale_close_intents=[],
            stale_submit_intents=[],
        )

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
    stale_close_intents = check_same_kind_intent(state_dir, intent)
    _report_stale_close_intents(
        current_change_ids=ordered_change_ids,
        current_commit_ids=ordered_commit_ids,
        current_cleanup=prepared_close.cleanup,
        stale_intents=stale_close_intents,
    )
    stale_submit_intents = (
        [
            loaded
            for loaded in prepared_status.prepared.state_store.list_intents()
            if isinstance(loaded.intent, SubmitIntent) and not pid_is_alive(loaded.intent.pid)
        ]
        if prepared_close.cleanup
        else []
    )
    return _CloseIntentState(
        intent=intent,
        intent_path=write_new_intent(state_dir, intent),
        stale_close_intents=stale_close_intents,
        stale_submit_intents=stale_submit_intents,
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
            console.note(f"Continuing interrupted {description}")
        elif mode_relation == "expanded" and match == "exact":
            console.note(
                t"Interrupted {description} is covered by this "
                t"{ui.cmd('close --cleanup')} run."
            )
        elif (
            mode_relation == "incompatible"
            and loaded.intent.cleanup
            and not current_cleanup
            and stack_match in {"exact", "same-logical", "covered"}
        ):
            console.warning(
                t"Interrupted {description} is still outstanding; plain close "
                t"does not finish cleanup. Run {ui.cmd('close --cleanup')} to complete it."
            )
        elif match == "same-logical":
            console.note(
                t"Interrupted {description} targeted the same logical stack, but it "
                t"has been rewritten. This "
                t"{ui.cmd('close --cleanup' if current_cleanup else 'close')} "
                t"will use the current stack."
            )
        elif match == "covered":
            console.note(
                t"Interrupted {description} targeted changes that are all included "
                t"in the current stack. This "
                t"{ui.cmd('close --cleanup' if current_cleanup else 'close')} "
                t"will use the current stack."
            )
        elif match == "overlap":
            console.warning(
                t"This {ui.cmd('close --cleanup' if current_cleanup else 'close')} "
                t"overlaps an incomplete earlier operation ({description})"
            )
        else:
            console.note(f"Incomplete operation outstanding: {description}")


async def _process_close_revisions(
    *,
    execution_state: _CloseExecutionState,
    github_client: GithubClient,
    github_repository,
    on_revision_complete: Callable[[], None] | None,
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
        if on_revision_complete is not None:
            on_revision_complete()
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
                body=(lookup.message or "cannot safely determine the pull request for this path"),
                status="blocked",
            )
        )
        return True

    cached_change = revision.cached_change or current_state.changes.get(revision.change_id)
    revision_label = t"{revision.subject} ({ui.change_id(revision.change_id)})"
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

    if cached_change is None:
        if lookup.pull_request is None:
            return False
        cached_change = CachedChange(
            bookmark=revision.bookmark,
            bookmark_ownership=bookmark_ownership_for_source(revision.bookmark_source),
            pr_number=lookup.pull_request.number,
            pr_state=lookup.pull_request.state,
            pr_url=lookup.pull_request.html_url,
            navigation_comment_id=(
                revision.managed_comments_lookup.navigation_comment.id
                if revision.managed_comments_lookup is not None
                and revision.managed_comments_lookup.state == "resolved"
                and revision.managed_comments_lookup.navigation_comment is not None
                else None
            ),
            overview_comment_id=(
                revision.managed_comments_lookup.overview_comment.id
                if revision.managed_comments_lookup is not None
                and revision.managed_comments_lookup.state == "resolved"
                and revision.managed_comments_lookup.overview_comment is not None
                else None
            ),
        )
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
    if cached_change is not None and cached_change.pr_state == "open":
        record_action(
            CloseAction(
                kind="close",
                body=(
                    t"cannot close {revision_label} because GitHub no longer reports a "
                    t"pull request for its branch; run {ui.cmd('status --fetch')} or "
                    t"{ui.cmd('relink')} before retrying"
                ),
                status="blocked",
            )
        )
        return True
    if (
        not prepared_close.cleanup
        or cached_change is None
        or not _has_retirable_cached_review_identity(cached_change)
    ):
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
                body=t"stop review tracking for {revision_label}",
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
        bookmark_prefix=prepared_close.config.bookmark_prefix,
        cleanup_user_bookmarks=prepared_close.config.cleanup_user_bookmarks,
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
        cached_change=cached_change,
        cleanup_user_bookmarks=context.cleanup_user_bookmarks,
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

    cleared_comment = False
    for kind, cached_comment_id in (
        ("navigation", cached_change.navigation_comment_id),
        ("overview", cached_change.overview_comment_id),
    ):
        comment, comment_error = await _find_managed_comment(
            cached_comment_id=cached_comment_id,
            github_client=context.github_client,
            github_repository=context.github_repository,
            kind=kind,
            pull_request_number=cached_change.pr_number,
        )
        if comment_error is not None:
            context.record_action(comment_error)
            return
        if comment is None:
            continue
        cleared_comment = True
        context.record_action(
            CloseAction(
                kind=stack_comment_label(kind),
                body=(
                    f"delete {stack_comment_label(kind)} #{comment.id} from PR "
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

    if (
        cached_change.navigation_comment_id is not None
        or cached_change.overview_comment_id is not None
        or cleared_comment
    ):
        context.next_changes[context.revision.change_id] = cached_change.model_copy(
            update={
                "navigation_comment_id": None,
                "overview_comment_id": None,
            }
        )


def _plan_review_bookmark_cleanup(
    *,
    bookmark: str | None,
    cached_change: CachedChange,
    cleanup_user_bookmarks: bool,
    bookmark_state: BookmarkState,
    commit_id: str | None,
    context: _CloseCleanupContext,
) -> _BookmarkCleanupPlan:
    """Validate bookmark ownership and decide which bookmark mutations are safe."""

    if bookmark is None:
        return _BookmarkCleanupPlan(local_forget=False, remote_delete=False)
    if cached_change.manages_bookmark:
        if not is_review_bookmark(bookmark, prefix=context.bookmark_prefix):
            return _BookmarkCleanupPlan(local_forget=False, remote_delete=False)
    elif not cleanup_user_bookmarks:
        return _BookmarkCleanupPlan(local_forget=False, remote_delete=False)

    local_forget = False
    remote_delete = False
    local_conflict = False
    remote_conflict = False
    local_target = bookmark_state.local_target
    branch_label = (
        f"{bookmark}@{context.remote_name}" if context.remote_name is not None else bookmark
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
            f"{bookmark}@{context.remote_name}" if context.remote_name is not None else bookmark
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


def _comment_matches_kind(*, body: str, kind: StackCommentKind) -> bool:
    if kind == "navigation":
        return is_navigation_comment(body)
    return is_overview_comment(body)


async def _find_managed_comment(
    *,
    cached_comment_id: int | None,
    github_client: GithubClient,
    github_repository,
    kind: StackCommentKind,
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
            if cached_comment_id is None:
                return None, None
            try:
                cached_comment = await github_client.get_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=cached_comment_id,
                )
            except GithubClientError as cached_comment_error:
                if cached_comment_error.status_code == 404:
                    return None, None
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot inspect saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id}: "
                            f"{summarize_github_error_reason(cached_comment_error)}"
                        ),
                        status="blocked",
                    ),
                )
            if not _comment_matches_kind(body=cached_comment.body, kind=kind):
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot delete saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id} because it does not belong to "
                            "jj-review"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None
        return (
            None,
            CloseAction(
                kind=stack_comment_label(kind),
                body=(
                    f"cannot inspect {stack_comment_label(kind)}s for PR "
                    f"#{pull_request_number}: "
                    f"{summarize_github_error_reason(error)}"
                ),
                status="blocked",
            ),
        )

    if cached_comment_id is not None:
        cached_comment = next(
            (comment for comment in comments if comment.id == cached_comment_id),
            None,
        )
        if cached_comment is not None:
            if not _comment_matches_kind(body=cached_comment.body, kind=kind):
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot delete saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id} because it does not belong to "
                            "jj-review"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None

    matching_comments = [
        comment
        for comment in comments
        if _comment_matches_kind(body=comment.body, kind=kind)
    ]
    if len(matching_comments) > 1:
        return (
            None,
            CloseAction(
                kind=stack_comment_label(kind),
                body=(
                    f"cannot delete {stack_comment_label(kind)}s because GitHub reports "
                    f"multiple candidates on PR #{pull_request_number}"
                ),
                status="blocked",
            ),
        )
    if not matching_comments:
        return None, None
    return matching_comments[0], None


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


def _has_retirable_cached_review_identity(cached_change: CachedChange) -> bool:
    """Return True when saved state proves this change previously had review identity."""

    return any(
        value is not None
        for value in (
            cached_change.last_submitted_commit_id,
            cached_change.pr_number,
            cached_change.pr_state,
            cached_change.pr_url,
            cached_change.navigation_comment_id,
            cached_change.overview_comment_id,
        )
    )


def _render_close_action_message(action: CloseAction) -> CloseActionBody:
    if action.kind == "tracking":
        return action.body
    return (ui.semantic_text(action.kind, "prefix"), ": ", action.body)


def _close_action_presentation(
    status: CloseActionStatus,
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...] | None]:
    if status == "applied":
        return (
            "  ✓",
            ("signature status good",),
            None,
        )
    if status == "planned":
        return (
            "  ~",
            ("hint heading",),
            None,
        )
    if status == "blocked":
        return (
            "  ✗",
            ("error heading",),
            ("warning heading",),
        )
    return ("  ?", None, None)
