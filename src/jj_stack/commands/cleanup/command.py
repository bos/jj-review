"""Remove PR branches, comments, and saved links no active pull request needs.

With no selector, it checks the whole repo. A revset limits cleanup to one local stack;
`--pull-request` selects one tracked pull request, and `--pull-request orphans` selects every
tracked pull request whose local change is gone. Add `--close` to close selected open pull
requests before cleanup.

Without `--close`, open pull requests are left alone. Already closed or merged pull requests do
not need the flag and are cleaned up normally.

If another pull request still uses a PR branch as its base, that branch stays, because GitHub
will not reopen a pull request whose base branch is gone. Retarget the pull request named in the
message, reopening it first if it is closed, then rerun the same cleanup command.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._cleanup_actions import (
    apply_overview_comment_cleanup,
    apply_remote_branch_cleanup,
    check_tracked_pr,
    emit_action_row,
    github_stack_cleanup_blockers,
    plan_pr_cleanup,
)
from jj_stack.errors import AmbiguousSelectionError, CliError, UsageError
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError, build_github_client
from jj_stack.github.error_messages import github_target_unavailable_messages
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER
from jj_stack.github.resolution import (
    GithubTarget,
    resolve_github_target,
)
from jj_stack.identifiers import short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import PRRefUpdate
from jj_stack.models.github import GithubIssueComment, GithubPR, GithubStack
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.change_status import enumerate_orphaned_records
from jj_stack.stack.pr_facts import (
    RepoFacts,
    observe_github_stacks,
    observe_prs,
)
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.selected import select_stack_path
from jj_stack.stack.selection import resolve_pr_reference
from jj_stack.state.operation_lock import operation_lock_if_mutating
from jj_stack.ui import plain_text

from .shared import (
    CleanupAction,
    CleanupResult,
    PreparedCleanup,
    PreparedCleanupChange,
)
from .stale import (
    LocalCleanupObservation,
    local_cleanup_observations,
)

HELP = "Remove PR data that no active pull request needs"
type CleanupPreflight = tuple[GithubPR | None, PRRefUpdate | None, CleanupAction | None]


def _build_action_streamer(*, header: str) -> Callable[[CleanupAction], None]:
    """Print the action header once, then stream actions as they arrive."""

    header_printed = False

    def emit_action(action: CleanupAction) -> None:
        nonlocal header_printed
        if not header_printed:
            console.output(header)
            header_printed = True
        emit_action_row(kind=action.kind, status=action.status, body=action.body)

    return emit_action


def cleanup(
    *,
    cli_args: JjCliArgs,
    close: bool,
    debug: bool,
    dry_run: bool,
    pr: str | None,
    repo: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `cleanup`."""

    if pr is not None and revset is not None:
        raise UsageError("cleanup --pull-request cannot be combined with a revset.")
    if close and pr is None:
        raise UsageError("cleanup --close requires --pull-request.")

    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    with operation_lock_if_mutating(
        context.state_store,
        command="cleanup",
        mutating=not dry_run,
    ):
        return _run_cleanup_command(
            close=close,
            context=context,
            dry_run=dry_run,
            pr=pr,
            revset=revset,
        )


def _run_cleanup_command(
    *,
    close: bool,
    context: CommandContext,
    dry_run: bool,
    pr: str | None,
    revset: str | None,
) -> int:
    """Render and run the stale cleanup command path."""

    with console.spinner(description="Loading PR state"):
        prepared_cleanup = _prepare_cleanup(
            close=close,
            context=context,
            dry_run=dry_run,
            pr=pr,
            revset=revset,
        )
    selected_change_ids = prepared_cleanup.selected_change_ids
    observed_change_ids = (
        tuple(prepared_cleanup.state.pr_identities)
        if selected_change_ids is None
        else selected_change_ids
    )
    local_observations = local_cleanup_observations(
        change_ids=observed_change_ids,
        context=prepared_cleanup.context,
    )
    if _cleanup_needs_remote_context(prepared_cleanup=prepared_cleanup):
        prepared_cleanup = _load_cleanup_remote_context(prepared_cleanup=prepared_cleanup)
        for message in github_target_unavailable_messages(prepared_cleanup.github_target):
            console.warning(plain_text(message))

    result = asyncio.run(
        _run_cleanup_async(
            on_action=_build_action_streamer(
                header=(
                    "Planned cleanup actions:"
                    if prepared_cleanup.dry_run
                    else "Applied cleanup actions:"
                ),
            ),
            prepared_cleanup=prepared_cleanup,
            local_observations=local_observations,
        )
    )
    if not result.actions:
        console.output("No cleanup actions needed.")
    return 1 if any(action.status == "blocked" for action in result.actions) else 0


