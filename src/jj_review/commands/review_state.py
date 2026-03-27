"""Shared review-state inspection for `status`."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review.bookmarks import BookmarkResolver, BookmarkSource
from jj_review.cache import ReviewStateStore
from jj_review.commands.submit import (
    _STACK_COMMENT_MARKER,
    ResolvedGithubRepository,
    _build_github_client,
    _discover_bookmarks_for_revisions,
    _ensure_unique_bookmarks,
    _github_token_from_env,
    resolve_github_repository,
    select_submit_remote,
)
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.intent import intent_is_stale, scan_intents
from jj_review.jj import JjClient
from jj_review.jj.client import RevsetResolutionError
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, LinkState, ReviewState
from jj_review.models.github import GithubIssueComment, GithubPullRequest
from jj_review.models.intent import LoadedIntent
from jj_review.models.stack import LocalRevision, LocalStack

logger = logging.getLogger(__name__)
_GITHUB_INSPECTION_CONCURRENCY = 4

PullRequestLookupState = Literal["ambiguous", "closed", "error", "missing", "open"]
StackCommentLookupState = Literal["ambiguous", "error", "missing", "present"]


@dataclass(frozen=True, slots=True)
class PullRequestLookup:
    """Best-effort GitHub pull request lookup for one review branch."""

    message: str | None
    pull_request: GithubPullRequest | None
    state: PullRequestLookupState
    review_decision: str | None = None
    review_decision_error: str | None = None
    repository_error: str | None = None


@dataclass(frozen=True, slots=True)
class StackCommentLookup:
    """Best-effort GitHub stack comment lookup for one pull request."""

    comment: GithubIssueComment | None
    message: str | None
    state: StackCommentLookupState


@dataclass(frozen=True, slots=True)
class ReviewStatusRevision:
    """Rendered review linkage state for one local revision."""

    bookmark: str
    bookmark_source: BookmarkSource
    cached_change: CachedChange | None
    change_id: str
    link_state: LinkState
    local_divergent: bool
    pull_request_lookup: PullRequestLookup | None
    remote_state: RemoteBookmarkState | None
    stack_comment_lookup: StackCommentLookup | None
    subject: str


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Status result for one selected local stack."""

    github_error: str | None
    github_repository: str | None
    incomplete: bool
    remote: GitRemote | None
    remote_error: str | None
    revisions: tuple[ReviewStatusRevision, ...]
    selected_revset: str
    trunk_subject: str


@dataclass(frozen=True, slots=True)
class PreparedStatus:
    """Locally prepared status inputs before any GitHub inspection."""

    github_repository: ResolvedGithubRepository | None
    github_repository_error: str | None
    outstanding_intents: tuple[LoadedIntent, ...]
    prepared: _PreparedStack
    selected_revset: str
    stale_intents: tuple[LoadedIntent, ...]
    trunk_subject: str


@dataclass(frozen=True, slots=True)
class _PreparedStack:
    bookmark_result_changed: bool
    client: JjClient
    remote: GitRemote | None
    remote_error: str | None
    stack: LocalStack
    state: ReviewState
    state_changes: dict[str, CachedChange]
    state_store: ReviewStateStore
    status_revisions: tuple[_PreparedRevision, ...]


@dataclass(frozen=True, slots=True)
class _PreparedRevision:
    """Local review revision with resolved bookmark and cached state."""

    bookmark: str
    bookmark_source: BookmarkSource
    cached_change: CachedChange | None
    revision: LocalRevision

def prepare_status(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    fetch_remote_state: bool = False,
    persist_bookmarks: bool = True,
    repo_root: Path,
    revset: str | None,
) -> PreparedStatus:
    """Resolve local status inputs before any GitHub network inspection."""

    prepared = _prepare_stack(
        change_overrides=change_overrides,
        config=config,
        persist_bookmarks=persist_bookmarks,
        refresh_remote_state=fetch_remote_state,
        repo_root=repo_root,
        require_remote=False,
        revset=revset,
    )
    logger.debug(
        "status prepared: selected_revset=%s revisions=%d remote=%s",
        prepared.stack.selected_revset,
        len(prepared.status_revisions),
        prepared.remote.name if prepared.remote is not None else "unavailable",
    )
    github_repository, github_repository_error = _resolve_status_github_repository(
        config=config,
        remote=prepared.remote,
    )
    outstanding_intents, stale_intents = _classify_status_intents(prepared)

    return PreparedStatus(
        github_repository=github_repository,
        github_repository_error=github_repository_error,
        outstanding_intents=outstanding_intents,
        prepared=prepared,
        selected_revset=prepared.stack.selected_revset,
        stale_intents=stale_intents,
        trunk_subject=prepared.stack.trunk.subject,
    )


