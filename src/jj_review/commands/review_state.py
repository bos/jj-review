"""Shared review-state inspection for `status` and `sync`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jj_review.bookmarks import BookmarkResolver, BookmarkSource
from jj_review.cache import ReviewStateStore
from jj_review.commands.submit import (
    _STACK_COMMENT_MARKER,
    _build_github_client,
    _discover_bookmarks_for_revisions,
    _ensure_pull_request_linkage_is_consistent,
    _ensure_unique_bookmarks,
    resolve_github_repository,
    select_submit_remote,
)
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.jj import JjClient
from jj_review.models.bookmarks import GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubIssueComment, GithubPullRequest
from jj_review.models.stack import LocalRevision, LocalStack

PullRequestLookupState = Literal["ambiguous", "closed", "error", "missing", "open"]
StackCommentLookupState = Literal["ambiguous", "error", "missing", "present"]


class SyncResolutionError(CliError):
    """Raised when `sync` cannot safely refresh cached review linkage."""


@dataclass(frozen=True, slots=True)
class PullRequestLookup:
    """Best-effort GitHub pull request lookup for one review branch."""

    message: str | None
    pull_request: GithubPullRequest | None
    state: PullRequestLookupState


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
    pull_request_lookup: PullRequestLookup | None
    remote_state: RemoteBookmarkState | None
    stack_comment_lookup: StackCommentLookup | None
    subject: str


@dataclass(frozen=True, slots=True)
class StatusResult:
    """Status result for one selected local stack."""

    github_error: str | None
    github_repository: str | None
    remote: GitRemote | None
    remote_error: str | None
    revisions: tuple[ReviewStatusRevision, ...]
    selected_revset: str
    trunk_subject: str


@dataclass(frozen=True, slots=True)
class SyncResult:
    """Cache-refresh result for one selected local stack."""

    github_repository: str
    remote: GitRemote
    revisions: tuple[ReviewStatusRevision, ...]
    selected_revset: str
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


def run_status(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> StatusResult:
    """Inspect local, cached, and discoverable review linkage."""

    return asyncio.run(
        _run_status_async(
            change_overrides=change_overrides,
            config=config,
            repo_root=repo_root,
            revset=revset,
        )
    )


def run_sync(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> SyncResult:
    """Refresh sparse local review cache from GitHub without mutating remotes."""

    return asyncio.run(
        _run_sync_async(
            change_overrides=change_overrides,
            config=config,
            repo_root=repo_root,
            revset=revset,
        )
    )


async def _run_status_async(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> StatusResult:
    prepared = _prepare_stack(
        change_overrides=change_overrides,
        config=config,
        persist_bookmarks=True,
        refresh_remote_state=False,
        repo_root=repo_root,
        require_remote=False,
        revset=revset,
    )
    if prepared.remote is None:
        return StatusResult(
            github_error=None,
            github_repository=None,
            remote=None,
            remote_error=prepared.remote_error,
            revisions=_build_status_revisions_without_github(prepared),
            selected_revset=prepared.stack.selected_revset,
            trunk_subject=prepared.stack.trunk.subject,
        )

    github_repository_error: str | None = None
    try:
        github_repository = resolve_github_repository(config, prepared.remote)
    except CliError as error:
        github_repository_error = str(error)
        github_repository = None

    if github_repository is None:
        return StatusResult(
            github_error=github_repository_error,
            github_repository=None,
            remote=prepared.remote,
            remote_error=None,
            revisions=_build_status_revisions_without_github(prepared),
            selected_revset=prepared.stack.selected_revset,
            trunk_subject=prepared.stack.trunk.subject,
        )

    try:
        revisions = await _inspect_revisions_with_github(
            github_repository=github_repository,
            prepared=prepared,
            raise_on_lookup_error=False,
        )
        github_error = None
    except CliError as error:
        revisions = _build_status_revisions_without_github(prepared)
        github_error = str(error)

    return StatusResult(
        github_error=github_error,
        github_repository=github_repository.full_name,
        remote=prepared.remote,
        remote_error=None,
        revisions=revisions,
        selected_revset=prepared.stack.selected_revset,
        trunk_subject=prepared.stack.trunk.subject,
    )


async def _run_sync_async(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> SyncResult:
    prepared = _prepare_stack(
        change_overrides=change_overrides,
        config=config,
        persist_bookmarks=False,
        refresh_remote_state=True,
        repo_root=repo_root,
        require_remote=True,
        revset=revset,
    )
    if prepared.remote is None:
        raise AssertionError("Sync requires a resolved remote.")
    github_repository = resolve_github_repository(config, prepared.remote)
    revisions = await _inspect_revisions_with_github(
        github_repository=github_repository,
        prepared=prepared,
        raise_on_lookup_error=True,
    )

    state = prepared.state
    state_changes = dict(prepared.state_changes)
    for revision in revisions:
        cached_change = (
            state_changes.get(revision.change_id)
            or state.changes.get(revision.change_id)
        )
        if (
            revision.bookmark_source == "generated"
            and revision.pull_request_lookup is not None
            and revision.pull_request_lookup.state == "missing"
        ):
            continue
        updated_change = cached_change or CachedChange(bookmark=revision.bookmark)
        pull_request_lookup = revision.pull_request_lookup
        if pull_request_lookup is not None and pull_request_lookup.state == "open":
            pull_request = pull_request_lookup.pull_request
            if pull_request is None:
                raise AssertionError("Open pull request lookup must include a pull request.")
            updated_change = updated_change.model_copy(
                update={
                    "bookmark": revision.bookmark,
                    "pr_number": pull_request.number,
                    "pr_url": pull_request.html_url,
                }
            )
            stack_comment_lookup = revision.stack_comment_lookup
            if stack_comment_lookup is not None and stack_comment_lookup.state == "present":
                if stack_comment_lookup.comment is None:
                    raise AssertionError("Present stack comment lookup must include a comment.")
                updated_change = updated_change.model_copy(
                    update={"stack_comment_id": stack_comment_lookup.comment.id}
                )
            elif stack_comment_lookup is not None and stack_comment_lookup.state == "missing":
                updated_change = updated_change.model_copy(update={"stack_comment_id": None})
        state_changes[revision.change_id] = updated_change

    next_state = state.model_copy(update={"changes": state_changes})
    if prepared.bookmark_result_changed or next_state != state:
        prepared.state_store.save(next_state)

    return SyncResult(
        github_repository=github_repository.full_name,
        remote=prepared.remote,
        revisions=revisions,
        selected_revset=prepared.stack.selected_revset,
        trunk_subject=prepared.stack.trunk.subject,
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
    stack = client.discover_review_stack(revset)
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


async def _inspect_revisions_with_github(
    *,
    github_repository,
    prepared: _PreparedStack,
    raise_on_lookup_error: bool,
) -> tuple[ReviewStatusRevision, ...]:
    async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
        revisions: list[ReviewStatusRevision] = []
        for prepared_revision in prepared.status_revisions:
            bookmark_state = prepared.client.get_bookmark_state(prepared_revision.bookmark)
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
            if raise_on_lookup_error:
                _ensure_syncable_pull_request(
                    bookmark=prepared_revision.bookmark,
                    cached_change=prepared_revision.cached_change,
                    pull_request_lookup=pull_request_lookup,
                )
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
                if raise_on_lookup_error:
                    _ensure_syncable_stack_comment(
                        pull_request_number=pull_request.number,
                        stack_comment_lookup=stack_comment_lookup,
                    )
            revisions.append(
                ReviewStatusRevision(
                    bookmark=prepared_revision.bookmark,
                    bookmark_source=prepared_revision.bookmark_source,
                    cached_change=prepared_revision.cached_change,
                    change_id=prepared_revision.revision.change_id,
                    pull_request_lookup=pull_request_lookup,
                    remote_state=remote_state,
                    stack_comment_lookup=stack_comment_lookup,
                    subject=prepared_revision.revision.subject,
                )
            )
        return tuple(revisions)


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
            message=f"Could not list pull requests for head {head_label!r}: {error}",
            pull_request=None,
            state="error",
        )

    if not pull_requests:
        return PullRequestLookup(message=None, pull_request=None, state="missing")
    if len(pull_requests) > 1:
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        return PullRequestLookup(
            message=(
                f"GitHub reports multiple pull requests for head branch {head_label!r}: "
                f"{numbers}."
            ),
            pull_request=None,
            state="ambiguous",
        )

    pull_request = pull_requests[0]
    if pull_request.state != "open":
        return PullRequestLookup(
            message=(
                f"GitHub reports pull request #{pull_request.number} for head branch "
                f"{head_label!r} in state {pull_request.state!r}."
            ),
            pull_request=pull_request,
            state="closed",
        )
    return PullRequestLookup(message=None, pull_request=pull_request, state="open")


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
            message=(
                f"Could not list stack comments for pull request #{pull_request_number}: {error}"
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


def _ensure_syncable_pull_request(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    pull_request_lookup: PullRequestLookup,
) -> None:
    if pull_request_lookup.state == "open":
        pull_request = pull_request_lookup.pull_request
        if pull_request is None:
            raise AssertionError("Open pull request lookup must include a pull request.")
        _ensure_pull_request_linkage_is_consistent(
            bookmark=bookmark,
            cached_change=cached_change,
            discovered_pull_request=pull_request,
        )
        return
    if pull_request_lookup.state == "missing":
        if cached_change is not None and (
            cached_change.pr_number is not None or cached_change.pr_url is not None
        ):
            raise SyncResolutionError(
                f"Cached pull request linkage exists for bookmark {bookmark!r}, but GitHub "
                "no longer reports an open PR for that head branch. Repair the linkage with "
                "`adopt` before syncing again."
            )
        return
    message = pull_request_lookup.message or "Unknown pull request lookup failure."
    raise SyncResolutionError(f"{message} Repair the linkage with `adopt` before syncing again.")


def _ensure_syncable_stack_comment(
    *,
    pull_request_number: int,
    stack_comment_lookup: StackCommentLookup,
) -> None:
    if stack_comment_lookup.state in {"missing", "present"}:
        return
    message = stack_comment_lookup.message or "Unknown stack comment lookup failure."
    raise SyncResolutionError(
        f"{message} Repair the linkage before syncing pull request #{pull_request_number} again."
    )
