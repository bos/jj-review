"""Review status preparation and GitHub inspection helpers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from jj_review import ui
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError, ErrorMessage, error_message
from jj_review.formatting import short_change_id
from jj_review.github.client import (
    GithubClient,
    GithubClientError,
    build_github_client,
)
from jj_review.github.error_messages import (
    summarize_github_lookup_error,
)
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    select_submit_remote,
)
from jj_review.github.stack_comments import is_stack_summary_comment
from jj_review.jj import JjClient, UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import GithubIssueComment, GithubPullRequest
from jj_review.models.intent import LoadedIntent
from jj_review.models.review_state import CachedChange, LinkState, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.bookmarks import (
    BookmarkResolver,
    BookmarkSource,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
)
from jj_review.review.intents import intent_is_stale
from jj_review.state.store import ReviewStateStore
from jj_review.ui import Message

logger = logging.getLogger(__name__)
_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY

HELP = "Check the review status of a jj stack"

PullRequestLookupState = Literal["ambiguous", "closed", "error", "missing", "open"]
StackCommentLookupState = Literal["ambiguous", "error", "missing", "present"]


@dataclass(frozen=True, slots=True)
class PullRequestLookup:
    """Best-effort GitHub pull request lookup for one branch."""

    message: ErrorMessage | None
    pull_request: GithubPullRequest | None
    state: PullRequestLookupState
    review_decision: str | None = None
    review_decision_error: str | None = None
    repository_error: ErrorMessage | None = None


@dataclass(frozen=True, slots=True)
class StackCommentLookup:
    """Best-effort GitHub stack summary comment lookup for one pull request."""

    comment: GithubIssueComment | None
    message: ErrorMessage | None
    state: StackCommentLookupState


@dataclass(frozen=True, slots=True)
class ReviewStatusRevision:
    """Rendered pull-request and branch state for one local revision."""

    bookmark: str
    bookmark_source: BookmarkSource
    cached_change: CachedChange | None
    change_id: str
    commit_id: str
    link_state: LinkState
    local_divergent: bool
    pull_request_lookup: PullRequestLookup | None
    remote_state: RemoteBookmarkState | None
    stack_comment_lookup: StackCommentLookup | None
    subject: str


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Status result for one selected local stack."""

    github_error: ErrorMessage | None
    github_repository: str | None
    incomplete: bool
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    revisions: tuple[ReviewStatusRevision, ...]
    selected_revset: str
    base_parent_subject: str


@dataclass(frozen=True, slots=True)
class PreparedStatus:
    """Locally prepared status inputs before any GitHub inspection."""

    github_repository: ParsedGithubRepo | None
    github_repository_error: ErrorMessage | None
    outstanding_intents: tuple[LoadedIntent, ...]
    prepared: PreparedStack
    selected_revset: str
    stale_intents: tuple[LoadedIntent, ...]
    base_parent_subject: str


@dataclass(frozen=True, slots=True)
class PreparedStack:
    """Prepared local stack inputs shared across inspection-driven commands."""

    bookmark_states: dict[str, BookmarkState]
    bookmark_result_changed: bool
    client: JjClient
    remote: GitRemote | None
    remote_error: ErrorMessage | None
    stack: LocalStack
    state: ReviewState
    state_changes: dict[str, CachedChange]
    state_store: ReviewStateStore
    status_revisions: tuple[PreparedRevision, ...]


@dataclass(frozen=True, slots=True)
class PreparedRevision:
    """Local review revision with resolved bookmark and cached state."""

    bookmark: str
    bookmark_source: BookmarkSource
    cached_change: CachedChange | None
    revision: LocalRevision


def status_preparation_cli_error(error: UnsupportedStackError) -> CliError:
    """Translate stack-shape preparation failures into a user-facing CLI error."""

    if error.reason == "divergent_change" and error.change_id is not None:
        return CliError(
            t"Could not inspect review status because local history no longer forms a "
            t"supported linear stack. {error}",
            hint=(
                t"Inspect the divergent revisions with {ui.cmd('jj log -r')} "
                t"{ui.revset(f'change_id({error.change_id})')} and reconcile them "
                t"before retrying. This can happen after {ui.cmd('status --fetch')} "
                t"or another fetch imports remote bookmark updates for landed PRs."
            ),
        )
    return CliError(
        t"Could not inspect review status because local history no longer forms a "
        t"supported linear stack. {error}"
    )