def _resolve_status_github_repository(
    *,
    config: RepoConfig,
    remote: GitRemote | None,
) -> tuple[ResolvedGithubRepository | None, str | None]:
    if remote is None:
        return None, None
    try:
        return resolve_github_repository(config, remote), None
    except CliError as error:
        return None, str(error)


def _classify_status_intents(
    prepared: _PreparedStack,
) -> tuple[tuple[LoadedIntent, ...], tuple[LoadedIntent, ...]]:
    state_dir = prepared.state_store.state_dir
    if state_dir is None:
        return (), ()

    outstanding_intents: list[LoadedIntent] = []
    stale_intents: list[LoadedIntent] = []
    now = datetime.now(UTC)

    def change_id_resolves(change_id: str) -> bool:
        return _change_id_resolves(prepared.client, change_id)

    for loaded in scan_intents(state_dir):
        if intent_is_stale(
            loaded.intent,
            change_id_resolves,
            now=now,
        ):
            stale_intents.append(loaded)
        else:
            outstanding_intents.append(loaded)
    return tuple(outstanding_intents), tuple(stale_intents)


def stream_status(
    *,
    persist_cache_updates: bool = True,
    prepared_status: PreparedStatus,
    on_github_status: Callable[[str | None, str | None], None] | None = None,
    on_revision: Callable[[ReviewStatusRevision, bool], None] | None = None,
) -> StatusResult:
    """Inspect GitHub state for a prepared stack and optionally stream results out."""

    return asyncio.run(
        _stream_status_async(
            on_github_status=on_github_status,
            on_revision=on_revision,
            persist_cache_updates=persist_cache_updates,
            prepared_status=prepared_status,
        )
    )


async def _stream_status_async(
    *,
    on_github_status: Callable[[str | None, str | None], None] | None,
    on_revision: Callable[[ReviewStatusRevision, bool], None] | None,
    persist_cache_updates: bool = True,
    prepared_status: PreparedStatus,
) -> StatusResult:
    prepared = prepared_status.prepared
    selected_revset = prepared_status.selected_revset
    trunk_subject = prepared_status.trunk_subject
    github_repository = prepared_status.github_repository
    github_repository_error = prepared_status.github_repository_error

    if prepared.remote is None:
        display_revisions = _status_revisions_in_display_order(
            _build_status_revisions_without_github(prepared)
        )
        if on_github_status is not None:
            on_github_status(None, None)
        for revision in display_revisions:
            if on_revision is not None:
                on_revision(revision, False)
        return StatusResult(
            github_error=None,
            github_repository=None,
            incomplete=True,
            remote=None,
            remote_error=prepared.remote_error,
            revisions=display_revisions,
            selected_revset=selected_revset,
            trunk_subject=trunk_subject,
        )

    if github_repository is None:
        logger.debug("status github target unavailable: %s", github_repository_error)
        display_revisions = _status_revisions_in_display_order(
            _build_status_revisions_without_github(prepared)
        )
        if on_github_status is not None:
            on_github_status(None, github_repository_error)
        for revision in display_revisions:
            if on_revision is not None:
                on_revision(revision, False)
        return StatusResult(
            github_error=github_repository_error,
            github_repository=None,
            incomplete=True,
            remote=prepared.remote,
            remote_error=None,
            revisions=display_revisions,
            selected_revset=selected_revset,
            trunk_subject=trunk_subject,
        )

    github_status_reported = False

    def emit_github_status(github_error: str | None) -> None:
        nonlocal github_status_reported
        if github_status_reported:
            return
        github_status_reported = True
        if on_github_status is not None:
            on_github_status(github_repository.full_name, github_error)

    if not prepared.status_revisions:
        if on_github_status is not None:
            on_github_status(
                github_repository.full_name,
                "not inspected; no reviewable commits",
            )
        return StatusResult(
            github_error=None,
            github_repository=github_repository.full_name,
            incomplete=False,
            remote=prepared.remote,
            remote_error=None,
            revisions=(),
            selected_revset=selected_revset,
            trunk_subject=trunk_subject,
        )

    revisions: list[ReviewStatusRevision] = []
    try:
        async for revision in _iter_status_revisions_with_github(
            github_repository=github_repository,
            on_github_status=emit_github_status,
            prepared=prepared,
        ):
            revisions.append(revision)
            if on_revision is not None:
                on_revision(revision, True)
    except CliError as error:
        if not github_status_reported:
            emit_github_status(None)
        fallback_revisions = _status_revisions_in_display_order(
            _build_status_revisions_without_github(prepared)
        )
        github_error = str(error)
        logger.debug("status github inspection failed: %s", github_error)
        streamed_change_ids = {revision.change_id for revision in revisions}
        for revision in fallback_revisions:
            if on_revision is not None and revision.change_id not in streamed_change_ids:
                on_revision(revision, False)
        return StatusResult(
            github_error=github_error,
            github_repository=github_repository.full_name,
            incomplete=True,
            remote=prepared.remote,
            remote_error=None,
            revisions=fallback_revisions,
            selected_revset=selected_revset,
            trunk_subject=trunk_subject,
        )

    if not github_status_reported:
        emit_github_status(None)
    if persist_cache_updates:
        _persist_status_cache_updates(prepared=prepared, revisions=tuple(revisions))
    return StatusResult(
        github_error=None,
        github_repository=github_repository.full_name,
        incomplete=_status_is_incomplete(tuple(revisions)),
        remote=prepared.remote,
        remote_error=None,
        revisions=tuple(revisions),
        selected_revset=selected_revset,
        trunk_subject=trunk_subject,
    )


