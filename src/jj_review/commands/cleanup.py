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
    PullRequestLookup,
    ReviewStatusRevision,
    prepare_status,
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
    requires_nontrunk_rebase: bool
    selected_revset: str


@dataclass(frozen=True, slots=True)
class PreparedRestack:
    """Locally prepared restack inputs before any rewrite."""

    apply: bool
    allow_nontrunk_rebase: bool
    prepared_status: PreparedStatus


@dataclass(frozen=True, slots=True)
class _RestackInspection:
    github_error: str | None
    github_repository: str | None
    remote: GitRemote | None
    remote_error: str | None
    revisions: tuple[ReviewStatusRevision, ...]
    selected_revset: str


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
    allow_nontrunk_rebase: bool,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> PreparedRestack:
    """Resolve local restack inputs before any rewrite."""

    return PreparedRestack(
        apply=apply,
        allow_nontrunk_rebase=allow_nontrunk_rebase,
        prepared_status=prepare_status(
            change_overrides=change_overrides,
            config=config,
            fetch_remote_state=False,
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

    inspection = asyncio.run(_inspect_restack(prepared_restack=prepared_restack))
    return _build_restack_result(
        inspection=inspection,
        on_action=on_action,
        prepared_restack=prepared_restack,
    )


async def _inspect_restack(
    *,
    prepared_restack: PreparedRestack,
) -> _RestackInspection:
    prepared_status = prepared_restack.prepared_status
    prepared = prepared_status.prepared
    selected_revset = prepared_status.selected_revset
    github_repository = prepared_status.github_repository
    github_repository_error = prepared_status.github_repository_error

    if prepared.remote is None:
        return _RestackInspection(
            github_error=None,
            github_repository=None,
            remote=None,
            remote_error=prepared.remote_error,
            revisions=(),
            selected_revset=selected_revset,
        )
    if github_repository is None:
        return _RestackInspection(
            github_error=github_repository_error,
            github_repository=None,
            remote=prepared.remote,
            remote_error=None,
            revisions=(),
            selected_revset=selected_revset,
        )
    if not prepared.status_revisions:
        return _RestackInspection(
            github_error=None,
            github_repository=github_repository.full_name,
            remote=prepared.remote,
            remote_error=None,
            revisions=(),
            selected_revset=selected_revset,
        )

    async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
        try:
            revisions = await _inspect_restack_revisions(
                github_client=github_client,
                github_repository=github_repository,
                prepared_status=prepared_status,
            )
        except GithubClientError as error:
            return _RestackInspection(
                github_error=_summarize_restack_batch_error(error),
                github_repository=github_repository.full_name,
                remote=prepared.remote,
                remote_error=None,
                revisions=(),
                selected_revset=selected_revset,
            )

    github_error = next(
        (
            lookup.repository_error
            for revision in revisions
            if revision.pull_request_lookup is not None
            and (lookup := revision.pull_request_lookup).repository_error is not None
        ),
        None,
    )
    return _RestackInspection(
        github_error=github_error,
        github_repository=github_repository.full_name,
        remote=prepared.remote,
        remote_error=None,
        revisions=tuple(revisions),
        selected_revset=selected_revset,
    )


async def _inspect_restack_revisions(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    prepared_status: PreparedStatus,
) -> list[ReviewStatusRevision]:
    prepared_revisions = prepared_status.prepared.status_revisions
    cached_pull_requests = await _load_cached_pull_requests_for_restack(
        github_client=github_client,
        github_repository=github_repository,
        prepared_revisions=prepared_revisions,
    )
    pull_requests_by_head_ref = await _load_pull_requests_by_head_refs_for_restack(
        cached_pull_requests=cached_pull_requests,
        github_client=github_client,
        github_repository=github_repository,
        prepared_revisions=prepared_revisions,
    )
    semaphore = asyncio.Semaphore(_GITHUB_INSPECTION_CONCURRENCY)
    tasks = [
        asyncio.create_task(
            _inspect_restack_revision(
                cached_pull_requests=cached_pull_requests,
                github_client=github_client,
                github_repository=github_repository,
                prepared_revision=prepared_revision,
                pull_requests_by_head_ref=pull_requests_by_head_ref,
                semaphore=semaphore,
            )
        )
        for prepared_revision in prepared_revisions
    ]
    return list(await asyncio.gather(*tasks))


async def _load_cached_pull_requests_for_restack(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    prepared_revisions,
) -> dict[int, GithubPullRequest | None]:
    pull_numbers = sorted(
        {
            prepared_revision.cached_change.pr_number
            for prepared_revision in prepared_revisions
            if prepared_revision.cached_change is not None
            and prepared_revision.cached_change.pr_number is not None
        }
    )
    if not pull_numbers:
        return {}
    try:
        return await github_client.get_pull_requests_by_numbers(
            github_repository.owner,
            github_repository.repo,
            pull_numbers=pull_numbers,
        )
    except GithubClientError as error:
        if error.status_code not in {404, 405, 501}:
            raise
        return {}


async def _load_pull_requests_by_head_refs_for_restack(
    *,
    cached_pull_requests: dict[int, GithubPullRequest | None],
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    prepared_revisions,
) -> dict[str, tuple[GithubPullRequest, ...]]:
    head_refs = sorted(
        {
            prepared_revision.bookmark
            for prepared_revision in prepared_revisions
            if not _cached_pull_request_matches_revision(
                prepared_revision=prepared_revision,
                cached_pull_requests=cached_pull_requests,
            )
        }
    )
    if not head_refs:
        return {}
    try:
        return await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=head_refs,
        )
    except GithubClientError as error:
        if error.status_code not in {404, 405, 501}:
            raise
        return {}


def _cached_pull_request_matches_revision(
    *,
    prepared_revision,
    cached_pull_requests: dict[int, GithubPullRequest | None],
) -> bool:
    cached_change = prepared_revision.cached_change
    if cached_change is None or cached_change.pr_number is None:
        return False
    cached_pull_request = cached_pull_requests.get(cached_change.pr_number)
    return (
        cached_pull_request is not None
        and cached_pull_request.head.ref == prepared_revision.bookmark
    )


async def _inspect_restack_revision(
    *,
    cached_pull_requests: dict[int, GithubPullRequest | None],
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    prepared_revision,
    pull_requests_by_head_ref: dict[str, tuple[GithubPullRequest, ...]],
    semaphore: asyncio.Semaphore,
) -> ReviewStatusRevision:
    async with semaphore:
        pull_request_lookup = await _inspect_restack_pull_request(
            bookmark=prepared_revision.bookmark,
            cached_change=prepared_revision.cached_change,
            cached_pull_requests=cached_pull_requests,
            github_client=github_client,
            github_repository=github_repository,
            pull_requests_by_head_ref=pull_requests_by_head_ref,
        )
        return ReviewStatusRevision(
            bookmark=prepared_revision.bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            cached_change=prepared_revision.cached_change,
            change_id=prepared_revision.revision.change_id,
            local_divergent=getattr(prepared_revision.revision, "divergent", False),
            pull_request_lookup=pull_request_lookup,
            remote_state=None,
            stack_comment_lookup=None,
            subject=prepared_revision.revision.subject,
        )


async def _inspect_restack_pull_request(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    cached_pull_requests: dict[int, GithubPullRequest | None],
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_requests_by_head_ref: dict[str, tuple[GithubPullRequest, ...]],
) -> PullRequestLookup:
    cached_pr_number = cached_change.pr_number if cached_change is not None else None
    if cached_pr_number is not None:
        cached_pull_request = cached_pull_requests.get(cached_pr_number)
        if cached_pull_request is not None and cached_pull_request.head.ref == bookmark:
            return _restack_lookup_from_pull_request(
                _normalize_restack_pull_request(cached_pull_request)
            )

    pull_requests = pull_requests_by_head_ref.get(bookmark)
    if pull_requests is not None:
        return _restack_lookup_from_head_pull_requests(
            bookmark=bookmark,
            pull_requests=pull_requests,
        )

    return await _inspect_restack_pull_request_by_head(
        bookmark=bookmark,
        github_client=github_client,
        github_repository=github_repository,
    )


async def _inspect_restack_pull_request_by_head(
    *,
    bookmark: str,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
) -> PullRequestLookup:
    head_label = f"{github_repository.owner}:{bookmark}"
    try:
        pull_requests = await github_client.list_pull_requests(
            github_repository.owner,
            github_repository.repo,
            head=head_label,
        )
    except GithubClientError as error:
        return _restack_lookup_from_error(action="pull request lookup", error=error)

    if not pull_requests:
        return PullRequestLookup(
            message=None,
            pull_request=None,
            repository_error=None,
            state="missing",
        )
    if len(pull_requests) > 1:
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        return PullRequestLookup(
            message=(
                f"GitHub reports multiple pull requests for head branch {head_label!r}: "
                f"{numbers}."
            ),
            pull_request=None,
            repository_error=None,
            state="ambiguous",
        )
    return _restack_lookup_from_pull_request(_normalize_restack_pull_request(pull_requests[0]))


def _restack_lookup_from_pull_request(pull_request: GithubPullRequest) -> PullRequestLookup:
    if pull_request.state != "open":
        return PullRequestLookup(
            message=(
                f"GitHub reports pull request #{pull_request.number} for head branch "
                f"{pull_request.head.ref!r} in state {pull_request.state!r}."
            ),
            pull_request=pull_request,
            review_decision=None,
            repository_error=None,
            state="closed",
        )
    return PullRequestLookup(
        message=None,
        pull_request=pull_request,
        review_decision=None,
        review_decision_error=None,
        repository_error=None,
        state="open",
    )


def _restack_lookup_from_head_pull_requests(
    *,
    bookmark: str,
    pull_requests: tuple[GithubPullRequest, ...],
) -> PullRequestLookup:
    head_label = bookmark
    if not pull_requests:
        return PullRequestLookup(
            message=None,
            pull_request=None,
            repository_error=None,
            state="missing",
        )
    if len(pull_requests) > 1:
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        return PullRequestLookup(
            message=(
                f"GitHub reports multiple pull requests for head branch {head_label!r}: "
                f"{numbers}."
            ),
            pull_request=None,
            repository_error=None,
            state="ambiguous",
        )
    return _restack_lookup_from_pull_request(_normalize_restack_pull_request(pull_requests[0]))


def _restack_lookup_from_error(*, action: str, error: GithubClientError) -> PullRequestLookup:
    return PullRequestLookup(
        message=_summarize_restack_lookup_error(action=action, error=error),
        pull_request=None,
        repository_error=(
            _summarize_restack_repository_error(error)
            if _is_repository_level_restack_error(error)
            else None
        ),
        state="error",
    )


def _normalize_restack_pull_request(pull_request: GithubPullRequest) -> GithubPullRequest:
    if pull_request.state != "closed" or pull_request.merged_at is None:
        return pull_request
    return pull_request.model_copy(update={"state": "merged"})


def _summarize_restack_lookup_error(*, action: str, error: GithubClientError) -> str:
    if error.status_code is None:
        return "GitHub is unavailable - check network connectivity"
    if error.status_code == 401:
        return "GitHub authentication failed - check GITHUB_TOKEN"
    if error.status_code == 403:
        return "GitHub access was denied - check GITHUB_TOKEN and repo access"
    if error.status_code >= 500:
        return "GitHub is unavailable - check network connectivity"
    return f"{action} failed (GitHub {error.status_code})"


def _summarize_restack_repository_error(error: GithubClientError) -> str:
    if error.status_code is None:
        return "unavailable - check network connectivity"
    if error.status_code == 401:
        return "auth failed - check GITHUB_TOKEN"
    if error.status_code == 403:
        return "access denied - check GITHUB_TOKEN and repo access"
    if error.status_code == 404:
        return "repo not found or inaccessible - check GITHUB_TOKEN and repo access"
    if error.status_code >= 500:
        return "unavailable - check network connectivity"
    return f"unavailable (GitHub {error.status_code})"


def _summarize_restack_batch_error(error: GithubClientError) -> str:
    if _is_repository_level_restack_error(error):
        return _summarize_restack_repository_error(error)
    return _summarize_restack_lookup_error(action="pull request lookup", error=error)


def _is_repository_level_restack_error(error: GithubClientError) -> bool:
    return (
        error.status_code in {401, 403, 404}
        or error.status_code is None
        or (error.status_code is not None and error.status_code >= 500)
    )


def _build_restack_result(
    *,
    inspection: _RestackInspection,
    on_action: Callable[[CleanupAction], None] | None,
    prepared_restack: PreparedRestack,
) -> RestackResult:
    prepared_status = prepared_restack.prepared_status
    prepared = prepared_status.prepared
    revisions_by_change_id = {revision.change_id: revision for revision in inspection.revisions}
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

    if inspection.github_error is not None or inspection.github_repository is None:
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
            github_error=inspection.github_error,
            github_repository=inspection.github_repository,
            remote=inspection.remote,
            remote_error=inspection.remote_error,
            requires_nontrunk_rebase=False,
            selected_revset=inspection.selected_revset,
        )

    hard_blocked = False
    requires_nontrunk_rebase = False
    merged_revisions = tuple(
        revision for revision in path_revisions if _revision_has_merged_pull_request(revision)
    )
    if not merged_revisions:
        return RestackResult(
            actions=(),
            applied=prepared_restack.apply,
            blocked=False,
            github_error=inspection.github_error,
            github_repository=inspection.github_repository,
            remote=inspection.remote,
            remote_error=inspection.remote_error,
            requires_nontrunk_rebase=False,
            selected_revset=inspection.selected_revset,
        )

    closed_unmerged_revisions = tuple(
        revision for revision in path_revisions if _revision_is_closed_unmerged(revision)
    )
    for revision in closed_unmerged_revisions:
        hard_blocked = True
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

    current_commit_id_by_change_id = {
        pr.revision.change_id: pr.revision.commit_id for pr in prepared.status_revisions
    }
    for revision in merged_revisions:
        cached_change = revision.cached_change
        if cached_change is None or cached_change.last_submitted_commit_id is None:
            continue
        # merged_revisions is a subset of path_revisions, which is derived from
        # prepared.status_revisions, so every change_id is guaranteed to be present.
        current_commit_id = current_commit_id_by_change_id[revision.change_id]
        last_submitted = cached_change.last_submitted_commit_id
        if current_commit_id == last_submitted:
            continue
        hard_blocked = True
        record_action(
            CleanupAction(
                kind="restack",
                message=(
                    f"cannot restack past {_revision_label(revision)} because it has "
                    "local edits since last submit; push a new version first or rebase "
                    "manually"
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
            hard_blocked = True
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
    if prepared_restack.apply and not hard_blocked:
        trunk_rebase_plans = tuple(
            (source_change_id, destination_change_id)
            for source_change_id, destination_change_id in rebase_plans
            if destination_change_id is None
        )
        for source_change_id, destination_change_id in trunk_rebase_plans:
            source_revision = client.resolve_revision(source_change_id)
            destination_revision = prepared.stack.trunk.commit_id
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

        remaining_nontrunk_rebase_plans = _remaining_nontrunk_rebase_plans(
            client=client,
            rebase_plans=rebase_plans,
        )
        if remaining_nontrunk_rebase_plans and not prepared_restack.allow_nontrunk_rebase:
            requires_nontrunk_rebase = True
            for source_change_id, destination_change_id in remaining_nontrunk_rebase_plans:
                record_action(
                    CleanupAction(
                        kind="restack",
                        message=_blocked_nontrunk_restack_message(
                            source_change_id=source_change_id,
                            destination_change_id=destination_change_id,
                        ),
                        status="blocked",
                    )
                )
        else:
            for source_change_id, destination_change_id in remaining_nontrunk_rebase_plans:
                destination_revision = client.resolve_revision(destination_change_id).commit_id
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
            message = _planned_restack_message(
                source_change_id=source_change_id,
                destination_change_id=destination_change_id,
            )
            status: CleanupActionStatus = "planned"
            if hard_blocked:
                status = "blocked"
                message = f"{message} once blocked path changes are resolved"
            elif (
                destination_change_id is not None
                and not prepared_restack.allow_nontrunk_rebase
            ):
                status = "blocked"
                requires_nontrunk_rebase = True
                message = _blocked_nontrunk_restack_message(
                    source_change_id=source_change_id,
                    destination_change_id=destination_change_id,
                )
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
        blocked=hard_blocked or requires_nontrunk_rebase,
        github_error=inspection.github_error,
        github_repository=inspection.github_repository,
        remote=inspection.remote,
        remote_error=inspection.remote_error,
        requires_nontrunk_rebase=requires_nontrunk_rebase,
        selected_revset=inspection.selected_revset,
    )


def _remaining_nontrunk_rebase_plans(
    *,
    client: JjClient,
    rebase_plans: list[tuple[str, str | None]],
) -> tuple[tuple[str, str], ...]:
    remaining_plans: list[tuple[str, str]] = []
    for source_change_id, destination_change_id in rebase_plans:
        if destination_change_id is None:
            continue
        source_revision = client.resolve_revision(source_change_id)
        destination_revision = client.resolve_revision(destination_change_id).commit_id
        if source_revision.only_parent_commit_id() == destination_revision:
            continue
        remaining_plans.append((source_change_id, destination_change_id))
    return tuple(remaining_plans)


def _planned_restack_message(
    *,
    source_change_id: str,
    destination_change_id: str | None,
) -> str:
    return (
        f"rebase {_short_change_id(source_change_id)} onto "
        f"{_restack_destination_label(destination_change_id)}"
    )


def _blocked_nontrunk_restack_message(
    *,
    source_change_id: str,
    destination_change_id: str,
) -> str:
    return (
        f"rebase {_short_change_id(source_change_id)} onto "
        f"{_restack_destination_label(destination_change_id)} requires "
        "--allow-nontrunk-rebase"
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
    return pull_request.head.ref != bookmark or pull_request.head.label != expected_label


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
            (comment for comment in comments if comment.id == cached_change.stack_comment_id),
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

    managed_comments = [comment for comment in comments if _STACK_COMMENT_MARKER in comment.body]
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
            f"Could not list stack comments for pull request #{pull_request_number}: {error}"
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