def prepare_status(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    fetch_remote_state: bool = False,
    fetch_only_when_tracked: bool = False,
    persist_bookmarks: bool = False,
    re_resolve_after_remote_refresh: bool = False,
    repo_root: Path,
    revset: str | None,
) -> PreparedStatus:
    """Resolve local status inputs before any GitHub network inspection."""

    prepared = _prepare_stack(
        change_overrides=change_overrides,
        config=config,
        persist_bookmarks=persist_bookmarks,
        re_resolve_after_remote_refresh=re_resolve_after_remote_refresh,
        refresh_remote_state=fetch_remote_state,
        refresh_requires_tracked=fetch_only_when_tracked,
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
    github_repository = None
    github_repository_error = None
    if prepared.remote is not None:
        github_repository = parse_github_repo(prepared.remote)
        if github_repository is None:
            github_repository_error = (
                t"Could not determine the GitHub repository for remote "
                t"{ui.bookmark(prepared.remote.name)}. Use a GitHub remote URL."
            )
    outstanding_intents, stale_intents = _classify_status_intents(prepared)

    return PreparedStatus(
        github_repository=github_repository,
        github_repository_error=github_repository_error,
        outstanding_intents=outstanding_intents,
        prepared=prepared,
        selected_revset=prepared.stack.selected_revset,
        stale_intents=stale_intents,
        base_parent_subject=prepared.stack.base_parent.subject,
    )


def _classify_status_intents(
    prepared: PreparedStack,
) -> tuple[tuple[LoadedIntent, ...], tuple[LoadedIntent, ...]]:
    outstanding_intents: list[LoadedIntent] = []
    stale_intents: list[LoadedIntent] = []
    now = datetime.now(UTC)

    for loaded in prepared.state_store.list_intents():
        if intent_is_stale(
            loaded.intent,
            lambda change_id: _change_id_resolves(prepared.client, change_id),
            now=now,
        ):
            stale_intents.append(loaded)
        else:
            outstanding_intents.append(loaded)
    return tuple(outstanding_intents), tuple(stale_intents)


def stream_status(
    *,
    discover_remote_review: bool = False,
    inspect_stack_comments: bool = True,
    persist_cache_updates: bool = True,
    prepared_status: PreparedStatus,
    on_github_status: Callable[[str | None, ErrorMessage | None], None] | None = None,
    on_revision: Callable[[ReviewStatusRevision, bool], None] | None = None,
) -> StatusResult:
    """Inspect GitHub state for a prepared stack and optionally stream results out."""

    return asyncio.run(
        stream_status_async(
            discover_remote_review=discover_remote_review,
            inspect_stack_comments=inspect_stack_comments,
            on_github_status=on_github_status,
            on_revision=on_revision,
            persist_cache_updates=persist_cache_updates,
            prepared_status=prepared_status,
        )
    )


async def stream_status_async(
    *,
    discover_remote_review: bool = False,
    inspect_stack_comments: bool = True,
    on_github_status: Callable[[str | None, ErrorMessage | None], None] | None,
    on_revision: Callable[[ReviewStatusRevision, bool], None] | None,
    persist_cache_updates: bool = True,
    prepared_status: PreparedStatus,
) -> StatusResult:
    prepared = prepared_status.prepared
    selected_revset = prepared_status.selected_revset
    base_parent_subject = prepared_status.base_parent_subject
    github_repository = prepared_status.github_repository
    github_repository_error = prepared_status.github_repository_error

    if prepared.remote is None:
        display_revisions = tuple(reversed(_build_status_revisions_without_github(prepared)))
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
            base_parent_subject=base_parent_subject,
        )

    if github_repository is None:
        logger.debug("status github target unavailable: %s", github_repository_error)
        display_revisions = tuple(reversed(_build_status_revisions_without_github(prepared)))
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
            base_parent_subject=base_parent_subject,
        )

    github_status_reported = False

    def emit_github_status(github_error: ErrorMessage | None) -> None:
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
            base_parent_subject=base_parent_subject,
        )

    fallback_revisions = tuple(reversed(_build_status_revisions_without_github(prepared)))
    prepared_revisions_for_github = tuple(
        prepared_revision
        for prepared_revision in prepared.status_revisions
        if _prepared_revision_needs_github_inspection(
            prepared_revision,
            discover_remote_review=discover_remote_review,
        )
    )
    if not prepared_revisions_for_github:
        return StatusResult(
            github_error=None,
            github_repository=github_repository.full_name,
            incomplete=_status_is_incomplete(fallback_revisions),
            remote=prepared.remote,
            remote_error=None,
            revisions=fallback_revisions,
            selected_revset=selected_revset,
            base_parent_subject=base_parent_subject,
        )

    revisions: list[ReviewStatusRevision] = []
    try:
        async for revision in _iter_status_revisions_with_github(
            github_repository=github_repository,
            inspect_stack_comments=inspect_stack_comments,
            on_github_status=emit_github_status,
            prepared=prepared,
            prepared_revisions=prepared_revisions_for_github,
        ):
            revisions.append(revision)
            if on_revision is not None:
                on_revision(revision, True)
    except CliError as error:
        if not github_status_reported:
            emit_github_status(None)
        github_error = error_message(error)
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
            base_parent_subject=base_parent_subject,
        )

    if not github_status_reported:
        emit_github_status(None)
    revisions_by_change_id = {revision.change_id: revision for revision in revisions}
    display_revisions = tuple(
        revisions_by_change_id.get(revision.change_id, revision)
        for revision in fallback_revisions
    )
    if persist_cache_updates:
        _persist_status_cache_updates(prepared=prepared, revisions=display_revisions)
    return StatusResult(
        github_error=None,
        github_repository=github_repository.full_name,
        incomplete=_status_is_incomplete(display_revisions),
        remote=prepared.remote,
        remote_error=None,
        revisions=display_revisions,
        selected_revset=selected_revset,
        base_parent_subject=base_parent_subject,
    )