def _prepare_stack(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    persist_bookmarks: bool,
    refresh_remote_state: bool,
    repo_root: Path,
    require_remote: bool,
    revset: str | None,
) -> _PreparedStack:
    client = JjClient(repo_root)
    stack = client.discover_review_stack(
        revset,
        allow_divergent=True,
        allow_immutable=True,
        allow_trunk_ancestors=True,
    )
    state_store = ReviewStateStore.for_repo(repo_root)
    state = state_store.load()
    remotes = client.list_git_remotes()
    remote: GitRemote | None = None
    remote_error: str | None = None
    if require_remote or remotes:
        try:
            remote = select_submit_remote(config, remotes)
        except CliError as error:
            if require_remote:
                raise
            remote_error = str(error)
    logger.debug(
        "prepared stack: remotes=%s selected_remote=%s remote_error=%s",
        [remote.name for remote in remotes],
        remote.name if remote is not None else None,
        remote_error,
    )
    if remote is not None and refresh_remote_state:
        client.fetch_remote(remote=remote.name)

    discovered_bookmarks: dict[str, str] = {}
    if remote is not None:
        discovered_bookmarks = _discover_bookmarks_for_revisions(
            bookmark_states=client.list_bookmark_states(),
            remote_name=remote.name,
            revisions=stack.revisions,
        )

    bookmark_result = BookmarkResolver(
        state,
        change_overrides,
        discovered_bookmarks=discovered_bookmarks,
    ).pin_revisions(stack.revisions)
    _ensure_unique_bookmarks(bookmark_result.resolutions)
    if persist_bookmarks and bookmark_result.changed:
        state_store.save(bookmark_result.state)

    state_changes = dict(bookmark_result.state.changes if persist_bookmarks else state.changes)
    status_revisions = tuple(
        _PreparedRevision(
            bookmark=resolution.bookmark,
            bookmark_source=resolution.source,
            cached_change=(
                state_changes.get(revision.change_id)
                or state.changes.get(revision.change_id)
            ),
            revision=revision,
        )
        for resolution, revision in zip(
            bookmark_result.resolutions,
            stack.revisions,
            strict=True,
        )
    )
    return _PreparedStack(
        bookmark_result_changed=persist_bookmarks and bookmark_result.changed,
        client=client,
        remote=remote,
        remote_error=remote_error,
        stack=stack,
        state=state,
        state_changes=state_changes,
        state_store=state_store,
        status_revisions=status_revisions,
    )


def _build_status_revisions_without_github(
    prepared: _PreparedStack,
) -> tuple[ReviewStatusRevision, ...]:
    return tuple(
        ReviewStatusRevision(
            bookmark=revision.bookmark,
            bookmark_source=revision.bookmark_source,
            cached_change=revision.cached_change,
            change_id=revision.revision.change_id,
            link_state=(
                revision.cached_change.link_state
                if revision.cached_change is not None
                else "active"
            ),
            local_divergent=getattr(revision.revision, "divergent", False),
            pull_request_lookup=None,
            remote_state=(
                prepared.client.get_bookmark_state(revision.bookmark).remote_target(
                    prepared.remote.name
                )
                if prepared.remote is not None
                else None
            ),
            stack_comment_lookup=None,
            subject=revision.revision.subject,
        )
        for revision in prepared.status_revisions
    )


