"""Stack status preparation and GitHub inspection helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError, ErrorMessage, error_message
from jj_stack.github.client import (
    GithubClient,
    GithubClientError,
    build_github_client,
)
from jj_stack.github.error_messages import github_action_error_message
from jj_stack.github.resolution import (
    GithubRepoAddress,
    GithubTarget,
    UnresolvedGithubTarget,
    resolve_github_target,
)
from jj_stack.identifiers import short_change_id
from jj_stack.jj.client import JjClient, UnsupportedStackError
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import TrackedPR, TrackingState
from jj_stack.stack.change_state import (
    UNOBSERVED,
    ChangeObservation,
    ChangeState,
    WithPR,
    classify,
    report_incomplete,
)
from jj_stack.stack.selected import select_stack_path, select_stack_path_containing_change

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PRLookup:
    """Raw GitHub facts for one saved PR branch, before classification."""

    # The saved pull request looked up by number; None when GitHub reports none.
    pr: GithubPR | None
    open_prs_on_branch: tuple[GithubPR, ...]
    error: ErrorMessage | None = None


@dataclass(frozen=True, slots=True)
class StackStatusChange:
    """One local change with its classified pull request state."""

    change: LocalCommit
    tracked: TrackedPR | None
    state: ChangeState

    @property
    def change_id(self) -> str:
        return self.change.change_id

    @property
    def commit_id(self) -> str:
        return self.change.commit_id

    @property
    def subject(self) -> str:
        return self.change.subject

    @property
    def branch(self) -> str | None:
        return self.tracked.pr_identity.head_ref if self.tracked is not None else None

    @property
    def pr(self) -> GithubPR | None:
        return self.state.pr if isinstance(self.state, WithPR) else None


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Status result for one selected local stack."""

    github_error: ErrorMessage | None
    github_repo: GithubRepoAddress | None
    incomplete: bool
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    changes: tuple[StackStatusChange, ...]
    selected_revset: str

    @property
    def submitted_state_disagreements(self) -> tuple[str, ...]:
        """Tracked changes whose local commit has moved past the submitted baseline."""

        return tuple(
            change.change_id for change in reversed(self.changes) if change.state.has_local_edits
        )


@dataclass(frozen=True, slots=True)
class PreparedStatus:
    """Locally prepared status inputs before any GitHub inspection."""

    github_target: GithubTarget | UnresolvedGithubTarget
    prepared: PreparedStack

    @property
    def github_repo(self) -> GithubRepoAddress | None:
        target = self.github_target
        return target.repo if isinstance(target, GithubTarget) else None

    @property
    def github_repo_error(self) -> ErrorMessage | None:
        return self.github_target.github_repo_error

    def github_inspection_count(self) -> int:
        """Return how many selected changes need live GitHub inspection."""

        if self.github_repo is None:
            return 0
        return sum(1 for change in self.prepared.status_changes if change.tracked is not None)


@dataclass(frozen=True, slots=True)
class PreparedStack:
    """Prepared local stack inputs shared across inspection-driven commands."""

    client: JjClient
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    stack: LocalStack
    state: TrackingState
    status_changes: tuple[PreparedChange, ...]


@dataclass(frozen=True, slots=True)
class PreparedChange:
    """Local stack change with its saved tracking, if any."""

    change: LocalCommit
    tracked: TrackedPR | None

    @property
    def branch(self) -> str | None:
        return self.tracked.pr_identity.head_ref if self.tracked is not None else None


def status_preparation_cli_error(error: UnsupportedStackError) -> CliError:
    """Translate stack-shape preparation failures into a user-facing CLI error."""

    if error.hint is not None:
        # An error that names its own recovery step already explains itself; prefixing it with
        # a shape summary would bury the hint inside the message.
        return CliError(error_message(error), hint=error.hint)
    return CliError(t"Local history does not form a linear stack. {error}")