def _prepare_stack(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    persist_bookmarks: bool,
    re_resolve_after_remote_refresh: bool,
    refresh_remote_state: bool,
    refresh_requires_tracked: bool = False,
    repo_root: Path,
    require_remote: bool,
    revset: str | None,
) -> PreparedStack:
    client = JjClient(repo_root)
    state_store = ReviewStateStore.for_repo(repo_root)
    state = state_store.load()
    remotes = client.list_git_remotes()
    remote: GitRemote | None = None
    remote_error: ErrorMessage | None = None
    if require_remote or remotes:
        try:
            remote = select_submit_remote(remotes)
        except CliError as error:
            if require_remote:
                raise
            remote_error = error_message(error)
    logger.debug(
        "prepared stack: remotes=%s selected_remote=%s remote_error=%s",
        [remote.name for remote in remotes],
        remote.name if remote is not None else None,
        remote_error,
    )
    stack: LocalStack | None = None
    if (
        remote is not None
        and refresh_remote_state
        and re_resolve_after_remote_refresh
        and not refresh_requires_tracked
    ):
        client.fetch_remote(remote=remote.name)
    else:
        stack = client.discover_review_stack(
            revset,
            allow_divergent=True,
            allow_immutable=True,
        )
        if remote is not None and refresh_remote_state:
            should_fetch = not refresh_requires_tracked or any(
                (cached := state.changes.get(revision.change_id)) is not None
                and cached.has_review_identity
                for revision in stack.revisions
            )
            if should_fetch:
                client.fetch_remote(remote=remote.name)
                if re_resolve_after_remote_refresh:
                    stack = None

    if stack is None:
        stack = client.discover_review_stack(
            revset,
            allow_divergent=True,
            allow_immutable=True,
        )

    pinned_bookmarks = _pinned_bookmarks_for_revisions(
        change_overrides=change_overrides,
        revisions=stack.revisions,
        state=state,
    )
    bookmark_states: dict[str, BookmarkState] = {}
    if remote is not None:
        bookmark_states = client.list_bookmark_states(pinned_bookmarks)

    discovered_bookmarks: dict[str, str] = {}
    if remote is not None and pinned_bookmarks is None:
        discovered_bookmarks = discover_bookmarks_for_revisions(
            bookmark_states=bookmark_states,
            remote_name=remote.name,
            revisions=stack.revisions,
        )

    bookmark_result = BookmarkResolver(
        state,
        change_overrides,
        discovered_bookmarks=discovered_bookmarks,
    ).pin_revisions(stack.revisions)
    ensure_unique_bookmarks(bookmark_result.resolutions)
    if persist_bookmarks and bookmark_result.changed:
        state_store.save(bookmark_result.state)

    state_changes = dict(bookmark_result.state.changes if persist_bookmarks else state.changes)
    status_revisions = tuple(
        PreparedRevision(
            bookmark=resolution.bookmark,
            bookmark_source=resolution.source,
            cached_change=(
                state_changes.get(revision.change_id) or state.changes.get(revision.change_id)
            ),
            revision=revision,
        )
        for resolution, revision in zip(
            bookmark_result.resolutions,
            stack.revisions,
            strict=True,
        )
    )
    return PreparedStack(
        bookmark_states=bookmark_states,
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


def _pinned_bookmarks_for_revisions(
    *,
    change_overrides: dict[str, ChangeConfig],
    revisions: tuple[LocalRevision, ...],
    state: ReviewState,
) -> tuple[str, ...] | None:
    """Return pinned bookmark names if every revision is already pinned, else None.

    Used to avoid listing every repo bookmark when rediscovery is impossible: if
    every revision has an override or saved bookmark, the rediscovery search has
    nothing to look for.
    """

    pinned: list[str] = []
    for revision in revisions:
        override = change_overrides.get(revision.change_id)
        if override is not None and override.bookmark_override:
            pinned.append(override.bookmark_override)
            continue
        cached = state.changes.get(revision.change_id)
        if cached is not None and cached.bookmark:
            pinned.append(cached.bookmark)
            continue
        return None
    return tuple(dict.fromkeys(pinned))


def _build_status_revisions_without_github(
    prepared: PreparedStack,
) -> tuple[ReviewStatusRevision, ...]:
    return tuple(
        ReviewStatusRevision(
            bookmark=revision.bookmark,
            bookmark_source=revision.bookmark_source,
            cached_change=revision.cached_change,
            change_id=revision.revision.change_id,
            commit_id=revision.revision.commit_id,
            link_state=(
                revision.cached_change.link_state
                if revision.cached_change is not None
                else "active"
            ),
            local_divergent=getattr(revision.revision, "divergent", False),
            pull_request_lookup=None,
            remote_state=(
                prepared.bookmark_states.get(
                    revision.bookmark,
                    BookmarkState(name=revision.bookmark),
                ).remote_target(prepared.remote.name)
                if prepared.remote is not None
                else None
            ),
            stack_comment_lookup=None,
            subject=revision.revision.subject,
        )
        for revision in prepared.status_revisions
    )


def prepared_status_github_inspection_count(
    *,
    discover_remote_review: bool = False,
    prepared_status: PreparedStatus,
) -> int:
    """Return how many selected revisions need live GitHub inspection."""

    if prepared_status.github_repository is None:
        return 0
    return sum(
        1
        for prepared_revision in prepared_status.prepared.status_revisions
        if _prepared_revision_needs_github_inspection(
            prepared_revision,
            discover_remote_review=discover_remote_review,
        )
    )


def _prepared_revision_needs_github_inspection(
    prepared_revision: PreparedRevision,
    *,
    discover_remote_review: bool,
) -> bool:
    if discover_remote_review:
        return True
    cached_change = getattr(prepared_revision, "cached_change", None)
    return cached_change is not None and cached_change.has_review_identity


def _status_is_incomplete(revisions: tuple[ReviewStatusRevision, ...]) -> bool:
    for revision in revisions:
        if revision.local_divergent and not revision_has_merged_pull_request(revision):
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


def revision_has_merged_pull_request(revision: ReviewStatusRevision) -> bool:
    lookup = revision.pull_request_lookup
    return (
        lookup is not None
        and lookup.state == "closed"
        and lookup.pull_request is not None
        and lookup.pull_request.state == "merged"
    )


def revision_pull_request_number(revision: ReviewStatusRevision) -> int | None:
    lookup = revision.pull_request_lookup
    if lookup is None or lookup.pull_request is None:
        return None
    return lookup.pull_request.number


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
    prepared: PreparedStack,
    revisions: tuple[ReviewStatusRevision, ...],
) -> None:
    state_changes = dict(prepared.state_changes)
    for revision in revisions:
        cached_change = state_changes.get(revision.change_id) or prepared.state.changes.get(
            revision.change_id
        )
        updated_change = cached_change
        if cached_change is not None and cached_change.is_unlinked:
            if updated_change != cached_change:
                state_changes[revision.change_id] = cached_change
            continue
        pull_request_lookup = revision.pull_request_lookup
        if pull_request_lookup is not None:
            if updated_change is None:
                updated_change = CachedChange(bookmark=revision.bookmark)
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
                    updated_change = updated_change.model_copy(update={"stack_comment_id": None})
        stack_comment_lookup = revision.stack_comment_lookup
        if stack_comment_lookup is not None:
            if updated_change is None:
                updated_change = CachedChange(bookmark=revision.bookmark)
            if stack_comment_lookup.state == "present":
                if stack_comment_lookup.comment is None:
                    raise AssertionError(
                        "Present stack summary comment lookup must include a comment."
                    )
                updated_change = updated_change.model_copy(
                    update={"stack_comment_id": stack_comment_lookup.comment.id}
                )
            elif stack_comment_lookup.state == "missing":
                updated_change = updated_change.model_copy(update={"stack_comment_id": None})
        if updated_change is not None and updated_change != cached_change:
            state_changes[revision.change_id] = updated_change

    next_state = prepared.state.model_copy(update={"changes": state_changes})
    if next_state != prepared.state:
        prepared.state_store.save(next_state)