def _status_revisions_in_display_order(
    revisions: tuple[ReviewStatusRevision, ...],
) -> tuple[ReviewStatusRevision, ...]:
    return tuple(reversed(revisions))


def _status_is_incomplete(revisions: tuple[ReviewStatusRevision, ...]) -> bool:
    for revision in revisions:
        if revision.local_divergent and not _revision_has_merged_pull_request(revision):
            return True
        pull_request_lookup = revision.pull_request_lookup
        if pull_request_lookup is not None and (
            pull_request_lookup.state == "ambiguous"
            or pull_request_lookup.state == "error"
            or (
                pull_request_lookup.state == "missing"
                and revision.cached_change is not None
                and (
                    revision.cached_change.pr_number is not None
                    or revision.cached_change.pr_url is not None
                )
            )
            or pull_request_lookup.review_decision_error is not None
        ):
            return True
        stack_comment_lookup = revision.stack_comment_lookup
        if stack_comment_lookup is not None and stack_comment_lookup.state in {
            "ambiguous",
            "error",
        }:
            return True
    return False


def _revision_has_merged_pull_request(revision: ReviewStatusRevision) -> bool:
    lookup = revision.pull_request_lookup
    return (
        lookup is not None
        and lookup.state == "closed"
        and lookup.pull_request is not None
        and lookup.pull_request.state == "merged"
    )


def _resolved_review_decision(
    *,
    cached_change: CachedChange | None,
    pull_request_lookup: PullRequestLookup,
) -> str | None:
    if pull_request_lookup.review_decision_error is None:
        return pull_request_lookup.review_decision
    if cached_change is None:
        return None
    return cached_change.pr_review_decision


def _persist_status_cache_updates(
    *,
    prepared: _PreparedStack,
    revisions: tuple[ReviewStatusRevision, ...],
) -> None:
    state_changes = dict(prepared.state_changes)
    for revision in revisions:
        cached_change = (
            state_changes.get(revision.change_id)
            or prepared.state.changes.get(revision.change_id)
        )
        updated_change = cached_change or CachedChange(bookmark=revision.bookmark)
        if cached_change is not None and cached_change.is_detached:
            if updated_change != cached_change:
                state_changes[revision.change_id] = updated_change
            continue
        pull_request_lookup = revision.pull_request_lookup
        if pull_request_lookup is not None:
            if pull_request_lookup.state == "missing":
                updated_change = updated_change.model_copy(
                    update={
                        "bookmark": revision.bookmark,
                        "pr_is_draft": None,
                        "pr_number": None,
                        "pr_review_decision": None,
                        "pr_state": None,
                        "pr_url": None,
                        "stack_comment_id": None,
                    }
                )
            elif pull_request_lookup.pull_request is not None:
                pull_request = pull_request_lookup.pull_request
                updated_change = updated_change.model_copy(
                    update={
                        "bookmark": revision.bookmark,
                        "pr_is_draft": pull_request.is_draft,
                        "pr_number": pull_request.number,
                        "pr_review_decision": _resolved_review_decision(
                            cached_change=cached_change,
                            pull_request_lookup=pull_request_lookup,
                        ),
                        "pr_state": pull_request.state,
                        "pr_url": pull_request.html_url,
                    }
                )
                if pull_request_lookup.state != "open":
                    updated_change = updated_change.model_copy(
                        update={"stack_comment_id": None}
                    )
        stack_comment_lookup = revision.stack_comment_lookup
        if stack_comment_lookup is not None:
            if stack_comment_lookup.state == "present":
                if stack_comment_lookup.comment is None:
                    raise AssertionError("Present stack comment lookup must include a comment.")
                updated_change = updated_change.model_copy(
                    update={"stack_comment_id": stack_comment_lookup.comment.id}
                )
            elif stack_comment_lookup.state == "missing":
                updated_change = updated_change.model_copy(update={"stack_comment_id": None})
        if updated_change != cached_change:
            state_changes[revision.change_id] = updated_change

    next_state = prepared.state.model_copy(update={"changes": state_changes})
    if next_state != prepared.state:
        prepared.state_store.save(next_state)


