"""Stack status preparation and GitHub inspection helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, replace

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
    ObservationFailed,
    classify,
    live_pr,
    report_incomplete,
)
from jj_stack.stack.selected import select_stack_path, select_stack_path_containing_change

logger = logging.getLogger(__name__)


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
        return live_pr(self.state)


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
class PreparedChange[TrackingT: TrackedPR | None = TrackedPR | None]:
    """Local stack change with its saved tracking, if any."""

    change: LocalCommit
    tracked: TrackingT

    @property
    def branch(self) -> str | None:
        tracked: TrackedPR | None = self.tracked
        return tracked.pr_identity.head_ref if tracked is not None else None


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
        # A commit ID resolves a hidden predecessor, while `change_id()` does not. Only
        # visible changes are stack members, so refuse both selector forms alike. `checkout`
        # selects its own path because it makes an imported hidden commit visible again.
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
    on_progress: Callable[[], None],
) -> StatusResult:
    """Inspect GitHub state for a prepared stack and report progress."""

    return asyncio.run(
        stream_status_async(
            on_progress=on_progress,
            prepared_status=prepared_status,
        )
    )


async def stream_status_async(
    *,
    on_progress: Callable[[], None],
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
        for _change in changes:
            on_progress()

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
        PreparedChange(change=change.change, tracked=change.tracked)
        for change in prepared.status_changes
        if change.tracked is not None
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
            on_progress()
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
            PreparedChange(change=change, tracked=state.prs.get(change.change_id))
            for change in stack.changes
        ),
    )


def build_status_changes_for_prepared_stack(
    prepared: PreparedStack,
    *,
    pr_lookups: dict[str, ChangeObservation] | None = None,
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
    lookup: ChangeObservation | None,
    remote_name: str | None,
) -> StackStatusChange:
    change = prepared_change.change
    observation = (
        replace(lookup, remote_name=remote_name, local=(change,), selected=change)
        if lookup is not None
        else _prepared_observation(prepared_change, remote_name=remote_name)
    )
    return StackStatusChange(
        change=change,
        tracked=prepared_change.tracked,
        state=classify(observation),
    )


def _prepared_observation(
    prepared_change: PreparedChange, *, remote_name: str | None
) -> ChangeObservation:
    change = prepared_change.change
    return ChangeObservation(
        change_id=change.change_id,
        tracked=prepared_change.tracked,
        branch=prepared_change.branch,
        remote_name=remote_name,
        local=(change,),
        selected=change,
    )


def status_is_incomplete(changes: tuple[StackStatusChange, ...]) -> bool:
    """Whether any change stops a report from describing the stack completely."""

    return any(report_incomplete(change.state) for change in changes)


async def _iter_status_changes_with_github(
    *,
    github_repo: GithubRepoAddress,
    prepared_changes: tuple[PreparedChange[TrackedPR], ...],
    remote_name: str,
) -> AsyncIterator[StackStatusChange]:
    ordered_prepared_changes = tuple(reversed(prepared_changes))
    async with build_github_client(repo=github_repo) as github_client:
        pr_lookups = await discover_pr_lookups(
            github_client=github_client,
            observations=_observations_by_branch(
                ordered_prepared_changes, remote_name=remote_name
            ),
        )
        for prepared_change in ordered_prepared_changes:
            branch = prepared_change.tracked.pr_identity.head_ref
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
    on_progress: Callable[[int], None],
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, ChangeObservation]:
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
    on_progress: Callable[[int], None],
    prepared_changes: tuple[PreparedChange, ...],
) -> dict[str, ChangeObservation]:
    """Return pull-request lookups for saved branches."""

    async with build_github_client(repo=github_repo) as github_client:
        pr_lookups = await discover_pr_lookups(
            github_client=github_client,
            observations=_observations_by_branch(prepared_changes, remote_name=None),
        )
        if pr_lookups:
            on_progress(len(pr_lookups))
        return pr_lookups


def _observations_by_branch(
    prepared_changes: tuple[PreparedChange, ...], *, remote_name: str | None
) -> dict[str, ChangeObservation]:
    return {
        change.tracked.pr_identity.head_ref: _prepared_observation(
            change, remote_name=remote_name
        )
        for change in prepared_changes
        if change.tracked is not None
    }


async def discover_pr_lookups(
    *,
    github_client: GithubClient,
    observations: Mapping[str, ChangeObservation],
) -> dict[str, ChangeObservation]:
    """Fetch the open pull requests on each branch, then each saved PR that is not one of them.

    A branch with no saved pull request yields only the open pull requests GitHub reports for
    it, which is what a first submit needs to know.
    """

    branches = tuple(observations)
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
            branch: replace(
                observations[branch], open_prs_on_branch=ObservationFailed(lookup_error)
            )
            for branch in branches
        }

    saved_open = {
        branch: next(
            (
                pr
                for pr in open_prs_by_branch.get(branch, ())
                if pr.number == tracked.pr_identity.pr_number
            ),
            None,
        )
        for branch, observation in observations.items()
        if (tracked := observation.tracked) is not None
    }
    # The saved PR number is the one reported. Look it up directly when it is not among the
    # open pull requests on its branch.
    remembered = {
        branch: tracked.pr_identity.pr_number
        for branch, observation in observations.items()
        if (tracked := observation.tracked) is not None and saved_open[branch] is None
    }
    remembered_prs: Mapping[int, GithubPR | None | ObservationFailed] = {}
    if remembered:
        try:
            remembered_prs = await github_client.get_prs_by_numbers(
                pr_numbers=tuple(remembered.values()),
            )
        except GithubClientError as error:
            failure = ObservationFailed(
                github_action_error_message(action="saved pull request lookup", error=error)
            )
            remembered_prs = dict.fromkeys(remembered.values(), failure)
    return {
        branch: replace(
            observation,
            pr=(
                remembered_prs.get(number)
                if (number := remembered.get(branch)) is not None
                else saved_open.get(branch, UNOBSERVED)
            ),
            open_prs_on_branch=open_prs_by_branch.get(branch, ()),
        )
        for branch, observation in observations.items()
    }