async def _iter_status_revisions_with_github(
    *,
    github_repository: ParsedGithubRepo,
    inspect_stack_comments: bool,
    on_github_status: Callable[[str | None], None] | None,
    prepared: PreparedStack,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> AsyncIterator[ReviewStatusRevision]:
    ordered_prepared_revisions = tuple(reversed(prepared_revisions))
    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        pull_request_lookups = await _discover_pull_request_lookups(
            github_client=github_client,
            github_repository=github_repository,
            prepared_revisions=ordered_prepared_revisions,
        )
        pull_request_lookups = await _attach_review_decisions_to_pull_request_lookups(
            github_client=github_client,
            github_repository=github_repository,
            pull_request_lookups=pull_request_lookups,
        )
        if on_github_status is not None:
            on_github_status(None)
        semaphore = asyncio.Semaphore(_GITHUB_INSPECTION_CONCURRENCY)
        tasks = tuple(
            asyncio.create_task(
                _inspect_revision_with_github(
                    bookmark_states=prepared.bookmark_states,
                    github_client=github_client,
                    github_repository=github_repository,
                    inspect_stack_comments=inspect_stack_comments,
                    prepared=prepared,
                    prepared_revision=prepared_revision,
                    pull_request_lookup=pull_request_lookups[prepared_revision.bookmark],
                    semaphore=semaphore,
                )
            )
            for prepared_revision in ordered_prepared_revisions
        )
        try:
            for task in tasks:
                yield await task
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
    inspect_stack_comments: bool,
    prepared: PreparedStack,
    prepared_revision: PreparedRevision,
    pull_request_lookup: PullRequestLookup,
    semaphore: asyncio.Semaphore,
) -> ReviewStatusRevision:
    async with semaphore:
        bookmark_state = bookmark_states.get(
            prepared_revision.bookmark,
            BookmarkState(name=prepared_revision.bookmark),
        )
        remote_state = (
            bookmark_state.remote_target(prepared.remote.name) if prepared.remote else None
        )
        stack_comment_lookup: StackCommentLookup | None = None
        if inspect_stack_comments and pull_request_lookup.state == "open":
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
            short_change_id(prepared_revision.revision.change_id),
            prepared_revision.bookmark,
            pull_request_lookup.state,
        )
        return ReviewStatusRevision(
            bookmark=prepared_revision.bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            cached_change=prepared_revision.cached_change,
            change_id=prepared_revision.revision.change_id,
            commit_id=prepared_revision.revision.commit_id,
            link_state=(
                prepared_revision.cached_change.link_state
                if prepared_revision.cached_change is not None
                else "active"
            ),
            local_divergent=prepared_revision.revision.divergent,
            pull_request_lookup=pull_request_lookup,
            remote_state=remote_state,
            stack_comment_lookup=stack_comment_lookup,
            subject=prepared_revision.revision.subject,
        )


