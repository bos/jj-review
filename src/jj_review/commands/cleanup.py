"""Find and remove stale review branches and tracking data left behind by
earlier review work.

By default, this removes safe stale data repo-wide, and with `--restack` it
can also rebase local descendants above changes that have already been merged.
Use `--dry-run` to preview those actions without mutating local or remote
state.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    select_submit_remote,
)
from jj_review.github.stack_comments import is_stack_summary_comment
from jj_review.jj import JjClient
from jj_review.jj.client import UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.github import GithubIssueComment
from jj_review.models.intent import CleanupIntent, CleanupRestackIntent, LoadedIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.bookmarks import bookmark_glob, is_review_bookmark
from jj_review.review.intents import (
    describe_intent,
    match_cleanup_restack_intent,
    retire_superseded_intents,
)
from jj_review.review.selection import resolve_selected_revset
from jj_review.review.status import (
    PreparedStatus,
    ReviewStatusRevision,
    prepare_status,
    prepared_status_github_inspection_count,
    revision_has_merged_pull_request,
    revision_pull_request_number,
    status_preparation_cli_error,
    stream_status,
)
from jj_review.state.intents import check_same_kind_intent, write_new_intent
from jj_review.state.store import ReviewStateStore
from jj_review.ui import Message, plain_text

HELP = "Clean up stale jj-review data for a jj stack"

CleanupActionStatus = Literal["applied", "blocked", "planned"]
type StackCommentCleanupEligibility = Literal["inspect", "needs-remote-check", "skip"]
type CleanupBody = Message
_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY


@dataclass(frozen=True, slots=True)
class CleanupAction:
    """One cleanup action that was planned, applied, or blocked."""

    kind: str
    status: CleanupActionStatus
    body: CleanupBody

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Rendered cleanup result for the selected repository."""

    actions: tuple[CleanupAction, ...]


@dataclass(frozen=True, slots=True)
class PreparedCleanup:
    """Locally prepared cleanup inputs before any GitHub inspection."""

    config: RepoConfig
    dry_run: bool
    bookmark_states: dict[str, BookmarkState]
    github_repository: ParsedGithubRepo | None
    github_repository_error: ErrorMessage | None
    jj_client: JjClient
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    remote_context_loaded: bool
    state: ReviewState
    state_store: ReviewStateStore


@dataclass(frozen=True, slots=True)
class StackCommentCleanupPlan:
    """Planned or blocked stack-comment cleanup details."""

    action: CleanupAction
    comment_id: int | None = None


@dataclass(frozen=True, slots=True)
class RemoteBranchCleanupPlan:
    """Planned or blocked remote-branch cleanup details."""

    action: CleanupAction
    expected_remote_target: str | None = None


@dataclass(frozen=True, slots=True)
class OrphanLocalBookmarkCleanupPlan:
    """Planned or blocked cleanup for one untracked local review bookmark."""

    action: CleanupAction
    bookmark: str


@dataclass(frozen=True, slots=True)
class PreparedCleanupChange:
    """Locally prepared cleanup state for one cached change."""

    bookmark_state: BookmarkState
    cached_change: CachedChange
    change_id: str
    inspect_stack_comment: bool
    stale_reason: str | None


@dataclass(frozen=True, slots=True)
class _StaleCleanupMutationPlan:
    """Planned local bookmark and remote branch mutations for one stale change."""

    cached_change: CachedChange
    local_bookmark_action: CleanupAction | None
    remote_plan: RemoteBranchCleanupPlan | None


@dataclass(frozen=True, slots=True)
class RestackResult:
    """Rendered restack result for one selected local stack."""

    actions: tuple[CleanupAction, ...]
    blocked: bool


@dataclass(frozen=True, slots=True)
class PreparedRestack:
    """Locally prepared restack inputs before any rewrite."""

    config: RepoConfig
    dry_run: bool
    prepared_status: PreparedStatus


@dataclass(frozen=True, slots=True)
class _RestackOperationPlan:
    """Derived restack planning data before preview/live rendering."""

    blocked: bool
    closed_unmerged_revisions: tuple[ReviewStatusRevision, ...]
    merged_revisions: tuple[ReviewStatusRevision, ...]
    pre_actions: tuple[CleanupAction, ...]
    rebase_plans: tuple[tuple[str, str | None], ...]


@dataclass(frozen=True, slots=True)
class _RestackIntentState:
    """Prepared restack intent bookkeeping for resumable live runs."""

    intent: CleanupRestackIntent | None
    intent_path: Path | None
    stale_intents: list[LoadedIntent]


def _render_cleanup_action_header(*, dry_run: bool) -> str:
    """Render the cleanup action section header."""

    return "Planned cleanup actions:" if dry_run else "Applied cleanup actions:"


def _render_cleanup_postamble(*, result: CleanupResult) -> tuple[str, ...]:
    """Render cleanup lines that only depend on the completed result."""

    if not result.actions:
        return ("No cleanup actions needed.",)
    return ()


def _render_restack_preamble(*, prepared_restack: PreparedRestack) -> tuple[tuple[str, str], ...]:
    """Render the non-streaming restack context lines for the CLI."""

    prepared_status = prepared_restack.prepared_status
    prepared = prepared_status.prepared
    return _render_remote_and_github_lines(
        remote=prepared.remote,
        remote_error=prepared.remote_error,
        github_repository=(
            prepared_status.github_repository.full_name
            if prepared_status.github_repository is not None
            else None
        ),
        github_error=prepared_status.github_repository_error,
    )


def _render_restack_action_header(*, dry_run: bool) -> str:
    """Render the restack action section header."""

    return "Planned restack actions:" if dry_run else "Applied restack actions:"


def _render_restack_postamble(*, result: RestackResult) -> tuple[str, ...]:
    """Render restack lines that only depend on the completed result."""

    if not result.actions:
        return ("No merged changes on the selected stack need restacking.",)
    return ()