async def cleanup_tracked_prs(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
    dry_run: bool,
    github_client: GithubClient,
    github_target: GithubTarget,
    planned_detached_dependents: frozenset[int] = frozenset(),
    planned_local_removals: frozenset[str] = frozenset(),
) -> CleanupResult:
    """Run cleanup for PRs reconciled by another command."""

    state = context.state_store.load()
    if not dry_run:
        context.state_store.require_writable()
    prepared_cleanup = PreparedCleanup(
        close_open_prs=False,
        context=context,
        github_target=github_target,
        dry_run=dry_run,
        selected_change_ids=change_ids,
        state=state,
    )
    local_observations = local_cleanup_observations(
        change_ids=change_ids,
        context=context,
    )
    if dry_run:
        local_observations.update(
            {
                change_id: LocalCleanupObservation(
                    has_mutable_copy=False,
                    stale_reason=None,
                )
                for change_id in planned_local_removals
            }
        )
    return await _run_cleanup_async(
        github_client=github_client,
        on_action=_build_action_streamer(
            header="Planned cleanup actions:" if dry_run else "Applied cleanup actions:",
        ),
        prepared_cleanup=prepared_cleanup,
        preview_detached_dependents=(planned_detached_dependents if dry_run else frozenset()),
        local_observations=local_observations,
    )