async def _discover_pull_request_lookups(
    *,
    github_client: GithubClient,
    github_repository,
    prepared_revisions: tuple[PreparedRevision, ...],
) -> dict[str, PullRequestLookup]:
    bookmarks = tuple(prepared_revision.bookmark for prepared_revision in prepared_revisions)
    if not bookmarks:
        return {}

    try:
        discovered_pull_requests = await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=bookmarks,
        )
    except GithubClientError as error:
        if _is_repository_level_github_lookup_error(error):
            raise CliError("") from error
        lookup_error = summarize_github_lookup_error(
            action="pull request lookup",
            error=error,
        )
        return {
            bookmark: PullRequestLookup(
                message=lookup_error,
                pull_request=None,
                repository_error=None,
                state="error",
            )
            for bookmark in bookmarks
        }

    return {
        bookmark: _pull_request_lookup_from_discovered(
            head_label=t"{github_repository.owner}:{ui.bookmark(bookmark)}",
            pull_requests=discovered_pull_requests.get(bookmark, ()),
        )
        for bookmark in bookmarks
    }


async def _attach_review_decisions_to_pull_request_lookups(
    *,
    github_client: GithubClient,
    github_repository,
    pull_request_lookups: dict[str, PullRequestLookup],
) -> dict[str, PullRequestLookup]:
    open_pull_requests = {
        bookmark: lookup.pull_request
        for bookmark, lookup in pull_request_lookups.items()
        if lookup.state == "open" and lookup.pull_request is not None
    }
    if not open_pull_requests:
        return pull_request_lookups

    try:
        review_decisions = await github_client.get_review_decisions_by_pull_request_numbers(
            github_repository.owner,
            github_repository.repo,
            pull_numbers=tuple(
                pull_request.number for pull_request in open_pull_requests.values()
            ),
        )
    except GithubClientError:
        return {
            bookmark: (
                replace(lookup, review_decision_error="review decision lookup failed")
                if bookmark in open_pull_requests
                else lookup
            )
            for bookmark, lookup in pull_request_lookups.items()
        }

    return {
        bookmark: (
            replace(
                lookup,
                review_decision=review_decisions.get(pull_request.number),
                review_decision_error=None,
            )
            if (lookup.state == "open" and (pull_request := lookup.pull_request) is not None)
            else lookup
        )
        for bookmark, lookup in pull_request_lookups.items()
    }


