"""Conservative cleanup of stale local and remote review state."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jj_review.cache import ReviewStateStore
from jj_review.commands.review_state import (
    PreparedStatus,
    ReviewStatusRevision,
    prepare_status,
    stream_status,
)
from jj_review.commands.submit import (
    _STACK_COMMENT_MARKER,
    ResolvedGithubRepository,
    _build_github_client,
    resolve_github_repository,
    select_submit_remote,
)
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.jj import JjClient
from jj_review.jj.client import UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubIssueComment, GithubPullRequest

CleanupActionStatus = Literal["applied", "blocked", "planned"]
_GITHUB_INSPECTION_CONCURRENCY = 4


class CleanupError(CliError):
    """Raised when cleanup cannot safely inspect remote review state."""


@dataclass(frozen=True, slots=True)
class CleanupAction:
    """One cleanup action that was planned, applied, or blocked."""

    kind: str
    message: str
    status: CleanupActionStatus


@dataclass(frozen=True, slots=True)
class CleanupResult:
    """Rendered cleanup result for the selected repository."""

    actions: tuple[CleanupAction, ...]
    applied: bool
    github_error: str | None
    github_repository: str | None
    remote: GitRemote | None
    remote_error: str | None


@dataclass(frozen=True, slots=True)
class PreparedCleanup:
    """Locally prepared cleanup inputs before any GitHub inspection."""

    apply: bool
    bookmark_states: dict[str, BookmarkState]
    github_repository: ResolvedGithubRepository | None
    github_repository_error: str | None
    jj_client: JjClient
    remote: GitRemote | None
    remote_error: str | None
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
class PreparedCleanupChange:
    """Locally prepared cleanup state for one cached change."""

    bookmark_state: BookmarkState
    cached_change: CachedChange
    change_id: str
    inspect_stack_comment: bool
    stale_reason: str | None


@dataclass(frozen=True, slots=True)
class RestackResult:
    """Rendered restack result for one selected local stack."""

    actions: tuple[CleanupAction, ...]
    applied: bool
    blocked: bool
    github_error: str | None
    github_repository: str | None
    remote: GitRemote | None
    remote_error: str | None
    selected_revset: str


@dataclass(frozen=True, slots=True)
class PreparedRestack:
    """Locally prepared restack inputs before any rewrite."""

    apply: bool
    prepared_status: PreparedStatus


def run_cleanup(
    *,
    apply: bool,
    config: RepoConfig,
    repo_root: Path,
) -> CleanupResult:
    """Plan or apply conservative sparse-state cleanup actions."""

    prepared_cleanup = prepare_cleanup(
        apply=apply,
        config=config,
        repo_root=repo_root,
    )
    return stream_cleanup(prepared_cleanup=prepared_cleanup)


def prepare_cleanup(
    *,
    apply: bool,
    config: RepoConfig,
    repo_root: Path,
) -> PreparedCleanup:
    """Resolve local cleanup inputs before any GitHub network inspection."""

    jj_client = JjClient(repo_root)
    state_store = ReviewStateStore.for_repo(repo_root)
    state = state_store.load()

    remote, remote_error = _resolve_remote(config=config, jj_client=jj_client)
    github_repository, github_error = _resolve_github_repository(
        config=config,
        remote=remote,
    )
    bookmark_states = _load_bookmark_states(
        jj_client=jj_client,
        remote=remote,
        state=state,
    )

    return PreparedCleanup(
        apply=apply,
        bookmark_states=bookmark_states,
        github_repository=github_repository,
        github_repository_error=github_error,
        jj_client=jj_client,
        remote=remote,
        remote_error=remote_error,
        state=state,
        state_store=state_store,
    )


def prepare_restack(
    *,
    apply: bool,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> PreparedRestack:
    """Resolve local restack inputs before any rewrite."""

    return PreparedRestack(
        apply=apply,
        prepared_status=prepare_status(
            change_overrides=change_overrides,
            config=config,
            fetch_remote_state=True,
            repo_root=repo_root,
            revset=revset,
        ),
    )


def stream_cleanup(
    *,
    on_action: Callable[[CleanupAction], None] | None = None,
    prepared_cleanup: PreparedCleanup,
) -> CleanupResult:
    """Inspect GitHub state for prepared cleanup inputs and optionally stream actions."""

    return asyncio.run(
        _stream_cleanup_async(
            on_action=on_action,
            prepared_cleanup=prepared_cleanup,
        )
    )


def stream_restack(
    *,
    on_action: Callable[[CleanupAction], None] | None = None,
    prepared_restack: PreparedRestack,
) -> RestackResult:
    """Inspect and optionally apply a local restack plan for merged path changes."""

    status_result = stream_status(prepared_status=prepared_restack.prepared_status)
    return _build_restack_result(
        on_action=on_action,
        prepared_restack=prepared_restack,
        status_result=status_result,
    )


def _build_restack_result(
    *,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_restack: PreparedRestack,
    status_result,
) -> RestackResult:
    prepared_status = prepared_restack.prepared_status
    prepared = prepared_status.prepared
    revisions_by_change_id = {
        revision.change_id: revision for revision in status_result.revisions
    }
    path_revisions = tuple(
        revisions_by_change_id[prepared_revision.revision.change_id]
        for prepared_revision in prepared.status_revisions
        if prepared_revision.revision.change_id in revisions_by_change_id
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
                message=(
                    "cannot compute a restack plan without live GitHub pull request state; "
                    "fix GitHub access and retry"
                ),
                status="blocked",
            )
        )
        return RestackResult(
            actions=tuple(actions),
            applied=prepared_restack.apply,
            blocked=True,
            github_error=status_result.github_error,
            github_repository=status_result.github_repository,
            remote=status_result.remote,
            remote_error=status_result.remote_error,
            selected_revset=status_result.selected_revset,
        )

    blocked = False
    merged_revisions = tuple(
        revision for revision in path_revisions if _revision_has_merged_pull_request(revision)
    )
    if not merged_revisions:
        return RestackResult(
            actions=(),
            applied=prepared_restack.apply,
            blocked=False,
            github_error=status_result.github_error,
            github_repository=status_result.github_repository,
            remote=status_result.remote,
            remote_error=status_result.remote_error,
            selected_revset=status_result.selected_revset,
        )

    closed_unmerged_revisions = tuple(
        revision for revision in path_revisions if _revision_is_closed_unmerged(revision)
    )
    for revision in closed_unmerged_revisions:
        blocked = True
        record_action(
            CleanupAction(
                kind="restack",
                message=(
                    f"cannot restack past {_revision_label(revision)} because PR "
                    f"#{_revision_pull_request_number(revision)} is closed without merge; "
                    "decide whether to keep or drop that change first"
                ),
                status="blocked",
            )
        )

    survivor_change_ids: list[str] = []
    rebase_plans: list[tuple[str, str | None]] = []
    for prepared_revision in prepared.status_revisions:
        revision = revisions_by_change_id.get(prepared_revision.revision.change_id)
        if revision is None:
            continue
        if _revision_has_merged_pull_request(revision):
            continue
        if _revision_is_closed_unmerged(revision):
            continue
        if revision.local_divergent:
            blocked = True
            record_action(
                CleanupAction(
                    kind="restack",
                    message=(
                        f"cannot restack {_revision_label(revision)} while multiple visible "
                        "revisions still share that change ID"
                    ),
                    status="blocked",
                )
            )
            survivor_change_ids.append(revision.change_id)
            continue

        desired_parent_change_id = survivor_change_ids[-1] if survivor_change_ids else None
        parent_commit_id = prepared_revision.revision.only_parent_commit_id()
        parent_is_merged = False
        for candidate in prepared.status_revisions:
            if candidate.revision.commit_id == parent_commit_id:
                parent_is_merged = _revision_has_merged_pull_request(
                    revisions_by_change_id[candidate.revision.change_id]
                )
                break
        if parent_is_merged:
            rebase_plans.append((revision.change_id, desired_parent_change_id))
        survivor_change_ids.append(revision.change_id)

    client = prepared.client
    if prepared_restack.apply and not blocked:
        for source_change_id, destination_change_id in rebase_plans:
            source_revision = client.resolve_revision(source_change_id)
            if destination_change_id is None:
                destination_revision = prepared.stack.trunk.commit_id
            else:
                destination_revision = client.resolve_revision(destination_change_id).commit_id
            if source_revision.only_parent_commit_id() == destination_revision:
                continue
            client.rebase_revision(
                source=source_change_id,
                destination=destination_revision,
            )
            record_action(
                CleanupAction(
                    kind="restack",
                    message=(
                        f"rebase {_short_change_id(source_change_id)} onto "
                        f"{_restack_destination_label(destination_change_id)}"
                    ),
                    status="applied",
                )
            )
    else:
        for source_change_id, destination_change_id in rebase_plans:
            status = "blocked" if blocked else "planned"
            message = (
                f"rebase {_short_change_id(source_change_id)} onto "
                f"{_restack_destination_label(destination_change_id)}"
            )
            if blocked and closed_unmerged_revisions:
                message = f"{message} once blocked path changes are resolved"
            record_action(
                CleanupAction(
                    kind="restack",
                    message=message,
                    status=status,
                )
            )

    for revision in merged_revisions:
        pull_request_number = _revision_pull_request_number(revision)
        if pull_request_number is None:
            continue
        base_ref = _revision_pull_request_base_ref(revision)
        if base_ref is None or not base_ref.startswith("review/"):
            continue
        record_action(
            CleanupAction(
                kind="policy",
                message=(
                    f"PR #{pull_request_number} merged into review branch {base_ref}; "
                    "configure GitHub to block merges of PRs targeting `review/*`"
                ),
                status="planned",
            )
        )

    if not actions and merged_revisions:
        merged_labels = ", ".join(_revision_label(revision) for revision in merged_revisions)
        record_action(
            CleanupAction(
                kind="restack",
                message=(
                    f"merged review units remain on the selected path ({merged_labels}), but "
                    "no surviving descendants need to move"
                ),
                status="planned" if not prepared_restack.apply else "applied",
            )
        )

    return RestackResult(
        actions=tuple(actions),
        applied=prepared_restack.apply,
        blocked=blocked,
        github_error=status_result.github_error,
        github_repository=status_result.github_repository,
        remote=status_result.remote,
        remote_error=status_result.remote_error,
        selected_revset=status_result.selected_revset,
    )


def _revision_has_merged_pull_request(revision: ReviewStatusRevision) -> bool:
    lookup = revision.pull_request_lookup
    return (
        lookup is not None
        and lookup.state == "closed"
        and lookup.pull_request is not None
        and lookup.pull_request.state == "merged"
    )


def _revision_is_closed_unmerged(revision: ReviewStatusRevision) -> bool:
    lookup = revision.pull_request_lookup
    return (
        lookup is not None
        and lookup.state == "closed"
        and lookup.pull_request is not None
        and lookup.pull_request.state != "merged"
    )


def _revision_pull_request_number(revision: ReviewStatusRevision) -> int | None:
    lookup = revision.pull_request_lookup
    if lookup is None or lookup.pull_request is None:
        return None
    return lookup.pull_request.number


def _revision_pull_request_base_ref(revision: ReviewStatusRevision) -> str | None:
    lookup = revision.pull_request_lookup
    if lookup is None or lookup.pull_request is None:
        return None
    return lookup.pull_request.base.ref


def _revision_label(revision: ReviewStatusRevision) -> str:
    return f"{revision.subject} [{_short_change_id(revision.change_id)}]"


def _short_change_id(change_id: str) -> str:
    return change_id[:8]


def _restack_destination_label(destination_change_id: str | None) -> str:
    if destination_change_id is None:
        return "trunk()"
    return _short_change_id(destination_change_id)


async def _stream_cleanup_async(
    *,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_cleanup: PreparedCleanup,
) -> CleanupResult:
    next_changes = dict(prepared_cleanup.state.changes)
    actions: list[CleanupAction] = []
    apply = prepared_cleanup.apply
    remote = prepared_cleanup.remote
    jj_client = prepared_cleanup.jj_client

    def record_action(action: CleanupAction) -> None:
        actions.append(action)
        if on_action is not None:
            on_action(action)

    if prepared_cleanup.github_repository is None:
        for change_id, cached_change in prepared_cleanup.state.changes.items():
            prepared_change = _prepare_cleanup_change(
                cached_change=cached_change,
                change_id=change_id,
                prepared_cleanup=prepared_cleanup,
            )
            stale_reason = prepared_change.stale_reason
            if stale_reason is None:
                continue
            record_action(
                _cache_action(
                    change_id=change_id,
                    reason=stale_reason,
                    status="applied" if apply else "planned",
                )
            )
            if apply:
                next_changes.pop(change_id, None)

            remote_plan = _plan_remote_branch_cleanup(
                bookmark_state=prepared_change.bookmark_state,
                cached_change=cached_change,
                remote=remote,
            )
            if remote_plan is not None:
                remote_action = remote_plan.action
                if (
                    apply
                    and remote_action.status == "planned"
                    and remote is not None
                    and remote_plan.expected_remote_target is not None
                ):
                    jj_client.delete_remote_bookmark(
                        remote=remote.name,
                        bookmark=cached_change.bookmark or "",
                        expected_remote_target=remote_plan.expected_remote_target,
                    )
                    remote_action = CleanupAction(
                        kind=remote_plan.action.kind,
                        message=remote_plan.action.message,
                        status="applied",
                    )
                record_action(remote_action)

        if apply and next_changes != prepared_cleanup.state.changes:
            prepared_cleanup.state_store.save(
                prepared_cleanup.state.model_copy(update={"changes": next_changes})
            )

        return CleanupResult(
            actions=tuple(actions),
            applied=apply,
            github_error=prepared_cleanup.github_repository_error,
            github_repository=None,
            remote=remote,
            remote_error=prepared_cleanup.remote_error,
        )

    github_repository = prepared_cleanup.github_repository
    async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
        prepared_changes: list[PreparedCleanupChange] = []
        for change_id, cached_change in prepared_cleanup.state.changes.items():
            prepared_change = _prepare_cleanup_change(
                cached_change=cached_change,
                change_id=change_id,
                prepared_cleanup=prepared_cleanup,
            )
            prepared_changes.append(prepared_change)

            stale_reason = prepared_change.stale_reason
            if stale_reason is None:
                continue

            record_action(
                _cache_action(
                    change_id=change_id,
                    reason=stale_reason,
                    status="applied" if apply else "planned",
                )
            )
            if apply:
                next_changes.pop(change_id, None)

            remote_plan = _plan_remote_branch_cleanup(
                bookmark_state=prepared_change.bookmark_state,
                cached_change=cached_change,
                remote=remote,
            )
            if remote_plan is not None:
                remote_action = remote_plan.action
                if (
                    apply
                    and remote_action.status == "planned"
                    and remote is not None
                    and remote_plan.expected_remote_target is not None
                ):
                    jj_client.delete_remote_bookmark(
                        remote=remote.name,
                        bookmark=cached_change.bookmark or "",
                        expected_remote_target=remote_plan.expected_remote_target,
                    )
                    remote_action = CleanupAction(
                        kind=remote_plan.action.kind,
                        message=remote_plan.action.message,
                        status="applied",
                    )
                record_action(remote_action)

        comment_plan_tasks = _create_stack_comment_cleanup_tasks(
            github_client=github_client,
            github_repository=github_repository,
            prepared_changes=tuple(prepared_changes),
        )
        try:
            for prepared_change in prepared_changes:
                change_id = prepared_change.change_id
                if not prepared_change.inspect_stack_comment:
                    continue

                comment_plan = await comment_plan_tasks[change_id]
                if comment_plan is None:
                    continue
                comment_action = comment_plan.action
                if (
                    apply
                    and comment_action.status == "planned"
                    and comment_plan.comment_id is not None
                ):
                    await _delete_issue_comment(
                        comment_id=comment_plan.comment_id,
                        github_client=github_client,
                        github_repository=github_repository,
                    )
                    if change_id in next_changes:
                        next_changes[change_id] = next_changes[change_id].model_copy(
                            update={"stack_comment_id": None}
                        )
                    comment_action = CleanupAction(
                        kind=comment_plan.action.kind,
                        message=comment_plan.action.message,
                        status="applied",
                    )
                record_action(comment_action)
        finally:
            for task in comment_plan_tasks.values():
                if not task.done():
                    task.cancel()
            if comment_plan_tasks:
                await asyncio.gather(*comment_plan_tasks.values(), return_exceptions=True)

    if apply and next_changes != prepared_cleanup.state.changes:
        prepared_cleanup.state_store.save(
            prepared_cleanup.state.model_copy(update={"changes": next_changes})
        )

    return CleanupResult(
        actions=tuple(actions),
        applied=apply,
        github_error=prepared_cleanup.github_repository_error,
        github_repository=github_repository.full_name,
        remote=remote,
        remote_error=prepared_cleanup.remote_error,
    )


def _prepare_cleanup_change(
    *,
    cached_change: CachedChange,
    change_id: str,
    prepared_cleanup: PreparedCleanup,
) -> PreparedCleanupChange:
    bookmark_state = prepared_cleanup.bookmark_states.get(
        cached_change.bookmark or "",
        BookmarkState(name=cached_change.bookmark or ""),
    )
    stale_reason = _stale_change_reason(
        change_id=change_id,
        jj_client=prepared_cleanup.jj_client,
    )
    return PreparedCleanupChange(
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


def _create_stack_comment_cleanup_tasks(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    prepared_changes: tuple[PreparedCleanupChange, ...],
) -> dict[str, asyncio.Task[StackCommentCleanupPlan | None]]:
    semaphore = asyncio.Semaphore(_GITHUB_INSPECTION_CONCURRENCY)
    return {
        prepared_change.change_id: asyncio.create_task(
            _plan_stack_comment_cleanup_with_semaphore(
                cached_change=prepared_change.cached_change,
                github_client=github_client,
                github_repository=github_repository,
                semaphore=semaphore,
            )
        )
        for prepared_change in prepared_changes
        if prepared_change.inspect_stack_comment
    }


async def _plan_stack_comment_cleanup_with_semaphore(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    semaphore: asyncio.Semaphore,
) -> StackCommentCleanupPlan | None:
    async with semaphore:
        return await _plan_stack_comment_cleanup(
            cached_change=cached_change,
            github_client=github_client,
            github_repository=github_repository,
        )


def _resolve_remote(
    *,
    config: RepoConfig,
    jj_client: JjClient,
) -> tuple[GitRemote | None, str | None]:
    try:
        return select_submit_remote(config, jj_client.list_git_remotes()), None
    except CliError as error:
        return None, str(error)


def _resolve_github_repository(
    *,
    config: RepoConfig,
    remote: GitRemote | None,
):
    if remote is None:
        return None, None
    try:
        return resolve_github_repository(config, remote), None
    except CliError as error:
        return None, str(error)


def _load_bookmark_states(
    *,
    jj_client: JjClient,
    remote: GitRemote | None,
    state: ReviewState,
) -> dict[str, BookmarkState]:
    if remote is None:
        return {}
    bookmarks = sorted(
        {
            cached_change.bookmark
            for cached_change in state.changes.values()
            if cached_change.bookmark is not None
        }
    )
    if not bookmarks:
        return {}
    return jj_client.list_bookmark_states(bookmarks)


def _cache_action(
    *,
    change_id: str,
    reason: str,
    status: CleanupActionStatus,
) -> CleanupAction:
    return CleanupAction(
        kind="cache",
        message=f"remove cached review state for {change_id[:8]} ({reason})",
        status=status,
    )


def _stale_change_reason(
    *,
    change_id: str,
    jj_client: JjClient,
) -> str | None:
    revisions = jj_client.query_revisions(change_id, limit=2)
    if not revisions:
        return "no visible local change matches that cached change ID"
    if len(revisions) > 1:
        return "multiple visible revisions still share that change ID"

    revision = revisions[0]
    if not revision.is_reviewable():
        return "local change is no longer reviewable"

    try:
        jj_client.discover_review_stack(change_id)
    except UnsupportedStackError as error:
        if str(error).startswith("`trunk()`"):
            raise
        return "local change no longer participates in a supported review stack"
    return None


def _plan_remote_branch_cleanup(
    *,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    remote: GitRemote | None,
) -> RemoteBranchCleanupPlan | None:
    bookmark = cached_change.bookmark
    if remote is None or bookmark is None or not bookmark.startswith("review/"):
        return None

    remote_state = bookmark_state.remote_target(remote.name)
    if remote_state is None or not remote_state.targets:
        return None

    branch_label = f"{bookmark}@{remote.name}"
    if bookmark_state.local_targets:
        return RemoteBranchCleanupPlan(
            action=CleanupAction(
                kind="remote branch",
                message=(
                    f"cannot delete remote review branch {branch_label} while the "
                    f"local bookmark {bookmark!r} still exists"
                ),
                status="blocked",
            ),
        )
    if len(remote_state.targets) > 1:
        return RemoteBranchCleanupPlan(
            action=CleanupAction(
                kind="remote branch",
                message=(
                    f"cannot delete remote review branch {branch_label} because the "
                    "remote bookmark is conflicted"
                ),
                status="blocked",
            ),
        )

    return RemoteBranchCleanupPlan(
        action=CleanupAction(
            kind="remote branch",
            message=f"delete remote review branch {branch_label}",
            status="planned",
        ),
        expected_remote_target=remote_state.target,
    )


def _should_inspect_stack_comment_cleanup(
    *,
    bookmark_state: BookmarkState,
    cached_change: CachedChange,
    remote: GitRemote | None,
    stale_reason: str | None,
) -> bool:
    if cached_change.pr_number is None:
        return False
    if stale_reason is None:
        return True
    if cached_change.stack_comment_id is not None:
        return True
    if cached_change.pr_state in {"closed", "merged"}:
        return True
    if remote is None:
        return False

    remote_state = bookmark_state.remote_target(remote.name)
    return remote_state is None or not remote_state.targets


async def _plan_stack_comment_cleanup(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
) -> StackCommentCleanupPlan | None:
    if cached_change.pr_number is None:
        return None

    pull_request = await _load_pull_request(
        cached_change=cached_change,
        github_client=github_client,
        github_repository=github_repository,
    )
    if pull_request is None:
        return None

    if not _pull_request_is_closed_or_detached(
        bookmark=cached_change.bookmark,
        github_repository=github_repository,
        pull_request=pull_request,
    ):
        return None

    managed_comment = await _resolve_managed_stack_comment(
        cached_change=cached_change,
        github_client=github_client,
        github_repository=github_repository,
    )
    if isinstance(managed_comment, CleanupAction):
        return StackCommentCleanupPlan(action=managed_comment)
    if managed_comment is None:
        return None

    return StackCommentCleanupPlan(
        action=CleanupAction(
            kind="stack comment",
            message=(
                "delete managed stack comment "
                f"#{managed_comment.id} from PR #{cached_change.pr_number}"
            ),
            status="planned",
        ),
        comment_id=managed_comment.id,
    )


async def _load_pull_request(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
) -> GithubPullRequest | None:
    try:
        return await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=cached_change.pr_number or 0,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return None
        raise CleanupError(
            f"Could not load pull request #{cached_change.pr_number}: {error}"
        ) from error


def _pull_request_is_closed_or_detached(
    *,
    bookmark: str | None,
    github_repository,
    pull_request: GithubPullRequest,
) -> bool:
    if pull_request.state == "closed":
        return True
    if bookmark is None:
        return False
    expected_label = f"{github_repository.owner}:{bookmark}"
    return (
        pull_request.head.ref != bookmark or pull_request.head.label != expected_label
    )


async def _resolve_managed_stack_comment(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
) -> GithubIssueComment | CleanupAction | None:
    comments = await _list_issue_comments(
        github_client=github_client,
        github_repository=github_repository,
        pull_request_number=cached_change.pr_number or 0,
    )
    if cached_change.stack_comment_id is not None:
        cached_comment = next(
            (
                comment
                for comment in comments
                if comment.id == cached_change.stack_comment_id
            ),
            None,
        )
        if cached_comment is not None:
            if _STACK_COMMENT_MARKER not in cached_comment.body:
                return CleanupAction(
                    kind="stack comment",
                    message=(
                        "cannot delete cached stack comment "
                        f"#{cached_comment.id} because it is not managed by "
                        "`jj-review`"
                    ),
                    status="blocked",
                )
            return cached_comment

    managed_comments = [
        comment for comment in comments if _STACK_COMMENT_MARKER in comment.body
    ]
    if len(managed_comments) > 1:
        return CleanupAction(
            kind="stack comment",
            message=(
                "cannot delete managed stack comments because GitHub reports "
                f"multiple candidates on PR #{cached_change.pr_number}"
            ),
            status="blocked",
        )
    if not managed_comments:
        return None
    return managed_comments[0]


async def _list_issue_comments(
    *,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> tuple[GithubIssueComment, ...]:
    try:
        return await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CleanupError(
            f"Could not list stack comments for pull request #{pull_request_number}: "
            f"{error}"
        ) from error


async def _delete_issue_comment(
    *,
    comment_id: int,
    github_client: GithubClient,
    github_repository,
) -> None:
    try:
        await github_client.delete_issue_comment(
            github_repository.owner,
            github_repository.repo,
            comment_id=comment_id,
        )
    except GithubClientError as error:
        raise CleanupError(f"Could not delete stack comment #{comment_id}: {error}") from error