def _prepare_cleanup(
    *,
    close: bool,
    context: CommandContext,
    dry_run: bool,
    pr: str | None,
    revset: str | None,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    state_store = context.state_store
    state = state_store.load()
    if not dry_run:
        state_store.require_writable()
    selected_change_ids = _resolve_cleanup_change_ids(
        context=context,
        pr=pr,
        revset=revset,
        state=state,
    )

    return PreparedCleanup(
        close_open_prs=close,
        context=context,
        github_target=None,
        dry_run=dry_run,
        selected_change_ids=selected_change_ids,
        state=state,
    )


def _resolve_cleanup_change_ids(
    *,
    context: CommandContext,
    pr: str | None,
    revset: str | None,
    state: TrackingState,
) -> tuple[str, ...] | None:
    """Resolve an optional cleanup selector to saved change IDs."""

    if pr == "orphans":
        repo_paths = observe_repo_paths(
            jj_client=context.jj_client,
            state=state,
        )
        tracked_stacks = tuple(path.stack for path in repo_paths.paths if path.tracked_change_ids)
        return tuple(
            orphan.change_id for orphan in enumerate_orphaned_records(state, tracked_stacks)
        )
    if pr is not None:
        pr_number, repo = resolve_pr_reference(
            jj_client=context.jj_client,
            pr_reference=pr,
        )
        matches = tuple(
            change_id
            for change_id, identity in state.pr_identities.items()
            if identity.pr_number == pr_number
        )
        if len(matches) > 1:
            pr_label = format_pr_label(pr_number, repo=repo)
            raise AmbiguousSelectionError(
                t"Multiple saved links claim {pr_label}.",
                hint=t"Run {ui.cmd('list')} to inspect them and repair the incorrect link.",
            )
        if not matches:
            pr_label = format_pr_label(pr_number, repo=repo)
            raise CliError(
                t"{pr_label} is not linked to any local change.",
                hint=t"Run {ui.cmd('checkout')} or {ui.cmd('relink')} to link it first, or "
                t"close it with {ui.cmd(f'gh pr close {pr_number}')}.",
            )
        return matches
    if revset is None:
        return None
    stack = select_stack_path(
        jj_client=context.jj_client,
        revset=revset,
        state=state,
    ).stack
    return tuple(
        change.change_id for change in stack.changes if change.change_id in state.pr_identities
    )


async def _run_cleanup_async(
    *,
    github_client: GithubClient | None = None,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int] = frozenset(),
    local_observations: dict[str, LocalCleanupObservation],
) -> CleanupResult:
    actions: list[CleanupAction] = []

    def record_action(action: CleanupAction) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    prepared_changes = _run_local_cleanup_pass(
        prepared_cleanup=prepared_cleanup,
        local_observations=local_observations,
    )
    github_target = prepared_cleanup.github_target
    if isinstance(github_target, GithubTarget) and prepared_changes:
        if github_client is not None:
            await _run_tracked_pr_cleanup_pass(
                github_client=github_client,
                prepared_changes=prepared_changes,
                prepared_cleanup=prepared_cleanup,
                preview_detached_dependents=preview_detached_dependents,
                record_action=record_action,
            )
        else:
            async with build_github_client(repo=github_target.repo) as client:
                await _run_tracked_pr_cleanup_pass(
                    github_client=client,
                    prepared_changes=prepared_changes,
                    prepared_cleanup=prepared_cleanup,
                    preview_detached_dependents=preview_detached_dependents,
                    record_action=record_action,
                )
    elif prepared_changes:
        for prepared_change in prepared_changes:
            candidate = prepared_change.candidate
            record_action(
                CleanupAction(
                    kind="tracking",
                    status="blocked",
                    body=t"cannot inspect PR #{candidate.pr_identity.pr_number} for "
                    t"{ui.change_id(candidate.change_id)} because the GitHub repo "
                    t"cannot be resolved",
                )
            )
    return CleanupResult(actions=tuple(actions))


def _run_local_cleanup_pass(
    *,
    prepared_cleanup: PreparedCleanup,
    local_observations: dict[str, LocalCleanupObservation],
) -> tuple[PreparedCleanupChange, ...]:
    prepared_changes: list[PreparedCleanupChange] = []
    selected_change_ids = prepared_cleanup.selected_change_ids
    change_ids = (
        tuple(prepared_cleanup.state.pr_identities)
        if selected_change_ids is None
        else selected_change_ids
    )
    for change_id in change_ids:
        candidate = prepared_cleanup.state.tracked_pr(change_id)
        if candidate is None:
            continue
        local_observation = local_observations.get(
            change_id,
            LocalCleanupObservation(
                has_mutable_copy=False,
                stale_reason="local change was not inspected",
            ),
        )
        prepared_changes.append(
            PreparedCleanupChange(
                candidate=candidate,
                has_mutable_copy=local_observation.has_mutable_copy,
                stale_reason=local_observation.stale_reason,
            )
        )
    return tuple(prepared_changes)