def prepare_status(
    *,
    context: CommandContext,
    fetch_remote_state: bool = False,
    revset: str | None,
    containing_change_id: str | None = None,
    inspection_mode: bool = False,
) -> PreparedStatus:
    """Resolve local status inputs before any GitHub network inspection."""

    jj_client = context.jj_client
    state_store = context.state_store
    state = state_store.load()
    github_target = resolve_github_target(jj_client.list_git_remotes())
    if fetch_remote_state and github_target.remote is not None:
        jj_client.fetch_remote(remote=github_target.remote.name)

    if containing_change_id is not None:
        selected_path = select_stack_path_containing_change(
            change_id=containing_change_id,
            inspection_mode=inspection_mode,
            jj_client=jj_client,
            state=state,
        )
    else:
        selected_path = select_stack_path(
            inspection_mode=inspection_mode,
            jj_client=jj_client,
            revset=revset,
            state=state,
        )
    if selected_path.stack.head.hidden:
        # An exact commit ID resolves a hidden predecessor, while `change_id()` does not. Only
        # visible changes are stack members, so refuse both selector forms alike. `checkout`
        # selects its own path because it re-materializes a hidden imported snapshot on purpose.
        selected_revset = ui.revset(selected_path.stack.selected_revset)
        restore = ui.cmd(f"jj new {selected_path.stack.head.commit_id}")
        raise UnsupportedStackError(
            t"Revset {selected_revset} did not resolve to a visible commit.",
            hint=t"Restore it with {restore}, or select a visible change.",
            reason="hidden_commit",
        )
    prepared = prepare_stack_for_status(
        context=context,
        remote=github_target.remote,
        remote_error=github_target.remote_error,
        stack=selected_path.stack,
        state=state,
    )
    logger.debug(
        "status prepared: selected_revset=%s changes=%d remote=%s",
        prepared.stack.selected_revset,
        len(prepared.status_changes),
        prepared.remote.name if prepared.remote is not None else "unavailable",
    )
    return PreparedStatus(
        github_target=github_target,
        prepared=prepared,
    )


def stream_status(
    *,
    prepared_status: PreparedStatus,
    on_change: Callable[[StackStatusChange, bool], None] | None = None,
) -> StatusResult:
    """Inspect GitHub state for a prepared stack and optionally stream results out."""

    return asyncio.run(
        stream_status_async(
            on_change=on_change,
            prepared_status=prepared_status,
        )
    )


async def stream_status_async(
    *,
    on_change: Callable[[StackStatusChange, bool], None] | None,
    prepared_status: PreparedStatus,
) -> StatusResult:
    prepared = prepared_status.prepared
    github_repo = prepared_status.github_repo
    github_repo_error = prepared_status.github_repo_error

    def result(
        changes: tuple[StackStatusChange, ...],
        *,
        github_error: ErrorMessage | None = None,
        github_repo: GithubRepoAddress | None = None,
        remote: GitRemote | None = None,
        remote_error: ErrorMessage | None = None,
    ) -> StatusResult:
        return StatusResult(
            github_error=github_error,
            github_repo=github_repo,
            incomplete=status_is_incomplete(changes),
            remote=remote,
            remote_error=remote_error,
            changes=changes,
            selected_revset=prepared.stack.selected_revset,
        )

    def stream_local(changes: tuple[StackStatusChange, ...]) -> None:
        if on_change is not None:
            for change in changes:
                on_change(change, False)

    fallback_changes = tuple(reversed(build_status_changes_for_prepared_stack(prepared)))
    if prepared.remote is None:
        stream_local(fallback_changes)
        return result(fallback_changes, remote_error=prepared.remote_error)

    if github_repo is None:
        logger.debug("status github target unavailable: %s", github_repo_error)
        stream_local(fallback_changes)
        return result(fallback_changes, github_error=github_repo_error, remote=prepared.remote)

    if not prepared.status_changes:
        return result((), github_repo=github_repo, remote=prepared.remote)

    prepared_changes_for_github = tuple(
        change for change in prepared.status_changes if change.tracked is not None
    )
    if not prepared_changes_for_github:
        return result(fallback_changes, github_repo=github_repo, remote=prepared.remote)

    changes: list[StackStatusChange] = []
    try:
        async for change in _iter_status_changes_with_github(
            github_repo=github_repo,
            prepared_changes=prepared_changes_for_github,
            remote_name=prepared.remote.name,
        ):
            changes.append(change)
            if on_change is not None:
                on_change(change, True)
    except CliError as error:
        github_error = error_message(error)
        logger.debug("status github inspection failed: %s", github_error)
        streamed_change_ids = {change.change_id for change in changes}
        stream_local(
            tuple(
                change
                for change in fallback_changes
                if change.change_id not in streamed_change_ids
            )
        )
        return result(
            fallback_changes,
            github_error=github_error,
            github_repo=github_repo,
            remote=prepared.remote,
        )

    changes_by_change_id = {change.change_id: change for change in changes}
    display_changes = tuple(
        changes_by_change_id.get(change.change_id, change) for change in fallback_changes
    )
    return result(display_changes, github_repo=github_repo, remote=prepared.remote)