async def _iter_status_revisions_with_github(
    *,
    github_repository: ResolvedGithubRepository,
    on_github_status: Callable[[str | None], None] | None,
    prepared: _PreparedStack,
) -> AsyncIterator[ReviewStatusRevision]:
    bookmark_states = prepared.client.list_bookmark_states(
        [revision.bookmark for revision in prepared.status_revisions]
    )
    status_revisions = tuple(reversed(prepared.status_revisions))
    github_status_result: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

    def observe_github_status(github_error: str | None) -> None:
        if not github_status_result.done():
            github_status_result.set_result(github_error)

    async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
        semaphore = asyncio.Semaphore(_GITHUB_INSPECTION_CONCURRENCY)
        tasks = tuple(
            asyncio.create_task(
                _inspect_revision_with_github(
                    bookmark_states=bookmark_states,
                    github_client=github_client,
                    github_repository=github_repository,
                    on_github_status=observe_github_status,
                    prepared=prepared,
                    prepared_revision=prepared_revision,
                    semaphore=semaphore,
                )
            )
            for prepared_revision in status_revisions
        )
        try:
            github_status_reported = False
            for task in tasks:
                while True:
                    if not github_status_reported:
                        done, _ = await asyncio.wait(
                            (task, github_status_result),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if github_status_result in done:
                            github_status_reported = True
                            github_error = github_status_result.result()
                            if on_github_status is not None:
                                on_github_status(github_error)
                            if github_error is not None:
                                raise CliError(github_error)
                            if task in done:
                                break
                            continue
                    else:
                        done, _ = await asyncio.wait(
                            (task,),
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    if task in done:
                        break
                yield await task
            if not github_status_reported:
                github_error = None
                if github_status_result.done():
                    github_error = github_status_result.result()
                if on_github_status is not None:
                    on_github_status(github_error)
                if github_error is not None:
                    raise CliError(github_error)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def _inspect_revision_with_github(
    *,
    bookmark_states: dict[str, BookmarkState],
    github_client: GithubClient,
    github_repository,
    on_github_status: Callable[[str | None], None] | None,
    prepared: _PreparedStack,
    prepared_revision: _PreparedRevision,
    semaphore: asyncio.Semaphore,
) -> ReviewStatusRevision:
    async with semaphore:
        bookmark_state = bookmark_states.get(
            prepared_revision.bookmark,
            BookmarkState(name=prepared_revision.bookmark),
        )
        remote_state = (
            bookmark_state.remote_target(prepared.remote.name)
            if prepared.remote
            else None
        )
        head_label = f"{github_repository.owner}:{prepared_revision.bookmark}"
        pull_request_lookup = await _inspect_pull_request(
            github_client=github_client,
            github_repository=github_repository,
            head_label=head_label,
        )
        if on_github_status is not None:
            on_github_status(pull_request_lookup.repository_error)
        stack_comment_lookup: StackCommentLookup | None = None
        if pull_request_lookup.state == "open":
            pull_request = pull_request_lookup.pull_request
            if pull_request is None:
                raise AssertionError("Open pull request lookup must include a pull request.")
            stack_comment_lookup = await _inspect_stack_comment(
                github_client=github_client,
                github_repository=github_repository,
                pull_request_number=pull_request.number,
            )
        logger.debug(
            "status revision inspected: change_id=%s bookmark=%s pr_state=%s",
            prepared_revision.revision.change_id[:12],
            prepared_revision.bookmark,
            pull_request_lookup.state,
        )
        return ReviewStatusRevision(
            bookmark=prepared_revision.bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            cached_change=prepared_revision.cached_change,
            change_id=prepared_revision.revision.change_id,
            link_state=(
                prepared_revision.cached_change.link_state
                if prepared_revision.cached_change is not None
                else "active"
            ),
            local_divergent=getattr(prepared_revision.revision, "divergent", False),
            pull_request_lookup=pull_request_lookup,
            remote_state=remote_state,
            stack_comment_lookup=stack_comment_lookup,
            subject=prepared_revision.revision.subject,
        )


async def _inspect_pull_request(
    *,
    github_client: GithubClient,
    github_repository,
    head_label: str,
) -> PullRequestLookup:
    try:
        pull_requests = await github_client.list_pull_requests(
            github_repository.owner,
            github_repository.repo,
            head=head_label,
        )
    except GithubClientError as error:
        return PullRequestLookup(
            message=_summarize_github_lookup_error(
                action="pull request lookup",
                error=error,
            ),
            pull_request=None,
            repository_error=_summarize_github_repository_error(error)
            if _is_repository_level_github_lookup_error(error)
            else None,
            state="error",
        )

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

    pull_request = pull_requests[0]
    effective_pull_request = _normalize_pull_request_state(pull_request)
    if effective_pull_request.state != "open":
        return PullRequestLookup(
            message=(
                f"GitHub reports pull request #{effective_pull_request.number} for head branch "
                f"{head_label!r} in state {effective_pull_request.state!r}."
            ),
            pull_request=effective_pull_request,
            review_decision=None,
            repository_error=None,
            state="closed",
        )
    review_decision, review_decision_error = await _inspect_pull_request_review_decision(
        github_client=github_client,
        github_repository=github_repository,
        pull_request_number=effective_pull_request.number,
    )
    return PullRequestLookup(
        message=None,
        pull_request=effective_pull_request,
        review_decision=review_decision,
        review_decision_error=review_decision_error,
        repository_error=None,
        state="open",
    )


def _normalize_pull_request_state(pull_request: GithubPullRequest) -> GithubPullRequest:
    if pull_request.state != "closed" or pull_request.merged_at is None:
        return pull_request
    return pull_request.model_copy(update={"state": "merged"})


async def _inspect_pull_request_review_decision(
    *,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> tuple[str | None, str | None]:
    try:
        reviews = await github_client.list_pull_request_reviews(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request_number,
        )
    except GithubClientError:
        return None, "review decision lookup failed"

    latest_relevant_reviews_by_user: dict[str, str] = {}
    for review in reviews:
        if review.user is None:
            continue
        normalized_state = review.state.upper()
        if normalized_state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        latest_relevant_reviews_by_user[review.user.login] = normalized_state

    review_states = set(latest_relevant_reviews_by_user.values())
    if "CHANGES_REQUESTED" in review_states:
        return "changes_requested", None
    if "APPROVED" in review_states:
        return "approved", None
    return None, None


async def _inspect_stack_comment(
    *,
    github_client: GithubClient,
    github_repository,
    pull_request_number: int,
) -> StackCommentLookup:
    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        return StackCommentLookup(
            comment=None,
            message=_summarize_github_lookup_error(
                action=f"stack comment lookup for pull request #{pull_request_number}",
                error=error,
            ),
            state="error",
        )

    matching_comments = [comment for comment in comments if _STACK_COMMENT_MARKER in comment.body]
    if not matching_comments:
        return StackCommentLookup(comment=None, message=None, state="missing")
    if len(matching_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in matching_comments)
        return StackCommentLookup(
            comment=None,
            message=(
                "GitHub reports multiple `jj-review` stack comments for the same pull "
                f"request: {comment_ids}."
            ),
            state="ambiguous",
        )
    return StackCommentLookup(comment=matching_comments[0], message=None, state="present")


def _summarize_github_lookup_error(*, action: str, error: GithubClientError) -> str:
    """Render a concise GitHub lookup failure for `status` output."""

    if error.status_code is None:
        return "GitHub is unavailable - check network connectivity"
    if error.status_code == 401:
        return "GitHub authentication failed - check GITHUB_TOKEN"
    if error.status_code == 403:
        return "GitHub access was denied - check GITHUB_TOKEN and repo access"
    if error.status_code >= 500:
        return "GitHub is unavailable - check network connectivity"
    return f"{action} failed (GitHub {error.status_code})"


def _summarize_github_repository_error(error: GithubClientError) -> str:
    """Render a concise repo-level GitHub availability failure."""

    if error.status_code is None:
        return "unavailable - check network connectivity"
    if error.status_code == 401:
        return "auth failed - check GITHUB_TOKEN"
    if error.status_code == 403:
        return "access denied - check GITHUB_TOKEN and repo access"
    if error.status_code == 404:
        return _github_auth_failure_message("repo not found or inaccessible")
    if error.status_code >= 500:
        return "unavailable - check network connectivity"
    return f"request failed (GitHub {error.status_code})"


def _is_repository_level_github_lookup_error(error: GithubClientError) -> bool:
    if error.status_code is None:
        return True
    if error.status_code in {401, 403, 404}:
        return True
    return error.status_code >= 500


def _github_auth_failure_message(message: str) -> str:
    if _github_token_from_env() is None:
        return f"{message} - check GITHUB_TOKEN or gh auth"
    return message


def _change_id_resolves(client: JjClient, change_id: str) -> bool:
    """Return True if the change_id resolves to a visible revision in the local repo."""
    try:
        client.resolve_revision(change_id)
        return True
    except (RevsetResolutionError, CliError):
        return False