async def _run_tracked_pr_cleanup_pass(
    *,
    github_client: GithubClient,
    prepared_changes: tuple[PreparedCleanupChange, ...],
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int] = frozenset(),
    record_action: Callable[[CleanupAction], None],
) -> None:
    """Clean exact closed-PR records while preserving open or ambiguous ones."""

    if not prepared_changes:
        return
    remote = prepared_cleanup.remote
    if remote is None:
        raise AssertionError("Tracked PR cleanup requires a configured remote.")
    remote_name = remote.name
    observation = await observe_prs(
        change_ids=tuple(change.candidate.change_id for change in prepared_changes),
        context=prepared_cleanup.context,
        github_client=github_client,
        include_dependents=True,
        include_open_head_prs=True,
        remote_name=remote_name,
    )
    preflights: dict[str, CleanupPreflight] = {}
    eligible_pr_numbers: list[int] = []
    for change in prepared_changes:
        candidate = change.candidate
        preflight = _preflight_tracked_pr_cleanup(
            initial_observation=observation,
            prepared_change=change,
            prepared_cleanup=prepared_cleanup,
            preview_detached_dependents=preview_detached_dependents,
        )
        preflights[candidate.change_id] = preflight
        if preflight[0] is not None:
            eligible_pr_numbers.append(candidate.pr_identity.pr_number)
    stacks, overview_comments = await _observe_cleanup_secondary_facts(
        github_client=github_client,
        pr_numbers=eligible_pr_numbers,
    )
    stack_blockers = github_stack_cleanup_blockers(
        pr_numbers=tuple(eligible_pr_numbers),
        stacks=stacks,
    )
    for prepared_change in prepared_changes:
        stop_after_failure = await _cleanup_tracked_pr(
            github_client=github_client,
            preflight=preflights[prepared_change.candidate.change_id],
            prepared_change=prepared_change,
            prepared_cleanup=prepared_cleanup,
            record_action=record_action,
            remote_name=remote_name,
            stack_blocker=stack_blockers.get(prepared_change.candidate.pr_identity.pr_number),
            overview_comments=overview_comments,
        )
        if stop_after_failure:
            break


async def _observe_cleanup_secondary_facts(
    *,
    github_client: GithubClient,
    pr_numbers: list[int],
) -> tuple[tuple[GithubStack, ...] | CliError, dict[int, GithubIssueComment | None]]:
    """Join the two shared secondary observations with stack errors taking precedence."""

    if not pr_numbers:
        return (), {}
    stacks_task = asyncio.create_task(observe_github_stacks(github=github_client))
    comments_task = asyncio.create_task(
        github_client.find_issue_comments_by_body_marker(
            body_marker=STACK_OVERVIEW_COMMENT_MARKER,
            pr_numbers=pr_numbers,
        )
    )
    await asyncio.gather(stacks_task, comments_task, return_exceptions=True)
    try:
        stacks = await stacks_task
    except CliError as error:
        return error, {}
    return stacks, await comments_task


async def _cleanup_tracked_pr(
    *,
    github_client: GithubClient,
    preflight: CleanupPreflight,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
    stack_blocker: CleanupAction | None,
    overview_comments: dict[int, GithubIssueComment | None],
) -> bool:
    """Apply one planned cleanup, returning whether a partial failure must stop the pass."""

    candidate = prepared_change.candidate
    identity = candidate.pr_identity
    pr, update, early_action = preflight
    if pr is None:
        if early_action is not None:
            record_action(early_action)
        return False
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    if stack_blocker is not None:
        record_action(stack_blocker)
        return False
    overview_comment = overview_comments[identity.pr_number]
    if pr.state == "open":
        close_action = CleanupAction(
            kind="pull request",
            status="planned" if prepared_cleanup.dry_run else "applied",
            body=t"close {pr_label}",
        )
        if not prepared_cleanup.dry_run:
            try:
                await github_client.close_pr(pr_number=identity.pr_number)
            except GithubClientError as error:
                record_action(
                    CleanupAction(
                        kind="pull request",
                        status="blocked",
                        body=t"cannot close {pr_label}: {error}",
                    )
                )
                return True
        record_action(close_action)
    return await _apply_tracked_pr_cleanup(
        branch_update=update,
        overview_comment=overview_comment,
        github_client=github_client,
        pr=pr,
        prepared_change=prepared_change,
        prepared_cleanup=prepared_cleanup,
        record_action=record_action,
        remote_name=remote_name,
    )