def prepare_stack_for_status(
    *,
    context: CommandContext,
    remote: GitRemote | None,
    remote_error: ErrorMessage | None,
    stack: LocalStack,
    state: TrackingState,
) -> PreparedStack:
    """Build prepared status inputs for one already-resolved local stack."""

    return PreparedStack(
        client=context.jj_client,
        remote=remote,
        remote_error=remote_error,
        stack=stack,
        state=state,
        status_changes=tuple(
            PreparedChange(change=change, tracked=state.tracked_pr(change.change_id))
            for change in stack.changes
        ),
    )


def build_status_changes_for_prepared_stack(
    prepared: PreparedStack,
    *,
    pr_lookups: dict[str, PRLookup] | None = None,
) -> tuple[StackStatusChange, ...]:
    """Classify every prepared change, using the GitHub lookups the caller has."""

    remote_name = prepared.remote.name if prepared.remote is not None else None
    return tuple(
        _status_change(
            change,
            lookup=(
                pr_lookups.get(change.branch)
                if pr_lookups is not None and change.branch is not None
                else None
            ),
            remote_name=remote_name,
        )
        for change in prepared.status_changes
    )


def _status_change(
    prepared_change: PreparedChange,
    *,
    lookup: PRLookup | None,
    remote_name: str | None,
) -> StackStatusChange:
    change = prepared_change.change
    observation = ChangeObservation(
        change_id=change.change_id,
        tracked=prepared_change.tracked,
        branch=prepared_change.branch,
        remote_name=remote_name,
        local=(change,),
        selected=change,
        pr=UNOBSERVED if lookup is None else lookup.pr,
        open_prs_on_branch=UNOBSERVED if lookup is None else lookup.open_prs_on_branch,
        lookup_error=None if lookup is None else lookup.error,
    )
    return StackStatusChange(
        change=change,
        tracked=prepared_change.tracked,
        state=classify(observation),
    )


def status_is_incomplete(changes: tuple[StackStatusChange, ...]) -> bool:
    """Whether any change stops a report from describing the stack completely."""

    return any(report_incomplete(change.state) for change in changes)


async def _iter_status_changes_with_github(
    *,
    github_repo: GithubRepoAddress,
    prepared_changes: tuple[PreparedChange, ...],
    remote_name: str,
) -> AsyncIterator[StackStatusChange]:
    ordered_prepared_changes = tuple(reversed(prepared_changes))
    async with build_github_client(repo=github_repo) as github_client:
        pr_lookups = await _discover_pr_lookups(
            github_client=github_client,
            prepared_changes=ordered_prepared_changes,
        )
        for prepared_change in ordered_prepared_changes:
            branch = _required_branch(prepared_change)
            status_change = _status_change(
                prepared_change,
                lookup=pr_lookups[branch],
                remote_name=remote_name,
            )
            logger.debug(
                "status change inspected: change_id=%s branch=%s state=%s",
                short_change_id(prepared_change.change.change_id),
                branch,
                type(status_change.state).__name__,
            )
            yield status_change