def _pull_request_lookup_from_discovered(
    *,
    head_label: Message,
    pull_requests: tuple[GithubPullRequest, ...],
) -> PullRequestLookup:
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
                t"GitHub reports multiple pull requests for head branch "
                t"{head_label}: {numbers}."
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
                t"GitHub reports pull request #{effective_pull_request.number} "
                t"for head branch {head_label} in state "
                t"{effective_pull_request.state}."
            ),
            pull_request=effective_pull_request,
            review_decision=None,
            repository_error=None,
            state="closed",
        )
    return PullRequestLookup(
        message=None,
        pull_request=effective_pull_request,
        review_decision=None,
        review_decision_error=None,
        repository_error=None,
        state="open",
    )


def _normalize_pull_request_state(pull_request: GithubPullRequest) -> GithubPullRequest:
    if pull_request.state != "closed" or pull_request.merged_at is None:
        return pull_request
    return pull_request.model_copy(update={"state": "merged"})


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
            message=summarize_github_lookup_error(
                action=f"stack summary comment lookup for pull request #{pull_request_number}",
                error=error,
            ),
            state="error",
        )

    matching_comments = [
        comment for comment in comments if is_stack_summary_comment(comment.body)
    ]
    if not matching_comments:
        return StackCommentLookup(comment=None, message=None, state="missing")
    if len(matching_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in matching_comments)
        return StackCommentLookup(
            comment=None,
            message=(
                "GitHub reports multiple jj-review stack summary comments for the same "
                f"request: {comment_ids}."
            ),
            state="ambiguous",
        )
    return StackCommentLookup(comment=matching_comments[0], message=None, state="present")


def _is_repository_level_github_lookup_error(error: GithubClientError) -> bool:
    if error.status_code is None:
        return True
    if error.status_code in {401, 403, 404}:
        return True
    return error.status_code >= 500


def _change_id_resolves(client: JjClient, change_id: str) -> bool:
    """Return True if the change_id resolves to a visible revision in the local repo."""
    try:
        client.resolve_revision(change_id)
        return True
    except CliError:
        return False
