"""Undo an interrupted jj-review operation.

Finds any operation that was cut short, retracts what it completed (closes
opened PRs, deletes pushed review branches, forgets local bookmarks, clears
tracking data), and cleans up the leftover operation state.

Use `--dry-run` to preview what would be undone without changing anything.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review.bootstrap import bootstrap_context
from jj_review.cache import ReviewStateStore
from jj_review.formatting import short_change_id
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.resolution import parse_github_repo, select_submit_remote
from jj_review.intent import intent_is_stale, pid_is_alive, write_new_intent
from jj_review.jj import JjClient, JjCommandError
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.intent import (
    AbortIntent,
    CleanupRestackIntent,
    CloseIntent,
    LandIntent,
    LoadedIntent,
    RelinkIntent,
    SubmitIntent,
)

HELP = "Undo an interrupted jj-review operation"

logger = logging.getLogger(__name__)

AbortActionStatus = Literal["applied", "blocked", "planned", "skipped"]


@dataclass(frozen=True, slots=True)
class AbortAction:
    """One retraction step that was planned, applied, blocked, or skipped."""

    kind: str
    message: str
    status: AbortActionStatus


@dataclass(frozen=True, slots=True)
class AbortResult:
    """Outcome of aborting one intent."""

    actions: tuple[AbortAction, ...]
    applied: bool
    dry_run: bool
    intent_kind: str
    intent_label: str
    intent_started_at: str


def abort(
    *,
    config_path: Path | None,
    debug: bool,
    dry_run: bool,
    repository: Path | None,
) -> int:
    """CLI entrypoint for `abort`."""

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )

    state_store = ReviewStateStore.for_repo(context.repo_root)
    jj_client = JjClient(context.repo_root)
    loaded_intents = state_store.list_intents()

    # Separate any AbortIntent lock files from the real operation intents.
    # A live-PID AbortIntent means another abort is already running; bail.
    # A dead-PID AbortIntent is a stale lock from a previous crash; clean it up.
    abort_locks = [
        loaded for loaded in loaded_intents if isinstance(loaded.intent, AbortIntent)
    ]
    operation_intents = [
        loaded for loaded in loaded_intents if not isinstance(loaded.intent, AbortIntent)
    ]

    for loaded in abort_locks:
        if pid_is_alive(loaded.intent.pid):
            print(
                f"Another abort operation is already in progress "
                f"(PID {loaded.intent.pid}). "
                "Wait for it to finish, then run abort again."
            )
            return 1
        loaded.path.unlink(missing_ok=True)

    loaded_intents = operation_intents

    if not loaded_intents:
        print("Nothing to abort.")
        return 0

    def _resolve_change_id(change_id: str) -> bool:
        try:
            jj_client.resolve_revision(change_id)
            return True
        except (JjCommandError, Exception):
            return False

    outstanding = [
        loaded
        for loaded in loaded_intents
        if not intent_is_stale(loaded.intent, _resolve_change_id)
    ]

    if not outstanding:
        count = len(loaded_intents)
        noun = "operation" if count == 1 else "operations"
        print(
            f"{count} stale incomplete {noun} found "
            "(changes no longer exist in this repo). "
            "Run `cleanup` to remove stale jj-review data."
        )
        return 1

    # Refuse to retract intents whose process is still running — aborting a
    # live operation would race against it and corrupt shared state.
    live = [loaded for loaded in outstanding if pid_is_alive(loaded.intent.pid)]
    outstanding = [loaded for loaded in outstanding if not pid_is_alive(loaded.intent.pid)]

    for loaded in live:
        print(
            f"'{loaded.intent.label}' is still in progress "
            f"(PID {loaded.intent.pid}) — wait for it to finish, then run abort again."
        )

    if not outstanding:
        return 1

    # Write an abort lock so concurrent abort processes bail rather than
    # racing. The lock is removed in the finally block regardless of outcome.
    abort_lock_path = write_new_intent(
        state_store.state_dir,
        AbortIntent(
            kind="abort",
            pid=os.getpid(),
            label="abort",
            started_at=datetime.now(UTC).isoformat(),
        ),
    )

    # Resolve the remote and GitHub target once for all intents.
    remote = None
    github_repository = None
    try:
        remotes = jj_client.list_git_remotes()
        if remotes:
            remote = select_submit_remote(remotes)
            github_repository = parse_github_repo(remote)
    except Exception as error:  # noqa: BLE001
        logger.debug("Could not resolve remote or GitHub target: %s", error)

    exit_code = 1 if live else 0
    try:
        for loaded in outstanding:
            result = asyncio.run(
                _abort_intent_async(
                    dry_run=dry_run,
                    github_repository=github_repository,
                    jj_client=jj_client,
                    loaded=loaded,
                    remote=remote,
                    state_store=state_store,
                )
            )
            _print_abort_result(result)
            if not result.applied and not result.dry_run:
                exit_code = 1
    finally:
        abort_lock_path.unlink(missing_ok=True)

    return exit_code


# ---------------------------------------------------------------------------
# Per-intent dispatch
# ---------------------------------------------------------------------------


async def _abort_intent_async(
    *,
    dry_run: bool,
    github_repository,
    jj_client: JjClient,
    loaded: LoadedIntent,
    remote,
    state_store: ReviewStateStore,
) -> AbortResult:
    intent = loaded.intent

    if isinstance(intent, SubmitIntent):
        return await _abort_submit(
            dry_run=dry_run,
            github_repository=github_repository,
            intent=intent,
            intent_path=loaded.path,
            jj_client=jj_client,
            remote=remote,
            state_store=state_store,
        )

    # For all other intent types, only the intent file can be removed.  The
    # operations themselves either mutate local jj history in ways that aren't
    # straightforwardly reversible (restack, land) or have no per-change state
    # tracked in the intent (cleanup, relink, close).
    note = _non_submit_note(intent)
    actions: list[AbortAction] = []
    if note:
        actions.append(
            AbortAction(kind="note", message=note, status="skipped")
        )
    _plan_intent_file_removal(
        actions=actions, dry_run=dry_run, intent_path=loaded.path
    )
    if not dry_run:
        loaded.path.unlink(missing_ok=True)

    return AbortResult(
        actions=tuple(actions),
        applied=not dry_run,
        dry_run=dry_run,
        intent_kind=intent.kind,
        intent_label=intent.label,
        intent_started_at=intent.started_at,
    )


def _non_submit_note(intent) -> str | None:
    if isinstance(intent, LandIntent):
        return (
            "Landing cannot be retracted; changes already merged to trunk are "
            "permanent. The intent file will be removed so future commands can "
            "proceed. Run `status` to inspect the current state."
        )
    if isinstance(intent, CleanupRestackIntent):
        return (
            "Restack changes to local jj history cannot be automatically reversed. "
            "The intent file will be removed. Inspect with `jj log` and repair "
            "manually if needed."
        )
    if isinstance(intent, CloseIntent):
        return (
            "Close operations cannot be automatically reversed here. "
            "The intent file will be removed. Run `status` to inspect which "
            "pull requests were closed, and reopen them on GitHub if needed."
        )
    if isinstance(intent, RelinkIntent):
        return (
            "Relink changes which PR a change tracks in local data. "
            "The intent file will be removed. Run `status` to confirm the "
            "current link state looks correct."
        )
    return None


# ---------------------------------------------------------------------------
# Submit retraction
# ---------------------------------------------------------------------------


async def _abort_submit(
    *,
    dry_run: bool,
    github_repository,
    intent: SubmitIntent,
    intent_path: Path,
    jj_client: JjClient,
    remote,
    state_store: ReviewStateStore,
) -> AbortResult:
    """Retract a partial submit: close PRs, delete remote branches, clear state."""

    actions: list[AbortAction] = []
    state = state_store.load()
    next_changes = dict(state.changes)
    remote_name = remote.name if remote is not None else None

    if github_repository is not None:
        async with build_github_client(
            base_url=github_repository.api_base_url
        ) as github_client:
            for change_id in intent.ordered_change_ids:
                await _retract_one_change(
                    actions=actions,
                    bookmark=intent.bookmarks.get(change_id),
                    cached=state.changes.get(change_id),
                    change_id=change_id,
                    dry_run=dry_run,
                    github_client=github_client,
                    github_repository=github_repository,
                    jj_client=jj_client,
                    next_changes=next_changes,
                    remote_name=remote_name,
                )
    else:
        # GitHub unreachable — do local cleanup only.
        for change_id in intent.ordered_change_ids:
            _retract_one_change_local(
                actions=actions,
                bookmark=intent.bookmarks.get(change_id),
                cached=state.changes.get(change_id),
                change_id=change_id,
                dry_run=dry_run,
                jj_client=jj_client,
                next_changes=next_changes,
                remote_name=remote_name,
            )
        actions.append(
            AbortAction(
                kind="github",
                message=(
                    "GitHub unreachable — pull request close skipped; "
                    "run `status --fetch` after fixing GitHub access"
                ),
                status="skipped",
            )
        )

    _plan_state_save(
        actions=actions,
        dry_run=dry_run,
        next_changes=next_changes,
        state=state,
    )
    if not dry_run and next_changes != dict(state.changes):
        state_store.save(state.model_copy(update={"changes": next_changes}))

    _plan_intent_file_removal(
        actions=actions, dry_run=dry_run, intent_path=intent_path
    )
    if not dry_run:
        intent_path.unlink(missing_ok=True)

    return AbortResult(
        actions=tuple(actions),
        applied=not dry_run,
        dry_run=dry_run,
        intent_kind=intent.kind,
        intent_label=intent.label,
        intent_started_at=intent.started_at,
    )


async def _retract_one_change(
    *,
    actions: list[AbortAction],
    bookmark: str | None,
    cached: CachedChange | None,
    change_id: str,
    dry_run: bool,
    github_client: GithubClient,
    github_repository,
    jj_client: JjClient,
    next_changes: dict[str, CachedChange],
    remote_name: str | None,
) -> None:
    label = short_change_id(change_id)

    pr_number = cached.pr_number if cached is not None else None
    pr_state = cached.pr_state if cached is not None else None

    if pr_number is not None and pr_state not in ("closed", "merged"):
        action_msg = f"close PR #{pr_number} for {label}"
        if dry_run:
            actions.append(AbortAction(kind="pull request", message=action_msg, status="planned"))
        else:
            try:
                await github_client.close_pull_request(
                    github_repository.owner,
                    github_repository.repo,
                    pull_number=pr_number,
                )
                actions.append(
                    AbortAction(kind="pull request", message=action_msg, status="applied")
                )
            except GithubClientError as error:
                # 404: PR no longer exists. 422: PR is already closed.
                # Either way the desired end state (closed/gone) is already reached.
                if error.status_code in (404, 422):
                    actions.append(
                        AbortAction(kind="pull request", message=action_msg, status="applied")
                    )
                else:
                    actions.append(
                        AbortAction(
                            kind="pull request",
                            message=f"could not close PR #{pr_number} for {label}: {error}",
                            status="blocked",
                        )
                    )

    _retract_one_change_local(
        actions=actions,
        bookmark=bookmark,
        cached=cached,
        change_id=change_id,
        dry_run=dry_run,
        jj_client=jj_client,
        next_changes=next_changes,
        remote_name=remote_name,
    )


def _retract_one_change_local(
    *,
    actions: list[AbortAction],
    bookmark: str | None,
    cached: CachedChange | None,
    change_id: str,
    dry_run: bool,
    jj_client: JjClient,
    next_changes: dict[str, CachedChange],
    remote_name: str | None,
) -> None:
    label = short_change_id(change_id)

    if bookmark is not None:
        bm_state = jj_client.get_bookmark_state(bookmark)

        if remote_name is not None:
            remote_target = bm_state.remote_target(remote_name)
            if remote_target is not None and remote_target.target is not None:
                remote_commit_id = remote_target.target
                action_msg = f"delete remote branch {bookmark}@{remote_name} for {label}"
                if dry_run:
                    actions.append(
                        AbortAction(kind="remote branch", message=action_msg, status="planned")
                    )
                else:
                    try:
                        jj_client.delete_remote_bookmarks(
                            remote=remote_name,
                            deletions=((bookmark, remote_commit_id),),
                        )
                        actions.append(
                            AbortAction(
                                kind="remote branch", message=action_msg, status="applied"
                            )
                        )
                    except JjCommandError as error:
                        actions.append(
                            AbortAction(
                                kind="remote branch",
                                message=(
                                    f"could not delete remote branch {bookmark}: {error}"
                                ),
                                status="blocked",
                            )
                        )

        if bm_state.local_target is not None:
            action_msg = f"forget local bookmark {bookmark} for {label}"
            if dry_run:
                actions.append(
                    AbortAction(kind="local bookmark", message=action_msg, status="planned")
                )
            else:
                try:
                    jj_client.forget_bookmarks((bookmark,))
                    actions.append(
                        AbortAction(kind="local bookmark", message=action_msg, status="applied")
                    )
                except JjCommandError as error:
                    actions.append(
                        AbortAction(
                            kind="local bookmark",
                            message=f"could not forget bookmark {bookmark}: {error}",
                            status="blocked",
                        )
                    )

    if change_id in next_changes:
        del next_changes[change_id]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _plan_state_save(
    *,
    actions: list[AbortAction],
    dry_run: bool,
    next_changes: dict[str, CachedChange],
    state: ReviewState,
) -> None:
    if next_changes != dict(state.changes):
        verb = "would clear" if dry_run else "cleared"
        actions.append(
            AbortAction(
                kind="saved state",
                message=f"{verb} saved jj-review data for aborted changes",
                status="planned" if dry_run else "applied",
            )
        )


def _plan_intent_file_removal(
    *,
    actions: list[AbortAction],
    dry_run: bool,
    intent_path: Path,
) -> None:
    verb = "would remove" if dry_run else "removed"
    actions.append(
        AbortAction(
            kind="intent file",
            message=f"{verb} intent file {intent_path.name}",
            status="planned" if dry_run else "applied",
        )
    )


# ---------------------------------------------------------------------------
# Output rendering
# ---------------------------------------------------------------------------


def _print_abort_result(result: AbortResult) -> None:
    if result.dry_run:
        header = f"Planned abort actions for {result.intent_label!r}:"
    elif result.applied:
        header = f"Applied abort actions for {result.intent_label!r}:"
    else:
        header = f"Abort incomplete for {result.intent_label!r}:"

    print(header)
    for action in result.actions:
        prefix = {
            "applied": "  ✓",
            "planned": "  ~",
            "blocked": "  ✗",
            "skipped": "  -",
        }.get(action.status, "  ?")
        print(f"{prefix} {action.message}")