def lookup_pr_lookups(
    *,
    github_repo: GithubRepoAddress,
    on_progress: Callable[[int], None] | None = None,
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, PRLookup]:
    """Return pull-request lookups for saved branches."""

    return asyncio.run(
        lookup_pr_lookups_async(
            github_repo=github_repo,
            on_progress=on_progress,
            prepared_changes=prepared_changes,
        )
    )


async def lookup_pr_lookups_async(
    *,
    github_repo: GithubRepoAddress,
    on_progress: Callable[[int], None] | None = None,
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, PRLookup]:
    """Return pull-request lookups for saved branches."""

    async with build_github_client(repo=github_repo) as github_client:
        pr_lookups = await _discover_pr_lookups(
            github_client=github_client,
            prepared_changes=prepared_changes,
        )
        if on_progress is not None and pr_lookups:
            on_progress(len(pr_lookups))
        return pr_lookups


def _required_branch(change: PreparedChange) -> str:
    if change.branch is None:
        raise AssertionError("GitHub inspection requires an exact saved PR branch.")
    return change.branch


async def _discover_pr_lookups(
    *,
    github_client: GithubClient,
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, PRLookup]:
    """Fetch the open pull requests on each saved branch, then the saved PR when it is not one."""

    tracked_by_branch = {
        _required_branch(change): change.tracked
        for change in prepared_changes
        if change.tracked is not None
    }
    branches = tuple(tracked_by_branch)
    if not branches:
        return {}

    try:
        open_prs_by_branch = await github_client.get_open_prs_by_head_refs(head_refs=branches)
    except GithubClientError as error:
        # Auth failures, missing repos, server errors, and transport failures are repo-level: no
        # per-branch lookup can succeed, so fail the whole inspection rather than reporting
        # per-branch errors.
        status_code = error.status_code
        if status_code is None or status_code in {401, 403, 404} or status_code >= 500:
            raise CliError(
                "",
                hint=t"Run {ui.cmd('jj-stack doctor')} to check GitHub access.",
            ) from error
        lookup_error = github_action_error_message(action="pull request lookup", error=error)
        return {
            branch: PRLookup(pr=None, open_prs_on_branch=(), error=lookup_error)
            for branch in branches
        }

    def saved_open_pr(branch: str) -> GithubPR | None:
        number = tracked_by_branch[branch].pr_identity.pr_number
        return next(
            (pr for pr in open_prs_by_branch.get(branch, ()) if pr.number == number),
            None,
        )

    # The saved PR number is the one reported. Look it up directly when it is not among the
    # open pull requests on its branch.
    remembered = {
        branch: tracked.pr_identity.pr_number
        for branch, tracked in tracked_by_branch.items()
        if saved_open_pr(branch) is None
    }
    remembered_prs: dict[int, GithubPR | None] = {}
    remembered_error: ErrorMessage | None = None
    if remembered:
        try:
            remembered_prs = await github_client.get_prs_by_numbers(
                pr_numbers=tuple(remembered.values()),
            )
        except GithubClientError as error:
            remembered_error = github_action_error_message(
                action="saved pull request lookup",
                error=error,
            )
    return {
        branch: PRLookup(
            pr=(
                saved_open_pr(branch)
                if branch not in remembered
                else remembered_prs.get(remembered[branch])
            ),
            open_prs_on_branch=open_prs_by_branch.get(branch, ()),
            error=remembered_error if branch in remembered else None,
        )
        for branch in branches
    }