def cleanup(
    *,
    config_path: Path | None,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
    restack: bool,
    revset: str | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    if restack:
        return _run_cleanup_restack_command(
            dry_run=dry_run,
            change_overrides=context.config.change,
            config=context.config,
            repo_root=context.repo_root,
            revset=revset,
        )

    return _run_cleanup_command(
        config=context.config,
        dry_run=dry_run,
        repo_root=context.repo_root,
    )


def _run_cleanup_restack_command(
    *,
    dry_run: bool,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> int:
    """Render and run the `cleanup --restack` command path."""

    selected_revset = resolve_selected_revset(
        command_label="cleanup --restack --dry-run" if dry_run else "cleanup --restack",
        default_revset="@-",
        require_explicit=False,
        revset=revset,
    )
    try:
        prepared_restack = PreparedRestack(
            config=config,
            dry_run=dry_run,
            prepared_status=prepare_status(
                change_overrides=change_overrides,
                config=config,
                fetch_remote_state=True,
                fetch_only_when_tracked=True,
                repo_root=repo_root,
                revset=selected_revset,
            ),
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    for severity, line in _render_restack_preamble(prepared_restack=prepared_restack):
        if severity == "warning":
            console.warning(line)
        else:
            console.output(line)

    try:
        result = _stream_restack(
            on_action=_build_action_streamer(
                dry_run=prepared_restack.dry_run,
                render_header=_render_restack_action_header,
            ),
            prepared_restack=prepared_restack,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    for line in _render_restack_postamble(result=result):
        console.output(line)
    return 1 if result.blocked else 0


def _run_cleanup_command(
    *,
    config: RepoConfig,
    dry_run: bool,
    repo_root: Path,
) -> int:
    """Render and run the stale cleanup command path."""

    prepared_cleanup = _prepare_cleanup(
        config=config,
        dry_run=dry_run,
        repo_root=repo_root,
    )
    stale_reasons = _stale_change_reasons(
        change_ids=tuple(prepared_cleanup.state.changes),
        jj_client=prepared_cleanup.jj_client,
    )
    if _cleanup_needs_remote_context(
        prepared_cleanup=prepared_cleanup,
        stale_reasons=stale_reasons,
    ):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        for severity, line in _render_remote_and_github_lines(
            remote=prepared_cleanup.remote,
            remote_error=prepared_cleanup.remote_error,
            github_repository=(
                prepared_cleanup.github_repository.full_name
                if prepared_cleanup.github_repository is not None
                else None
            ),
            github_error=prepared_cleanup.github_repository_error,
        ):
            if severity == "warning":
                console.warning(line)
            else:
                console.output(line)

    result = asyncio.run(
        _run_cleanup_async(
            on_action=_build_action_streamer(
                dry_run=prepared_cleanup.dry_run,
                render_header=_render_cleanup_action_header,
            ),
            prepared_cleanup=prepared_cleanup,
            stale_reasons=stale_reasons,
        )
    )
    for line in _render_cleanup_postamble(result=result):
        console.output(line)
    return 0


def _build_action_streamer(
    *,
    dry_run: bool,
    render_header: Callable[..., str],
) -> Callable[[CleanupAction], None]:
    """Print the action header once, then stream actions as they arrive."""

    header_printed = False

    def emit_action(action: CleanupAction) -> None:
        nonlocal header_printed
        if not header_printed:
            console.output(render_header(dry_run=dry_run))
            header_printed = True
        prefix, prefix_style, body_style = _action_presentation(action.status)
        body = action.body
        if action.kind != "tracking":
            body = (ui.semantic_text(action.kind, "prefix"), ": ", body)
        console.output(
            ui.prefixed_line(
                f"{prefix} ",
                body,
                message_labels=body_style,
                prefix_labels=prefix_style,
            )
        )

    return emit_action


def _render_remote_and_github_lines(
    *,
    remote: GitRemote | None,
    remote_error: ErrorMessage | None,
    github_repository: str | None,
    github_error: ErrorMessage | None,
) -> tuple[tuple[str, str], ...]:
    lines: list[tuple[str, str]] = []
    if remote is None:
        if remote_error is None:
            lines.append(("warning", "Selected remote: unavailable"))
        else:
            lines.append(
                (
                    "warning",
                    ui.plain_text(("Selected remote: unavailable (", remote_error, ")")),
                )
            )
    if github_repository is None and github_error is not None:
        lines.append(("warning", f"GitHub target: unavailable ({github_error})"))
    return tuple(lines)


def _action_presentation(
    status: CleanupActionStatus,
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


def _revision_label_template(revision: ReviewStatusRevision):
    return t"{revision.subject} ({ui.change_id(revision.change_id)})"


def _restack_destination_template(destination_change_id: str | None):
    if destination_change_id is None:
        return ui.revset("trunk()")
    return ui.change_id(destination_change_id)


def _prepare_cleanup(
    *,
    config: RepoConfig,
    dry_run: bool,
    repo_root: Path,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    jj_client = JjClient(repo_root)
    state_store = ReviewStateStore.for_repo(repo_root)
    state = state_store.load()
    if not dry_run:
        state_store.require_writable()

    bookmark_states = _load_bookmark_states(
        prefix=config.bookmark_prefix,
        jj_client=jj_client,
        state=state,
    )

    return PreparedCleanup(
        config=config,
        dry_run=dry_run,
        bookmark_states=bookmark_states,
        github_repository=None,
        github_repository_error=None,
        jj_client=jj_client,
        remote=None,
        remote_error=None,
        remote_context_loaded=False,
        state=state,
        state_store=state_store,
    )


def _prepared_restack_has_potential_work(*, prepared_status: PreparedStatus) -> bool:
    """Whether any selected revision could possibly need restacking.

    Restack rebases surviving descendants past merged ancestors, so a stack
    where no revision carries review identity cannot have any known merged PRs
    and has nothing for restack to plan. Skipping the GitHub inspection here
    also avoids misreporting GitHub outages as restack-blocking when there
    would have been nothing to restack regardless.
    """

    for prepared_revision in prepared_status.prepared.status_revisions:
        cached = prepared_revision.cached_change
        if cached is not None and cached.has_review_identity:
            return True
    return False


def _stream_restack(
    *,
    on_action: Callable[[CleanupAction], None] | None = None,
    prepared_restack: PreparedRestack,
) -> RestackResult:
    """Inspect and optionally execute a local restack plan after merged changes."""

    prepared_status = prepared_restack.prepared_status
    if not _prepared_restack_has_potential_work(prepared_status=prepared_status):
        return RestackResult(actions=(), blocked=False)
    progress_total = prepared_status_github_inspection_count(
        prepared_status=prepared_status,
    )
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = stream_status(
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    prepared_status = prepared_restack.prepared_status
    prepared = prepared_status.prepared
    path_revisions = _resolve_restack_path_revisions(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    actions: list[CleanupAction] = []

    def record_action(action: CleanupAction) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    if status_result.github_error is not None or status_result.github_repository is None:
        record_action(
            CleanupAction(
                kind="restack",
                status="blocked",
                body=(
                    "cannot compute a restack plan without live GitHub pull request "
                    "state; fix GitHub access and retry"
                ),
            )
        )
        return RestackResult(
            actions=tuple(actions),
            blocked=True,
        )

    operation_plan = _plan_restack_operations(
        path_revisions=path_revisions,
        prepared_status=prepared_status,
    )
    blocked = operation_plan.blocked
    merged_revisions = operation_plan.merged_revisions
    if not merged_revisions:
        return RestackResult(
            actions=(),
            blocked=False,
        )

    closed_unmerged_revisions = operation_plan.closed_unmerged_revisions
    for action in operation_plan.pre_actions:
        record_action(action)
    rebase_plans = list(operation_plan.rebase_plans)

    restack_intent_state = _start_restack_intent(
        blocked=blocked,
        prepared=prepared,
        prepared_restack=prepared_restack,
        selected_revset=status_result.selected_revset,
    )

    client = prepared.client
    _restack_succeeded = False
    try:
        _run_restack_rebase_pass(
            blocked=blocked,
            client=client,
            closed_unmerged_revisions=closed_unmerged_revisions,
            prepared_restack=prepared_restack,
            rebase_plans=tuple(rebase_plans),
            record_action=record_action,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )

        _record_restack_policy_actions(
            prefix=prepared_restack.config.bookmark_prefix,
            merged_revisions=merged_revisions,
            record_action=record_action,
        )

        if not actions and merged_revisions:
            record_action(
                CleanupAction(
                    kind="restack",
                    status="planned" if prepared_restack.dry_run else "applied",
                    body=t"merged changes remain on the selected stack "
                    t"({ui.join(_revision_label_template, merged_revisions)}), but no "
                    t"surviving descendants need to move",
                )
            )

        _restack_succeeded = True
        return RestackResult(
            actions=tuple(actions),
            blocked=blocked,
        )
    finally:
        if (
            _restack_succeeded
            and restack_intent_state.intent_path is not None
            and restack_intent_state.intent is not None
        ):
            retire_superseded_intents(
                restack_intent_state.stale_intents,
                restack_intent_state.intent,
            )
            restack_intent_state.intent_path.unlink(missing_ok=True)


def _start_restack_intent(
    *,
    blocked: bool,
    prepared,
    prepared_restack: PreparedRestack,
    selected_revset: str,
) -> _RestackIntentState:
    """Write a restack intent before live rebases begin."""

    if blocked or prepared_restack.dry_run:
        return _RestackIntentState(intent=None, intent_path=None, stale_intents=[])

    ordered_change_ids = tuple(
        prepared_revision.revision.change_id for prepared_revision in prepared.status_revisions
    )
    ordered_commit_ids = tuple(
        prepared_revision.revision.commit_id for prepared_revision in prepared.status_revisions
    )
    intent = CleanupRestackIntent(
        kind="cleanup-restack",
        pid=os.getpid(),
        label=(
            f"cleanup --restack for {short_change_id(ordered_change_ids[-1])} "
            f"(from {selected_revset})"
        ),
        display_revset=selected_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        started_at=datetime.now(UTC).isoformat(),
    )
    state_dir = prepared.state_store.require_writable()
    stale_intents = check_same_kind_intent(state_dir, intent)
    for loaded in stale_intents:
        if not isinstance(loaded.intent, CleanupRestackIntent):
            continue
        match = match_cleanup_restack_intent(
            intent=loaded.intent,
            current_change_ids=ordered_change_ids,
            current_commit_ids=ordered_commit_ids,
        )
        description = describe_intent(loaded.intent)
        if match == "exact":
            console.note(t"Continuing interrupted {description}")
        elif match == "same-logical":
            console.note(
                t"Note: interrupted {description} targeted the same logical stack, "
                t"but it has been rewritten. This {ui.cmd('cleanup --restack')} run "
                t"will use the current stack."
            )
        elif match == "covered":
            console.note(
                t"Note: interrupted {description} targeted changes that are all "
                t"included in the current stack. This {ui.cmd('cleanup --restack')} "
                t"run will use the current stack."
            )
        elif match == "trimmed":
            console.note(
                t"Note: interrupted {description} still includes changes that are no "
                t"longer on the current stack. This {ui.cmd('cleanup --restack')} run "
                t"will use the current stack."
            )
        elif match == "overlap":
            console.warning(t"Warning: this restack overlaps an incomplete earlier "
                            t"operation ({description})")
        else:
            console.note(t"Note: incomplete operation outstanding: {description}")
    return _RestackIntentState(
        intent=intent,
        intent_path=write_new_intent(state_dir, intent),
        stale_intents=stale_intents,
    )


def _record_restack_policy_actions(
    *,
    prefix: str,
    merged_revisions: tuple[ReviewStatusRevision, ...],
    record_action: Callable[[CleanupAction], None],
) -> None:
    """Warn when a merged PR targeted another review branch."""

    for revision in merged_revisions:
        pull_request_number = revision_pull_request_number(revision)
        if pull_request_number is None:
            continue
        base_ref = _revision_pull_request_base_ref(revision)
        if base_ref is None or not is_review_bookmark(base_ref, prefix=prefix):
            continue
        record_action(
            CleanupAction(
                kind="policy",
                status="planned",
                body=(
                    t"PR #{pull_request_number} merged into branch {ui.bookmark(base_ref)}; "
                    t"configure GitHub to block merges of PRs targeting "
                    t"{ui.bookmark(bookmark_glob(prefix))}"
                ),
            )
        )

def _resolve_restack_path_revisions(
    *,
    prepared_status: PreparedStatus,
    status_result,
) -> tuple[ReviewStatusRevision, ...]:
    revisions_by_change_id = {
        revision.change_id: revision for revision in status_result.revisions
    }
    return tuple(
        revisions_by_change_id[prepared_revision.revision.change_id]
        for prepared_revision in prepared_status.prepared.status_revisions
        if prepared_revision.revision.change_id in revisions_by_change_id
    )


def _run_restack_rebase_pass(
    *,
    blocked: bool,
    client: JjClient,
    closed_unmerged_revisions: tuple[ReviewStatusRevision, ...],
    prepared_restack: PreparedRestack,
    rebase_plans: tuple[tuple[str, str | None], ...],
    record_action: Callable[[CleanupAction], None],
    trunk_commit_id: str,
) -> None:
    if not prepared_restack.dry_run and not blocked:
        for source_change_id, destination_change_id in rebase_plans:
            source_revision = client.resolve_revision(source_change_id)
            destination_commit_id = _restack_destination_commit_id(
                client=client,
                destination_change_id=destination_change_id,
                trunk_commit_id=trunk_commit_id,
            )
            if source_revision.only_parent_commit_id() == destination_commit_id:
                continue
            client.rebase_revision(
                source=source_change_id,
                destination=destination_commit_id,
            )
            record_action(
                CleanupAction(
                    kind="restack",
                    status="applied",
                    body=(
                        t"rebase {ui.change_id(source_change_id)} onto "
                        t"{_restack_destination_template(destination_change_id)}"
                    ),
                )
            )
        return

    for source_change_id, destination_change_id in rebase_plans:
        status = "blocked" if blocked else "planned"
        body = (
            t"rebase {ui.change_id(source_change_id)} onto "
            t"{_restack_destination_template(destination_change_id)}"
        )
        if blocked and closed_unmerged_revisions:
            body = t"{body} once blocked changes on the stack are resolved"
        record_action(
            CleanupAction(
                kind="restack",
                status=status,
                body=body,
            )
        )


def _restack_destination_commit_id(
    *,
    client: JjClient,
    destination_change_id: str | None,
    trunk_commit_id: str,
) -> str:
    if destination_change_id is None:
        return trunk_commit_id
    return client.resolve_revision(destination_change_id).commit_id


def _plan_restack_operations(
    *,
    path_revisions: tuple[ReviewStatusRevision, ...],
    prepared_status: PreparedStatus,
) -> _RestackOperationPlan:
    merged_revisions = tuple(
        revision for revision in path_revisions if revision_has_merged_pull_request(revision)
    )
    closed_unmerged_revisions = tuple(
        revision for revision in path_revisions if _revision_is_closed_unmerged(revision)
    )
    revisions_by_change_id = {revision.change_id: revision for revision in path_revisions}
    current_commit_id_by_change_id = {
        prepared_revision.revision.change_id: prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    }

    blocked, actions = _collect_restack_pre_actions(
        closed_unmerged_revisions=closed_unmerged_revisions,
        current_commit_id_by_change_id=current_commit_id_by_change_id,
        merged_revisions=merged_revisions,
    )
    blocked, rebase_plans = _plan_restack_rebases(
        actions=actions,
        blocked=blocked,
        prepared_status=prepared_status,
        revisions_by_change_id=revisions_by_change_id,
    )

    return _RestackOperationPlan(
        blocked=blocked,
        closed_unmerged_revisions=closed_unmerged_revisions,
        merged_revisions=merged_revisions,
        pre_actions=tuple(actions),
        rebase_plans=tuple(rebase_plans),
    )


def _collect_restack_pre_actions(
    *,
    closed_unmerged_revisions: tuple[ReviewStatusRevision, ...],
    current_commit_id_by_change_id: dict[str, str],
    merged_revisions: tuple[ReviewStatusRevision, ...],
) -> tuple[bool, list[CleanupAction]]:
    """Record blocking restack conditions before survivor planning begins."""

    blocked = False
    actions: list[CleanupAction] = []
    for revision in closed_unmerged_revisions:
        blocked = True
        actions.append(
            CleanupAction(
                kind="restack",
                status="blocked",
                body=(
                    t"cannot restack past {_revision_label_template(revision)} because "
                    t"PR #{revision_pull_request_number(revision)} is closed without "
                    t"merge; decide whether to keep or drop that change first"
                ),
            )
        )

    for revision in merged_revisions:
        cached_change = revision.cached_change
        if cached_change is None or cached_change.last_submitted_commit_id is None:
            continue
        current_commit_id = current_commit_id_by_change_id[revision.change_id]
        if current_commit_id == cached_change.last_submitted_commit_id:
            continue
        blocked = True
        actions.append(
            CleanupAction(
                kind="restack",
                status="blocked",
                body=(
                    t"cannot restack past {_revision_label_template(revision)} because it "
                    t"has local edits since last submit; push a new version first or "
                    t"rebase manually"
                ),
            )
        )

    return blocked, actions


def _plan_restack_rebases(
    *,
    actions: list[CleanupAction],
    blocked: bool,
    prepared_status: PreparedStatus,
    revisions_by_change_id: dict[str, ReviewStatusRevision],
) -> tuple[bool, list[tuple[str, str | None]]]:
    """Plan survivor rebases after merged ancestors are removed from the path."""

    survivor_change_ids: list[str] = []
    rebase_plans: list[tuple[str, str | None]] = []
    for prepared_revision in prepared_status.prepared.status_revisions:
        revision = revisions_by_change_id.get(prepared_revision.revision.change_id)
        if revision is None:
            continue
        if revision_has_merged_pull_request(revision):
            continue
        if _revision_is_closed_unmerged(revision):
            continue
        if revision.local_divergent:
            blocked = True
            actions.append(
                CleanupAction(
                    kind="restack",
                    status="blocked",
                    body=(
                        t"cannot restack {_revision_label_template(revision)} while "
                        t"multiple visible revisions still share that change ID"
                    ),
                )
            )
            survivor_change_ids.append(revision.change_id)
            continue

        desired_parent_change_id = survivor_change_ids[-1] if survivor_change_ids else None
        if _restack_parent_is_merged(
            parent_commit_id=prepared_revision.revision.only_parent_commit_id(),
            prepared_status=prepared_status,
            revisions_by_change_id=revisions_by_change_id,
        ):
            rebase_plans.append((revision.change_id, desired_parent_change_id))
        survivor_change_ids.append(revision.change_id)
    return blocked, rebase_plans


def _restack_parent_is_merged(
    *,
    parent_commit_id: str | None,
    prepared_status: PreparedStatus,
    revisions_by_change_id: dict[str, ReviewStatusRevision],
) -> bool:
    for candidate in prepared_status.prepared.status_revisions:
        if candidate.revision.commit_id != parent_commit_id:
            continue
        revision = revisions_by_change_id.get(candidate.revision.change_id)
        return revision is not None and revision_has_merged_pull_request(revision)
    return False


def _revision_is_closed_unmerged(revision: ReviewStatusRevision) -> bool:
    lookup = revision.pull_request_lookup
    return (
        lookup is not None
        and lookup.state == "closed"
        and lookup.pull_request is not None
        and lookup.pull_request.state != "merged"
    )


def _revision_pull_request_base_ref(revision: ReviewStatusRevision) -> str | None:
    lookup = revision.pull_request_lookup
    if lookup is None or lookup.pull_request is None:
        return None
    return lookup.pull_request.base.ref


async def _run_cleanup_async(
    *,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
    stale_reasons: dict[str, str | None] | None = None,
) -> CleanupResult:
    next_changes = dict(prepared_cleanup.state.changes)
    actions: list[CleanupAction] = []
    dry_run = prepared_cleanup.dry_run

    def record_action(action: CleanupAction) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    # Write an intent file before the first mutation on live runs only.
    intent_path: Path | None = None
    _cleanup_succeeded = False
    stale_intents: list[LoadedIntent] = []
    if not dry_run:
        state_dir = prepared_cleanup.state_store.require_writable()
        _intent = CleanupIntent(
            kind="cleanup",
            pid=os.getpid(),
            label="cleanup",
            started_at=datetime.now(UTC).isoformat(),
        )
        stale_intents = check_same_kind_intent(state_dir, _intent)
        for _loaded in stale_intents:
            console.note(f"Note: a previous cleanup was interrupted ({_loaded.intent.label})")
        intent_path = write_new_intent(state_dir, _intent)

    try:
        if stale_reasons is None:
            stale_reasons = _stale_change_reasons(
                change_ids=tuple(prepared_cleanup.state.changes),
                jj_client=prepared_cleanup.jj_client,
            )
        if _cleanup_needs_remote_context(
            prepared_cleanup=prepared_cleanup,
            stale_reasons=stale_reasons,
        ):
            prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        prepared_changes = _run_local_cleanup_pass(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
            stale_reasons=stale_reasons,
        )
        if prepared_cleanup.github_repository is None:
            _save_cleanup_state_if_changed(
                next_changes=next_changes,
                prepared_cleanup=prepared_cleanup,
            )

            _cleanup_succeeded = True
            return CleanupResult(
                actions=tuple(actions),
            )

        if not any(
            prepared_change.inspect_stack_comment for prepared_change in prepared_changes
        ):
            _save_cleanup_state_if_changed(
                next_changes=next_changes,
                prepared_cleanup=prepared_cleanup,
            )

            _cleanup_succeeded = True
            return CleanupResult(
                actions=tuple(actions),
            )

        github_repository = prepared_cleanup.github_repository
        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            await _run_stack_comment_cleanup_pass(
                github_client=github_client,
                github_repository=github_repository,
                next_changes=next_changes,
                prepared_changes=prepared_changes,
                prepared_cleanup=prepared_cleanup,
                record_action=record_action,
            )

        _save_cleanup_state_if_changed(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
        )

        _cleanup_succeeded = True
        return CleanupResult(
            actions=tuple(actions),
        )
    finally:
        if _cleanup_succeeded and intent_path is not None:
            for loaded in stale_intents:
                loaded.path.unlink(missing_ok=True)
            intent_path.unlink(missing_ok=True)

def _run_local_cleanup_pass(
    *,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    stale_reasons: dict[str, str | None],
) -> tuple[PreparedCleanupChange, ...]:
    prepared_changes: list[PreparedCleanupChange] = []
    mutation_plans: list[_StaleCleanupMutationPlan] = []
    orphan_local_bookmark_plans: list[OrphanLocalBookmarkCleanupPlan] = []
    for change_id, cached_change in prepared_cleanup.state.changes.items():
        stale_reason = stale_reasons.get(change_id)
        bookmark_state = prepared_cleanup.bookmark_states.get(
            cached_change.bookmark or "",
            BookmarkState(name=cached_change.bookmark or ""),
        )
        prepared_change = PreparedCleanupChange(
            bookmark_state=bookmark_state,
            cached_change=cached_change,
            change_id=change_id,
            inspect_stack_comment=_should_inspect_stack_comment_cleanup(
                bookmark_state=bookmark_state,
                cached_change=cached_change,
                remote=prepared_cleanup.remote,
                stale_reason=stale_reason,
            ),
            stale_reason=stale_reason,
        )
        prepared_changes.append(prepared_change)
        mutation_plan = _process_stale_cleanup_change(
            next_changes=next_changes,
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )
        if mutation_plan is not None:
            mutation_plans.append(mutation_plan)

    tracked_bookmarks = {
        cached_change.bookmark
        for cached_change in prepared_cleanup.state.changes.values()
        if cached_change.bookmark is not None
    }
    for bookmark, bookmark_state in sorted(prepared_cleanup.bookmark_states.items()):
        if bookmark in tracked_bookmarks or not is_review_bookmark(
            bookmark,
            prefix=prepared_cleanup.config.bookmark_prefix,
        ):
            continue
        orphan_plan = _plan_orphan_local_bookmark_cleanup(
            prefix=prepared_cleanup.config.bookmark_prefix,
            bookmark_state=bookmark_state,
            jj_client=prepared_cleanup.jj_client,
        )
        if orphan_plan is None:
            continue
        if prepared_cleanup.dry_run:
            record_action(orphan_plan.action)
            continue
        if orphan_plan.action.status != "planned":
            record_action(orphan_plan.action)
            continue
        orphan_local_bookmark_plans.append(orphan_plan)

    if not prepared_cleanup.dry_run:
        _apply_stale_cleanup_mutation_plans(
            jj_client=prepared_cleanup.jj_client,
            mutation_plans=tuple(mutation_plans),
            orphan_local_bookmark_plans=tuple(orphan_local_bookmark_plans),
            record_action=record_action,
            remote=prepared_cleanup.remote,
        )
        _save_cleanup_state_if_changed(
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
        )
    return tuple(prepared_changes)


def _process_stale_cleanup_change(
    *,
    next_changes: dict[str, CachedChange],
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> _StaleCleanupMutationPlan | None:
    stale_reason = prepared_change.stale_reason
    if stale_reason is None:
        return None

    record_action(
        CleanupAction(
            kind="tracking",
            status="planned" if prepared_cleanup.dry_run else "applied",
            body=t"remove tracking for {ui.change_id(prepared_change.change_id)} "
            t"({stale_reason})",
        )
    )
    if not prepared_cleanup.dry_run:
        next_changes.pop(prepared_change.change_id, None)

    local_bookmark_plan = _plan_local_bookmark_cleanup(
        bookmark_state=prepared_change.bookmark_state,
        prefix=prepared_cleanup.config.bookmark_prefix,
        cached_change=prepared_change.cached_change,
        stale_reason=stale_reason,
    )
    remote_plan = _plan_remote_branch_cleanup(
        bookmark_state=prepared_change.bookmark_state,
        prefix=prepared_cleanup.config.bookmark_prefix,
        cached_change=prepared_change.cached_change,
        local_bookmark_forget_planned=(
            local_bookmark_plan is not None and local_bookmark_plan.status == "planned"
        ),
        remote=prepared_cleanup.remote,
    )
    if prepared_cleanup.dry_run:
        if local_bookmark_plan is not None:
            record_action(local_bookmark_plan)
        if remote_plan is not None:
            record_action(remote_plan.action)
        return None

    if local_bookmark_plan is not None and local_bookmark_plan.status != "planned":
        record_action(local_bookmark_plan)
    if remote_plan is not None and remote_plan.action.status != "planned":
        record_action(remote_plan.action)

    if (local_bookmark_plan is None or local_bookmark_plan.status != "planned") and (
        remote_plan is None or remote_plan.action.status != "planned"
    ):
        return None

    return _StaleCleanupMutationPlan(
        cached_change=prepared_change.cached_change,
        local_bookmark_action=local_bookmark_plan,
        remote_plan=remote_plan,
    )


def _apply_stale_cleanup_mutation_plans(
    *,
    jj_client: JjClient,
    mutation_plans: tuple[_StaleCleanupMutationPlan, ...],
    orphan_local_bookmark_plans: tuple[OrphanLocalBookmarkCleanupPlan, ...] = (),
    record_action: Callable[[CleanupAction], None],
    remote: GitRemote | None,
) -> None:
    remote_deletions: list[tuple[str, str]] = []
    remote_actions: list[CleanupAction] = []
    local_bookmarks: list[str] = []
    local_actions: list[CleanupAction] = []

    for mutation_plan in mutation_plans:
        remote_plan = mutation_plan.remote_plan
        if (
            remote_plan is not None
            and remote_plan.action.status == "planned"
            and remote is not None
            and remote_plan.expected_remote_target is not None
        ):
            bookmark = mutation_plan.cached_change.bookmark
            if bookmark is None:
                raise AssertionError("Planned remote branch cleanup requires a bookmark.")
            remote_deletions.append((bookmark, remote_plan.expected_remote_target))
            remote_actions.append(remote_plan.action)

        local_bookmark_action = mutation_plan.local_bookmark_action
        if local_bookmark_action is not None and local_bookmark_action.status == "planned":
            bookmark = mutation_plan.cached_change.bookmark
            if bookmark is None:
                raise AssertionError("Planned local bookmark cleanup requires a bookmark.")
            local_bookmarks.append(bookmark)
            local_actions.append(local_bookmark_action)

    for orphan_plan in orphan_local_bookmark_plans:
        if orphan_plan.action.status != "planned":
            continue
        local_bookmarks.append(orphan_plan.bookmark)
        local_actions.append(orphan_plan.action)

    remote_deleted = False
    try:
        if remote_deletions and remote is not None:
            jj_client.delete_remote_bookmarks(
                remote=remote.name,
                deletions=tuple(remote_deletions),
                fetch=False,
            )
            remote_deleted = True
        if local_bookmarks:
            jj_client.forget_bookmarks(tuple(local_bookmarks))
    finally:
        if remote_deleted and remote is not None:
            jj_client.fetch_remote(remote=remote.name)

    for remote_action in remote_actions:
        record_action(replace(remote_action, status="applied"))
    for local_action in local_actions:
        record_action(replace(local_action, status="applied"))


def _save_cleanup_state_if_changed(
    *,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
) -> None:
    if not prepared_cleanup.dry_run and next_changes != prepared_cleanup.state.changes:
        prepared_cleanup.state_store.save(
            prepared_cleanup.state.model_copy(update={"changes": dict(next_changes)})
        )


async def _run_stack_comment_cleanup_pass(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    next_changes: dict[str, CachedChange],
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    stack_comment_changes = tuple(
        prepared_change
        for prepared_change in prepared_changes
        if prepared_change.inspect_stack_comment
    )
    with console.progress(
        description="Inspecting stack comments",
        total=len(stack_comment_changes),
    ) as progress:
        comment_plans = await run_bounded_tasks(
            concurrency=_GITHUB_INSPECTION_CONCURRENCY,
            items=stack_comment_changes,
            run_item=lambda prepared_change: _plan_stack_comment_cleanup(
                cached_change=prepared_change.cached_change,
                bookmark_state=prepared_change.bookmark_state,
                github_client=github_client,
                github_repository=github_repository,
            ),
            on_success=lambda _index, _result: progress.advance(),
        )
    for prepared_change, comment_plan in zip(
        stack_comment_changes,
        comment_plans,
        strict=True,
    ):
        if comment_plan is None:
            continue
        await _apply_stack_comment_cleanup_action(
            comment_plan=comment_plan,
            change_id=prepared_change.change_id,
            github_client=github_client,
            github_repository=github_repository,
            next_changes=next_changes,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
        )


async def _apply_stack_comment_cleanup_action(
    *,
    comment_plan: StackCommentCleanupPlan,
    change_id: str,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    next_changes: dict[str, CachedChange],
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
) -> None:
    comment_action = comment_plan.action
    if (
        not prepared_cleanup.dry_run
        and comment_action.status == "planned"
        and comment_plan.comment_id is not None
    ):
        try:
            await github_client.delete_issue_comment(
                github_repository.owner,
                github_repository.repo,
                comment_id=comment_plan.comment_id,
            )
        except GithubClientError as error:
            raise CliError(
                f"Could not delete stack summary comment #{comment_plan.comment_id}"
            ) from error
        if change_id in next_changes:
            next_changes[change_id] = next_changes[change_id].model_copy(
                update={"stack_comment_id": None}
            )
        comment_action = replace(comment_plan.action, status="applied")
    record_action(comment_action)
    if not prepared_cleanup.dry_run:
        prepared_cleanup.state_store.save(
            prepared_cleanup.state.model_copy(update={"changes": dict(next_changes)})
        )


def _resolve_remote(*, jj_client: JjClient) -> tuple[GitRemote | None, ErrorMessage | None]:
    try:
        return select_submit_remote(jj_client.list_git_remotes()), None
    except CliError as error:
        return None, error_message(error)


def _load_cleanup_remote_context(*, prepared_cleanup: PreparedCleanup) -> PreparedCleanup:
    """Resolve remote and GitHub target details once plain cleanup actually needs them."""

    if prepared_cleanup.remote_context_loaded:
        return prepared_cleanup

    remote, remote_error = _resolve_remote(jj_client=prepared_cleanup.jj_client)
    github_repository = None
    github_error = None
    if remote is not None:
        github_repository = parse_github_repo(remote)
        if github_repository is None:
            github_error = (
                t"Could not determine the GitHub repository for remote "
                t"{ui.bookmark(remote.name)}. Use a GitHub remote URL."
            )

    return replace(
        prepared_cleanup,
        github_repository=github_repository,
        github_repository_error=github_error,
        remote=remote,
        remote_error=remote_error,
        remote_context_loaded=True,
    )


def _cleanup_needs_remote_context(
    *,
    prepared_cleanup: PreparedCleanup,
    stale_reasons: dict[str, str | None],
) -> bool:
    """Whether plain cleanup might need remote or GitHub state beyond local checks."""

    for change_id, cached_change in prepared_cleanup.state.changes.items():
        stale_reason = stale_reasons.get(change_id)
        bookmark = cached_change.bookmark
        bookmark_state = prepared_cleanup.bookmark_states.get(
            bookmark or "",
            BookmarkState(name=bookmark or ""),
        )
        if (
            stale_reason is not None
            and bookmark is not None
            and is_review_bookmark(
                bookmark,
                prefix=prepared_cleanup.config.bookmark_prefix,
            )
            and bookmark_state.remote_targets
        ):
            return True
        if _stack_comment_cleanup_eligibility(
            cached_change=cached_change,
            stale_reason=stale_reason,
        ) != "skip":
            return True
    return False


def _stack_comment_cleanup_eligibility(
    *,
    cached_change: CachedChange,
    stale_reason: str | None,
) -> StackCommentCleanupEligibility:
    """Classify whether cleanup can inspect stack comments for this change."""

    if cached_change.pr_number is None:
        if cached_change.is_unlinked and cached_change.bookmark is not None:
            return "inspect"
        return "skip"
    if cached_change.is_unlinked:
        return "inspect"
    if cached_change.bookmark is None and cached_change.stack_comment_id is None:
        return "skip"
    if stale_reason is None:
        return "inspect"
    if cached_change.pr_state in {"closed", "merged"}:
        return "skip"
    if cached_change.stack_comment_id is not None:
        return "inspect"
    if cached_change.bookmark is None:
        return "skip"
    return "needs-remote-check"


def _load_bookmark_states(
    *,
    prefix: str,
    jj_client: JjClient,
    state: ReviewState,
) -> dict[str, BookmarkState]:
    bookmark_states = jj_client.list_bookmark_states()
    tracked_bookmarks = {
        cached_change.bookmark
        for cached_change in state.changes.values()
        if cached_change.bookmark is not None
    }
    relevant_bookmarks = {
        bookmark
        for bookmark, bookmark_state in bookmark_states.items()
        if is_review_bookmark(bookmark, prefix=prefix)
        and bookmark_state.local_targets
    }
    relevant_bookmarks.update(tracked_bookmarks)

    if not relevant_bookmarks:
        return {}

    filtered = {
        bookmark: bookmark_states[bookmark]
        for bookmark in relevant_bookmarks
        if bookmark in bookmark_states
    }
    for bookmark in tracked_bookmarks:
        filtered.setdefault(bookmark, BookmarkState(name=bookmark))
    return filtered


def _stale_change_reasons(
    *,
    change_ids: tuple[str, ...],
    jj_client: JjClient,
) -> dict[str, str | None]:
    matched_revisions = jj_client.query_revisions_by_change_ids(change_ids)
    reasons: dict[str, str | None] = {}

    for change_id in change_ids:
        revisions = matched_revisions.get(change_id, ())
        if not revisions:
            reasons[change_id] = "no visible local change matches that cached change ID"
            continue
        if len(revisions) > 1:
            reasons[change_id] = "multiple visible revisions still share that change ID"
            continue

        revision = revisions[0]
        if not revision.is_reviewable():
            reasons[change_id] = "local change is no longer reviewable"
            continue

        reasons[change_id] = None

    candidate_revisions = tuple(
        revisions[0]
        for change_id in change_ids
        if reasons.get(change_id) is None
        for revisions in (matched_revisions.get(change_id, ()),)
        if revisions
    )
    supported_change_ids = jj_client.supported_review_stack_change_ids(candidate_revisions)
    for revision in candidate_revisions:
        if revision.change_id not in supported_change_ids:
            reasons[revision.change_id] = (
                "local change no longer participates in a supported review stack"
            )
    return reasons


def _plan_remote_branch_cleanup(
    *,
    bookmark_state: BookmarkState,
    prefix: str,
    cached_change: CachedChange,
    local_bookmark_forget_planned: bool,
    remote: GitRemote | None,
) -> RemoteBranchCleanupPlan | None:
    bookmark = cached_change.bookmark
    if remote is None or bookmark is None or not is_review_bookmark(
        bookmark,
        prefix=prefix,
    ):
        return None

    remote_state = bookmark_state.remote_target(remote.name)
    if remote_state is None or not remote_state.targets:
        return None

    branch_label = f"{bookmark}@{remote.name}"
    if bookmark_state.local_targets and not local_bookmark_forget_planned:
        return RemoteBranchCleanupPlan(
            action=CleanupAction(
                kind="remote branch",
                status="blocked",
                body=(
                    t"cannot delete {ui.bookmark(branch_label)} while the local "
                    t"bookmark {ui.bookmark(bookmark)} still exists"
                ),
            ),
        )
    if len(remote_state.targets) > 1:
        return RemoteBranchCleanupPlan(
            action=CleanupAction(
                kind="remote branch",
                status="blocked",
                body=(
                    t"cannot delete {ui.bookmark(branch_label)} because the remote "
                    t"bookmark is conflicted"
                ),
            ),
        )

    return RemoteBranchCleanupPlan(
        action=CleanupAction(
            kind="remote branch",
            status="planned",
            body=t"delete {ui.bookmark(branch_label)}",
        ),
        expected_remote_target=remote_state.target,
    )


def _plan_local_bookmark_cleanup(
    *,
    bookmark_state: BookmarkState,
    prefix: str,
    cached_change: CachedChange,
    stale_reason: str,
) -> CleanupAction | None:
    bookmark = cached_change.bookmark
    if bookmark is None or not is_review_bookmark(
        bookmark,
        prefix=prefix,
    ):
        return None
    if not bookmark_state.local_targets:
        return None
    if len(bookmark_state.local_targets) > 1:
        return CleanupAction(
            kind="local bookmark",
            status="blocked",
            body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
        )

    local_target = bookmark_state.local_target
    if local_target is None:
        return None

    expected_target = cached_change.last_submitted_commit_id
    if expected_target is not None and local_target != expected_target:
        return CleanupAction(
            kind="local bookmark",
            status="blocked",
            body=(
                t"cannot forget {ui.bookmark(bookmark)} because it already points "
                t"to a different revision"
            ),
        )

    return CleanupAction(
        kind="local bookmark",
        status="planned",
        body=t"forget {ui.bookmark(bookmark)} ({stale_reason})",
    )


def _plan_orphan_local_bookmark_cleanup(
    *,
    prefix: str,
    bookmark_state: BookmarkState,
    jj_client: JjClient,
) -> OrphanLocalBookmarkCleanupPlan | None:
    bookmark = bookmark_state.name
    if not is_review_bookmark(bookmark, prefix=prefix) or not bookmark_state.local_targets:
        return None
    if len(bookmark_state.local_targets) > 1:
        return OrphanLocalBookmarkCleanupPlan(
            bookmark=bookmark,
            action=CleanupAction(
                kind="local bookmark",
                status="blocked",
                body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
            ),
        )

    local_target = bookmark_state.local_target
    if local_target is None:
        return None

    revisions = jj_client.query_revisions(local_target)
    if not revisions:
        stale_reason = "target is no longer visible locally"
    else:
        revision = revisions[0]
        if not revision.is_reviewable():
            stale_reason = "target is no longer reviewable"
        elif revision.change_id not in jj_client.supported_review_stack_change_ids((revision,)):
            stale_reason = "target no longer participates in a supported review stack"
        else:
            return None

    return OrphanLocalBookmarkCleanupPlan(
        bookmark=bookmark,
        action=CleanupAction(
            kind="local bookmark",
            status="planned",
            body=t"forget {ui.bookmark(bookmark)} ({stale_reason})",
        ),
    )


def _should_inspect_stack_comment_cleanup(
    *,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    remote: GitRemote | None,
    stale_reason: str | None,
) -> bool:
    eligibility = _stack_comment_cleanup_eligibility(
        cached_change=cached_change,
        stale_reason=stale_reason,
    )
    if eligibility == "inspect":
        return True
    if eligibility == "skip":
        return False
    if remote is None:
        return False

    remote_state = bookmark_state.remote_target(remote.name)
    return remote_state is None or not remote_state.targets


async def _plan_stack_comment_cleanup(
    *,
    cached_change: CachedChange,
    bookmark_state: BookmarkState,
    github_client: GithubClient,
    github_repository,
) -> StackCommentCleanupPlan | None:
    pull_request_number = cached_change.pr_number
    if pull_request_number is None and cached_change.is_unlinked:
        pull_request_number = await _resolve_unlinked_pull_request_number(
            bookmark_state=bookmark_state,
            github_client=github_client,
            github_repository=github_repository,
        )
        if isinstance(pull_request_number, CleanupAction):
            return StackCommentCleanupPlan(action=pull_request_number)

    if pull_request_number is None:
        return None

    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return None
        raise CliError(f"Could not load pull request #{pull_request_number}") from error

    if not cached_change.is_unlinked:
        bookmark = cached_change.bookmark
        if bookmark is None:
            return None
        expected_label = f"{github_repository.owner}:{bookmark}"
        if pull_request.head.ref == bookmark and pull_request.head.label == expected_label:
            return None

    stack_summary_comment = await _resolve_stack_summary_comment(
        cached_change=cached_change,
        github_client=github_client,
        github_repository=github_repository,
        pull_request_number=pull_request_number,
    )
    if isinstance(stack_summary_comment, CleanupAction):
        return StackCommentCleanupPlan(action=stack_summary_comment)
    if stack_summary_comment is None:
        return None

    return StackCommentCleanupPlan(
        action=CleanupAction(
            kind="stack summary comment",
            status="planned",
            body=(
                "delete stack summary comment "
                f"#{stack_summary_comment.id} from PR #{pull_request_number}"
            ),
        ),
        comment_id=stack_summary_comment.id,
    )
async def _resolve_stack_summary_comment(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> GithubIssueComment | CleanupAction | None:
    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not list stack summary comments for pull request #{pull_request_number}"
        ) from error
    if cached_change.stack_comment_id is not None:
        cached_comment = next(
            (comment for comment in comments if comment.id == cached_change.stack_comment_id),
            None,
        )
        if cached_comment is not None:
            if not is_stack_summary_comment(cached_comment.body):
                return CleanupAction(
                    kind="stack summary comment",
                    status="blocked",
                    body=(
                        "cannot delete saved stack summary comment "
                        f"#{cached_comment.id} because it does not belong to us"
                    ),
                )
            return cached_comment

    stack_summary_comments = [c for c in comments if is_stack_summary_comment(c.body)]
    if len(stack_summary_comments) > 1:
        return CleanupAction(
            kind="stack summary comment",
            status="blocked",
            body=(
                "cannot delete stack summary comments because GitHub reports "
                f"multiple candidates on PR #{pull_request_number}"
            ),
        )
    if not stack_summary_comments:
        return None
    return stack_summary_comments[0]


async def _resolve_unlinked_pull_request_number(
    *,
    bookmark_state: BookmarkState,
    github_client: GithubClient,
    github_repository,
) -> int | CleanupAction | None:
    if bookmark_state.name == "":
        return None

    try:
        pull_requests = await github_client.list_pull_requests(
            github_repository.owner,
            github_repository.repo,
            head=f"{github_repository.owner}:{bookmark_state.name}",
            state="all",
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not list pull requests for unlinked bookmark "
            t"{ui.bookmark(bookmark_state.name)}"
        ) from error

    if not pull_requests:
        return None
    if len(pull_requests) > 1:
        return CleanupAction(
            kind="stack summary comment",
            status="blocked",
            body=(
                t"cannot delete stack summary comment because GitHub reports multiple "
                t"pull requests for unlinked bookmark {ui.bookmark(bookmark_state.name)}"
            ),
        )
    return pull_requests[0].number