def _preflight_tracked_pr_cleanup(
    *,
    initial_observation: RepoFacts,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    preview_detached_dependents: frozenset[int],
) -> CleanupPreflight:
    candidate = prepared_change.candidate
    pr, blocker = check_tracked_pr(
        allowed_states=frozenset({"open", "closed", "merged"}),
        candidate=candidate,
        observation=initial_observation,
    )
    if blocker is not None:
        return None, None, blocker
    if pr is None:
        raise AssertionError("Exact cleanup lookup must return a pull request.")
    if pr.state == "open" and not prepared_cleanup.close_open_prs:
        pr_label = format_pr_label(pr.number, url=pr.html_url)
        action = (
            CleanupAction(
                kind="tracking",
                status="skipped",
                body=t"preserve open orphan {pr_label}",
            )
            if prepared_change.stale_reason is not None
            else None
        )
        return None, None, action
    _pr, update, blocker = plan_pr_cleanup(
        allowed_states=(
            frozenset({"open", "closed", "merged"})
            if prepared_cleanup.close_open_prs
            else frozenset({"closed", "merged"})
        ),
        candidate=candidate,
        observation=initial_observation,
        preview_detached_dependents=preview_detached_dependents,
    )
    if blocker is not None:
        return None, update, blocker
    if pr.state == "merged" and prepared_change.has_mutable_copy:
        pr_label = format_pr_label(pr.number, url=pr.html_url)
        action = CleanupAction(
            kind="tracking",
            status="skipped",
            body=t"preserve merged {pr_label} for "
            t"{ui.change_id(candidate.change_id)}; run "
            t"{ui.cmd(f'sync {short_change_id(candidate.change_id)}')} before cleanup",
        )
        return None, None, action
    return pr, update, None


async def _apply_tracked_pr_cleanup(
    *,
    branch_update: PRRefUpdate | None,
    overview_comment: GithubIssueComment | None,
    github_client: GithubClient,
    pr: GithubPR,
    prepared_change: PreparedCleanupChange,
    prepared_cleanup: PreparedCleanup,
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
) -> bool:
    """Apply checked cleanup, returning whether a partial failure must stop the pass."""

    candidate = prepared_change.candidate
    mutation_started = not prepared_cleanup.dry_run and (
        branch_update is not None or overview_comment is not None
    )
    apply_remote_branch_cleanup(
        dry_run=prepared_cleanup.dry_run,
        jj_client=prepared_cleanup.context.jj_client,
        record_action=record_action,
        remote_name=remote_name,
        update=branch_update,
    )
    comment_actions, comments_current = await apply_overview_comment_cleanup(
        comment=overview_comment,
        dry_run=prepared_cleanup.dry_run,
        github_client=github_client,
        pr_number=candidate.pr_identity.pr_number,
    )
    for action in comment_actions:
        record_action(action)
    if not comments_current:
        return mutation_started
    reason = (
        f" ({prepared_change.stale_reason})" if prepared_change.stale_reason is not None else ""
    )
    action = CleanupAction(
        kind="tracking",
        status="planned" if prepared_cleanup.dry_run else "applied",
        body=t"forget {format_pr_label(pr.number, url=pr.html_url)} for "
        t"{ui.change_id(candidate.change_id)}{reason}",
    )
    if prepared_cleanup.dry_run:
        record_action(action)
    else:
        prepared_cleanup.context.state_store.retire_pr(
            candidate.change_id,
        )
        record_action(action)
    return False


def _load_cleanup_remote_context(*, prepared_cleanup: PreparedCleanup) -> PreparedCleanup:
    """Resolve remote and GitHub target details once plain cleanup actually needs them."""

    if prepared_cleanup.github_target is not None:
        return prepared_cleanup
    return replace(
        prepared_cleanup,
        github_target=resolve_github_target(
            prepared_cleanup.context.jj_client.list_git_remotes()
        ),
    )


def _cleanup_needs_remote_context(
    *,
    prepared_cleanup: PreparedCleanup,
) -> bool:
    """Whether plain cleanup might need remote or GitHub state beyond local checks."""

    return any(
        change_id in prepared_cleanup.state.submitted_baselines
        for change_id in (
            tuple(prepared_cleanup.state.pr_identities)
            if prepared_cleanup.selected_change_ids is None
            else prepared_cleanup.selected_change_ids
        )
    )
