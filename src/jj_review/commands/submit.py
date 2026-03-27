"""Submit command support for syncing remote bookmarks and pull requests."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, TypeVar
from urllib.parse import urlparse

from jj_review.bookmarks import BookmarkResolver, BookmarkSource, ResolvedBookmark
from jj_review.cache import ReviewStateStore
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.intent import (
    check_same_kind_intent,
    delete_intent,
    match_ordered_change_ids,
    pid_is_alive,
    retire_superseded_intents,
    scan_intents,
    write_intent,
)
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubIssueComment, GithubPullRequest, GithubRepository
from jj_review.models.intent import LoadedIntent, SubmitIntent


class SubmitRemoteResolutionError(CliError):
    """Raised when `submit` cannot resolve which Git remote to use."""


class SubmitBookmarkCollisionError(CliError):
    """Raised when multiple review units resolve to the same bookmark."""


class SubmitBookmarkConflictError(CliError):
    """Raised when a local bookmark has multiple conflicting targets."""


class SubmitBookmarkResolutionError(CliError):
    """Raised when `submit` cannot safely rediscover review bookmark link."""


class SubmitRemoteBookmarkConflictError(CliError):
    """Raised when the selected remote bookmark is conflicted."""


class SubmitRemoteBookmarkOwnershipError(CliError):
    """Raised when `submit` cannot prove an existing remote branch belongs to it."""


class SubmitGithubResolutionError(CliError):
    """Raised when `submit` cannot resolve GitHub repository information."""


class SubmitPullRequestResolutionError(CliError):
    """Raised when `submit` cannot safely resolve a pull request."""


class SubmitDetachedChangeError(CliError):
    """Raised when `submit` hits a change explicitly detached from review."""


class SubmitPrivateCommitError(CliError):
    """Raised when the stack contains commits blocked by git.private-commits."""


class SubmitStackCommentError(CliError):
    """Raised when `submit` cannot create or update stack metadata comments."""


class SubmitDescriptionCommandError(CliError):
    """Raised when `submit` cannot generate metadata through a helper command."""


LocalBookmarkAction = Literal["created", "moved", "unchanged"]
PullRequestAction = Literal["created", "unchanged", "updated"]
SubmitDraftMode = Literal["default", "draft", "draft_all", "publish"]
RemoteBookmarkAction = Literal["pushed", "up to date"]
PushOperation = Literal["batch", "git_update", "up_to_date"]
_DEFAULT_GITHUB_HOST = "github.com"
_GITHUB_INSPECTION_CONCURRENCY = 4
_DESCRIBE_WITH_STACK_INPUT_ENV = "JJ_REVIEW_STACK_INPUT_FILE"
_STACK_COMMENT_MARKER = "<!-- jj-review-stack -->"


@dataclass(frozen=True, slots=True)
class SubmittedRevision:
    """Remote bookmark and GitHub result for one revision in the submitted stack."""

    bookmark: str
    bookmark_source: BookmarkSource
    change_id: str
    local_action: LocalBookmarkAction
    pull_request_action: PullRequestAction
    pull_request_is_draft: bool | None
    pull_request_number: int | None
    pull_request_url: str | None
    remote_action: RemoteBookmarkAction
    subject: str


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Remote bookmark and pull request state for the selected stack."""

    dry_run: bool
    remote: GitRemote
    revisions: tuple[SubmittedRevision, ...]
    selected_revset: str
    trunk_branch: str
    trunk_subject: str


@dataclass(frozen=True, slots=True)
class ResolvedGithubRepository:
    """Resolved GitHub repository target for the selected remote."""

    host: str
    owner: str
    repo: str

    @property
    def api_base_url(self) -> str:
        if self.host == _DEFAULT_GITHUB_HOST:
            return "https://api.github.com"
        return f"https://api.{self.host}"

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True, slots=True)
class ParsedRemoteUrl:
    """Owner, repo, and host parsed from a Git remote URL."""

    host: str
    owner: str
    repo: str


@dataclass(frozen=True, slots=True)
class PullRequestSyncResult:
    """Result of creating, reusing, or updating one pull request."""

    action: PullRequestAction
    cached_change: CachedChange | None
    pull_request: GithubPullRequest | None


@dataclass(frozen=True, slots=True)
class GeneratedDescription:
    """Generated title/body pair for a pull request or stack summary."""

    body: str
    title: str


@dataclass(frozen=True, slots=True)
class PreparedSubmitRevision:
    """Local submit state gathered before remote and GitHub mutation."""

    bookmark: str
    bookmark_source: BookmarkSource
    change_id: str
    expected_remote_target: str | None
    local_action: LocalBookmarkAction
    push_operation: PushOperation
    remote_action: RemoteBookmarkAction
    revision: Any


@dataclass(frozen=True, slots=True)
class SubmittedPullRequestSync:
    """One completed PR sync plus its cache update."""

    cached_change: CachedChange | None
    submitted_revision: SubmittedRevision


@dataclass(frozen=True, slots=True)
class PendingPullRequestSync:
    """One queued PR sync task."""

    base_branch: str
    discovered_pull_request: GithubPullRequest | None
    generated_description: GeneratedDescription
    prepared_revision: PreparedSubmitRevision


@dataclass(frozen=True, slots=True)
class PendingStackCommentSync:
    """One queued stack-comment sync task."""

    cached_change: CachedChange
    change_id: str
    comment_body: str
    pull_request_number: int


class BookmarkStateReader(Protocol):
    """Subset of the jj client interface needed for trunk-branch fallback."""

    def list_bookmark_states(self) -> dict[str, BookmarkState]:
        """Return bookmark state keyed by bookmark name."""


class PrivateCommitFinder(Protocol):
    """Subset of the jj client interface needed for git.private-commits checks."""

    def find_private_commits(
        self,
        revisions: tuple[Any, ...],
    ) -> tuple[Any, ...]:
        """Return the revisions blocked by the repo's private-commit policy."""


class RemoteBookmarkSyncer(Protocol):
    """Subset of the jj client interface needed for remote bookmark updates."""

    def push_bookmarks(self, *, remote: str, bookmarks: tuple[str, ...]) -> None:
        """Push a batch of bookmarks to the selected remote."""

    def update_untracked_remote_bookmark(
        self,
        *,
        remote: str,
        bookmark: str,
        desired_target: str,
        expected_remote_target: str,
    ) -> None:
        """Update an existing untracked remote bookmark without importing it first."""


class InterruptedRemoteBookmarkRepairer(Protocol):
    """Subset of the jj client interface needed for stale remote bookmark repair."""

    def fetch_remote(self, *, remote: str) -> None:
        """Refresh remembered remote bookmark state for the selected remote."""

    def list_bookmark_states(
        self,
        bookmarks: tuple[str, ...] | None = None,
    ) -> dict[str, BookmarkState]:
        """Return local and remote state for the requested bookmark names."""

    def track_bookmark(self, *, remote: str, bookmark: str) -> None:
        """Track an existing remote bookmark locally."""


_TaskItemT = TypeVar("_TaskItemT")
_TaskResultT = TypeVar("_TaskResultT")


def run_submit(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    describe_with: str | None = None,
    draft_mode: SubmitDraftMode = "default",
    dry_run: bool = False,
    on_prepared: Callable[[str, GitRemote, bool], None] | None = None,
    on_trunk_resolved: Callable[[str, str, bool], None] | None = None,
    repo_root: Path,
    revset: str | None,
    reviewers: list[str] | None = None,
    team_reviewers: list[str] | None = None,
) -> SubmitResult:
    """Submit the selected local stack as review bookmarks and pull requests."""

    state_store = ReviewStateStore.for_repo(repo_root)
    state_dir = state_store.require_writable() if not dry_run else state_store.state_dir

    return asyncio.run(
        _run_submit_async(
            change_overrides=change_overrides,
            config=config,
            describe_with=describe_with,
            draft_mode=draft_mode,
            dry_run=dry_run,
            on_prepared=on_prepared,
            on_trunk_resolved=on_trunk_resolved,
            repo_root=repo_root,
            revset=revset,
            reviewers=reviewers,
            state_dir=state_dir,
            state_store=state_store,
            team_reviewers=team_reviewers,
        )
    )


async def _run_submit_async(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    describe_with: str | None,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    on_prepared: Callable[[str, GitRemote, bool], None] | None,
    on_trunk_resolved: Callable[[str, str, bool], None] | None,
    repo_root: Path,
    revset: str | None,
    reviewers: list[str] | None,
    state_dir: Path | None,
    state_store: ReviewStateStore,
    team_reviewers: list[str] | None,
) -> SubmitResult:
    client = JjClient(repo_root)
    remotes = client.list_git_remotes()
    remote = select_submit_remote(config, remotes)
    if not dry_run:
        _repair_interrupted_untracked_remote_bookmarks(
            client=client,
            remote=remote,
            state_dir=state_dir,
        )
    stack = client.discover_review_stack(revset)
    if on_prepared is not None:
        on_prepared(stack.selected_revset, remote, bool(stack.revisions))
    state = state_store.load()
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
    _preflight_private_commits(client, stack.revisions)
    (
        generated_pull_request_descriptions,
        generated_stack_description,
    ) = _resolve_generated_descriptions(
        describe_with=describe_with,
        repo_root=repo_root,
        selected_revset=stack.selected_revset,
        revisions=stack.revisions,
    )

    if not stack.revisions:
        if bookmark_result.changed and not dry_run:
            state_store.save(bookmark_result.state)
        trunk_branch = config.trunk_branch
        if trunk_branch is None:
            remote_bookmarks = _remote_bookmarks_pointing_at_trunk(
                client=client,
                remote_name=remote.name,
                trunk_commit_id=stack.trunk.commit_id,
            )
            if len(remote_bookmarks) == 1:
                trunk_branch = remote_bookmarks[0]
        if on_trunk_resolved is not None:
            on_trunk_resolved(stack.trunk.subject, trunk_branch or stack.trunk.subject, False)
        return SubmitResult(
            dry_run=dry_run,
            remote=remote,
            revisions=(),
            selected_revset=stack.selected_revset,
            trunk_branch=trunk_branch or stack.trunk.subject,
            trunk_subject=stack.trunk.subject,
        )

    github_repository = resolve_github_repository(config, remote)
    resolved_reviewers = config.reviewers if reviewers is None else reviewers
    resolved_team_reviewers = (
        config.team_reviewers if team_reviewers is None else team_reviewers
    )
    state_changes = dict(bookmark_result.state.changes)

    # Build the intent before any mutation, using info already in hand
    ordered_change_ids = tuple(r.change_id for r in stack.revisions)
    bookmarks_map = {
        r.change_id: res.bookmark
        for r, res in zip(stack.revisions, bookmark_result.resolutions, strict=True)
    }
    intent = SubmitIntent(
        kind="submit",
        pid=os.getpid(),
        label=f"submit on {stack.selected_revset}",
        display_revset=stack.selected_revset,
        head_change_id=(
            stack.revisions[-1].change_id if stack.revisions else stack.trunk.change_id
        ),
        ordered_change_ids=ordered_change_ids,
        bookmarks=bookmarks_map,
        bases={},
        started_at=datetime.now(UTC).isoformat(),
    )
    stale_intents = []
    intent_path: Path | None = None
    if dry_run:
        stale_intents = _list_stale_submit_intents_without_waiting(
            state_dir=state_dir,
            intent=intent,
        )
    else:
        if state_dir is None:
            raise AssertionError("Live submit requires a writable state directory.")
        stale_intents = check_same_kind_intent(state_dir, intent)

    for loaded in stale_intents:
        if not isinstance(loaded.intent, SubmitIntent):
            continue
        match = match_ordered_change_ids(loaded.intent.ordered_change_ids, ordered_change_ids)
        if match == "exact":
            print(f"Resuming interrupted {loaded.intent.label}")
        elif match == "superset":
            pass  # proceed silently, retire on success
        elif match == "overlap":
            print(
                f"Warning: this submit overlaps an incomplete earlier operation "
                f"({loaded.intent.label})"
            )
        else:
            print(f"Note: incomplete operation outstanding: {loaded.intent.label}")

    if not dry_run:
        if state_dir is None:
            raise AssertionError("Live submit requires a writable state directory.")
        intent_path = write_intent(state_dir, intent)

    succeeded = False
    submitted_revisions: tuple[SubmittedRevision, ...] = ()
    try:
        async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
            github_repository_state = await _get_github_repository(
                github_client,
                github_repository=github_repository,
            )
            trunk_branch = resolve_trunk_branch(
                client=client,
                config=config,
                github_repository_state=github_repository_state,
                remote=remote,
                stack=stack,
            )
            discovered_pull_requests = await _discover_pull_requests_by_bookmark(
                github_client=github_client,
                github_repository=github_repository,
                bookmarks=tuple(
                    resolution.bookmark for resolution in bookmark_result.resolutions
                ),
            )
            if on_trunk_resolved is not None:
                on_trunk_resolved(stack.trunk.subject, trunk_branch, True)

            prepared_revisions: list[PreparedSubmitRevision] = []
            for resolution, revision in zip(
                bookmark_result.resolutions,
                stack.revisions,
                strict=True,
            ):
                _ensure_change_is_not_detached(
                    cached_change=bookmark_result.state.changes.get(revision.change_id),
                    change_id=revision.change_id,
                )
                bookmark_state = client.get_bookmark_state(resolution.bookmark)
                local_action = _resolve_local_action(
                    resolution.bookmark,
                    bookmark_state.local_targets,
                    revision.commit_id,
                )
                remote_state = bookmark_state.remote_target(remote.name)
                _ensure_remote_can_be_updated(
                    bookmark=resolution.bookmark,
                    bookmark_source=resolution.source,
                    bookmark_state=bookmark_state,
                    change_id=revision.change_id,
                    desired_target=revision.commit_id,
                    remote=remote.name,
                    remote_state=remote_state,
                    state=bookmark_result.state,
                )

                if local_action != "unchanged" and not dry_run:
                    client.set_bookmark(resolution.bookmark, revision.commit_id)

                expected_remote_target: str | None = None
                if _remote_is_up_to_date(remote_state, revision.commit_id):
                    push_operation: PushOperation = "up_to_date"
                    remote_action: RemoteBookmarkAction = "up to date"
                elif _should_update_untracked_remote_with_git(remote_state, revision.commit_id):
                    if remote_state is None:
                        raise AssertionError("Checked remote bookmark state must exist.")
                    expected_remote_target = remote_state.target
                    if expected_remote_target is None:
                        raise AssertionError("Checked remote target must be unambiguous.")
                    push_operation = "git_update"
                    remote_action = "pushed"
                else:
                    push_operation = "batch"
                    remote_action = "pushed"

                prepared_revisions.append(
                    PreparedSubmitRevision(
                        bookmark=resolution.bookmark,
                        bookmark_source=resolution.source,
                        change_id=revision.change_id,
                        expected_remote_target=expected_remote_target,
                        local_action=local_action,
                        push_operation=push_operation,
                        remote_action=remote_action,
                        revision=revision,
                    )
                )

            _sync_remote_bookmarks(
                client=client,
                dry_run=dry_run,
                prepared_revisions=tuple(prepared_revisions),
                remote=remote,
            )
            submitted_revisions = await _sync_pull_requests(
                draft_mode=draft_mode,
                dry_run=dry_run,
                github_client=github_client,
                github_repository=github_repository,
                prepared_revisions=tuple(prepared_revisions),
                discovered_pull_requests=discovered_pull_requests,
                labels=config.labels,
                reviewers=resolved_reviewers,
                state=bookmark_result.state,
                state_changes=state_changes,
                state_store=state_store,
                team_reviewers=resolved_team_reviewers,
                trunk_branch=trunk_branch,
                generated_descriptions=generated_pull_request_descriptions,
            )

            await _sync_stack_comments(
                dry_run=dry_run,
                generated_stack_description=generated_stack_description,
                github_client=github_client,
                github_repository=github_repository,
                revisions=submitted_revisions,
                state=bookmark_result.state,
                state_changes=state_changes,
                state_store=state_store,
                trunk_branch=trunk_branch,
            )

        if not dry_run:
            next_state = bookmark_result.state.model_copy(update={"changes": state_changes})
            if bookmark_result.changed or next_state != state:
                state_store.save(next_state)

        succeeded = True
        return SubmitResult(
            dry_run=dry_run,
            remote=remote,
            revisions=submitted_revisions,
            selected_revset=stack.selected_revset,
            trunk_branch=trunk_branch,
            trunk_subject=stack.trunk.subject,
        )
    finally:
        if succeeded and intent_path is not None:
            retire_superseded_intents(stale_intents, intent)
            delete_intent(intent_path)


def _list_stale_submit_intents_without_waiting(
    *,
    state_dir: Path | None,
    intent: SubmitIntent,
) -> list[LoadedIntent]:
    if state_dir is None:
        return []
    return [
        loaded
        for loaded in scan_intents(state_dir)
        if loaded.intent.kind == intent.kind and not pid_is_alive(loaded.intent.pid)
    ]


def _repair_interrupted_untracked_remote_bookmarks(
    *,
    client: InterruptedRemoteBookmarkRepairer,
    remote: GitRemote,
    state_dir: Path | None,
) -> None:
    if state_dir is None:
        return
    stale_submit_intents = [
        loaded
        for loaded in scan_intents(state_dir)
        if loaded.intent.kind == "submit" and not pid_is_alive(loaded.intent.pid)
    ]
    if not stale_submit_intents:
        return

    bookmarks = tuple(
        sorted(
            {
                bookmark
                for loaded in stale_submit_intents
                if isinstance(loaded.intent, SubmitIntent)
                for bookmark in loaded.intent.bookmarks.values()
            }
        )
    )
    if not bookmarks:
        return

    client.fetch_remote(remote=remote.name)
    bookmark_states = client.list_bookmark_states(bookmarks)
    for bookmark in bookmarks:
        bookmark_state = bookmark_states.get(bookmark)
        if bookmark_state is None:
            continue
        remote_state = bookmark_state.remote_target(remote.name)
        if remote_state is None or remote_state.is_tracked:
            continue
        local_target = bookmark_state.local_target
        if local_target is None or remote_state.target != local_target:
            continue
        client.track_bookmark(remote=remote.name, bookmark=bookmark)


def select_submit_remote(
    config: RepoConfig,
    remotes: tuple[GitRemote, ...],
) -> GitRemote:
    """Resolve the Git remote used by `submit`."""

    remotes_by_name = {remote.name: remote for remote in remotes}
    if config.remote:
        remote = remotes_by_name.get(config.remote)
        if remote is None:
            raise SubmitRemoteResolutionError(
                f"Configured remote {config.remote!r} is not defined in this repository."
            )
        return remote
    if "origin" in remotes_by_name:
        return remotes_by_name["origin"]
    if len(remotes) == 1:
        return remotes[0]
    raise SubmitRemoteResolutionError(
        "Could not determine which Git remote to use for submit. Configure "
        "`repo.remote`, add an `origin` remote, or leave exactly one remote."
    )


def resolve_github_repository(
    config: RepoConfig,
    remote: GitRemote,
) -> ResolvedGithubRepository:
    """Resolve the GitHub repository target for the selected remote."""

    parsed_remote = _parse_remote_url(remote.url)
    host = config.github_host
    if host == _DEFAULT_GITHUB_HOST and parsed_remote is not None:
        host = parsed_remote.host
    owner = config.github_owner or (parsed_remote.owner if parsed_remote else None)
    repo = config.github_repo or (parsed_remote.repo if parsed_remote else None)
    if owner and repo:
        return ResolvedGithubRepository(host=host, owner=owner, repo=repo)
    raise SubmitGithubResolutionError(
        f"Could not determine the GitHub repository for remote {remote.name!r}. "
        "Configure `repo.github_owner` and `repo.github_repo`, or use a GitHub remote "
        "URL."
    )


def resolve_trunk_branch(
    *,
    client: BookmarkStateReader,
    config: RepoConfig,
    github_repository_state: GithubRepository,
    remote: GitRemote,
    stack: Any,
) -> str:
    """Resolve the GitHub base branch used for bottom-of-stack pull requests."""

    if config.trunk_branch:
        return config.trunk_branch
    if github_repository_state.default_branch:
        return github_repository_state.default_branch

    remote_bookmarks = _remote_bookmarks_pointing_at_trunk(
        client=client,
        remote_name=remote.name,
        trunk_commit_id=stack.trunk.commit_id,
    )
    if len(remote_bookmarks) == 1:
        return remote_bookmarks[0]
    if len(remote_bookmarks) > 1:
        raise SubmitGithubResolutionError(
            "Could not determine the trunk branch because multiple remote bookmarks on "
            f"{remote.name!r} point at `trunk()`: {', '.join(remote_bookmarks)}."
        )
    raise SubmitGithubResolutionError(
        f"Could not determine the trunk branch for remote {remote.name!r}. Configure "
        "`repo.trunk_branch`, ensure the GitHub repository exposes a default branch, or "
        "create one remote bookmark that points at `trunk()`."
    )


def _resolve_local_action(
    bookmark: str,
    local_targets: tuple[str, ...],
    desired_target: str,
) -> LocalBookmarkAction:
    if len(local_targets) > 1:
        raise SubmitBookmarkConflictError(
            f"Bookmark {bookmark!r} has {len(local_targets)} conflicting local targets. "
            "Resolve the bookmark conflict with `jj bookmark` before submitting."
        )
    local_target = local_targets[0] if local_targets else None
    if local_target == desired_target:
        return "unchanged"
    if local_target is None:
        return "created"
    return "moved"


def _remote_is_up_to_date(
    remote_state: RemoteBookmarkState | None,
    desired_target: str,
) -> bool:
    if remote_state is None:
        return False
    return remote_state.target == desired_target


def _ensure_remote_can_be_updated(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    bookmark_state: BookmarkState,
    change_id: str,
    desired_target: str,
    remote: str,
    remote_state: RemoteBookmarkState | None,
    state: ReviewState,
) -> None:
    if remote_state is None or not remote_state.targets:
        return
    if len(remote_state.targets) > 1:
        raise SubmitRemoteBookmarkConflictError(
            f"Remote bookmark {bookmark!r}@{remote} is conflicted. Resolve it with `jj "
            "git fetch` and retry."
        )
    if remote_state.target == desired_target:
        return
    if _bookmark_link_is_proven(
        bookmark=bookmark,
        bookmark_source=bookmark_source,
        bookmark_state=bookmark_state,
        change_id=change_id,
        state=state,
    ):
        return
    raise SubmitRemoteBookmarkOwnershipError(
        f"Remote bookmark {bookmark!r}@{remote} already exists and points elsewhere. "
        "Submit will not take over an existing remote branch unless its link is "
        "already proven by local state, cached state, or explicit relinking."
    )


def _bookmark_link_is_proven(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    bookmark_state: BookmarkState,
    change_id: str,
    state: ReviewState,
) -> bool:
    if bookmark_state.local_target is not None:
        return True
    if bookmark_source == "discovered":
        return True
    if bookmark_source != "cache":
        return False
    cached_change = state.changes.get(change_id)
    return (
        cached_change is not None
        and not cached_change.is_detached
        and cached_change.bookmark == bookmark
    )


def _should_update_untracked_remote_with_git(
    remote_state: RemoteBookmarkState | None,
    desired_target: str,
) -> bool:
    if remote_state is None or remote_state.is_tracked:
        return False
    if len(remote_state.targets) != 1:
        return False
    return remote_state.target != desired_target


def _preflight_private_commits(
    client: PrivateCommitFinder,
    revisions: tuple[Any, ...],
) -> None:
    private = client.find_private_commits(revisions)
    if not private:
        return
    subjects = ", ".join(f"{r.change_id[:8]} ({r.subject})" for r in private)
    raise SubmitPrivateCommitError(
        f"Stack contains commits blocked by `git.private-commits`: {subjects}. "
        "Remove these changes from the stack before submitting."
    )


def _ensure_unique_bookmarks(resolutions: tuple[ResolvedBookmark, ...]) -> None:
    bookmarks_to_changes: dict[str, list[str]] = defaultdict(list)
    for resolution in resolutions:
        bookmarks_to_changes[resolution.bookmark].append(resolution.change_id)

    duplicates = {
        bookmark: change_ids
        for bookmark, change_ids in bookmarks_to_changes.items()
        if len(change_ids) > 1
    }
    if not duplicates:
        return

    collision_descriptions = ", ".join(
        (
            f"{bookmark!r} for changes {', '.join(change_ids)}"
            for bookmark, change_ids in sorted(duplicates.items())
        )
    )
    raise SubmitBookmarkCollisionError(
        "Selected stack resolves multiple review units to the same bookmark: "
        f"{collision_descriptions}. Configure distinct bookmark names before "
        "submitting."
    )


async def _get_github_repository(
    github_client: GithubClient,
    *,
    github_repository: ResolvedGithubRepository,
) -> GithubRepository:
    try:
        return await github_client.get_repository(
            github_repository.owner,
            github_repository.repo,
        )
    except GithubClientError as error:
        raise SubmitGithubResolutionError(
            f"Could not load GitHub repository {github_repository.full_name}: {error}"
        ) from error


async def _discover_pull_requests_by_bookmark(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    bookmarks: tuple[str, ...],
) -> dict[str, GithubPullRequest | None]:
    if not bookmarks:
        return {}

    try:
        discovered_pull_requests = await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=bookmarks,
        )
    except GithubClientError as error:
        raise SubmitPullRequestResolutionError(
            "Could not batch pull request discovery for review branches: "
            f"{error}"
        ) from error

    return {
        bookmark: _select_discovered_pull_request(
            head_label=f"{github_repository.owner}:{bookmark}",
            pull_requests=discovered_pull_requests.get(bookmark, ()),
        )
        for bookmark in bookmarks
    }


def _sync_remote_bookmarks(
    *,
    client: RemoteBookmarkSyncer,
    dry_run: bool,
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote: GitRemote,
) -> None:
    batch_push_bookmarks = tuple(
        prepared_revision.bookmark
        for prepared_revision in prepared_revisions
        if prepared_revision.push_operation == "batch"
    )
    if batch_push_bookmarks:
        if not dry_run:
            client.push_bookmarks(
                remote=remote.name,
                bookmarks=batch_push_bookmarks,
            )

    for prepared_revision in prepared_revisions:
        if prepared_revision.push_operation != "git_update":
            continue
        if not dry_run:
            if prepared_revision.expected_remote_target is None:
                raise AssertionError("Git remote update requires an expected target.")
            client.update_untracked_remote_bookmark(
                remote=remote.name,
                bookmark=prepared_revision.bookmark,
                desired_target=prepared_revision.revision.commit_id,
                expected_remote_target=prepared_revision.expected_remote_target,
            )


def _save_submit_state_checkpoint(
    *,
    dry_run: bool,
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
) -> None:
    if dry_run:
        return
    interim_state = state.model_copy(update={"changes": dict(state_changes)})
    state_store.save(interim_state)


def _resolve_generated_descriptions(
    *,
    describe_with: str | None,
    repo_root: Path,
    revisions: tuple[Any, ...],
    selected_revset: str,
) -> tuple[dict[str, GeneratedDescription], GeneratedDescription | None]:
    if describe_with is None:
        return (
            {
                revision.change_id: GeneratedDescription(
                    body=_pull_request_body(revision.description),
                    title=revision.subject,
                )
                for revision in revisions
            },
            None,
        )

    generated_descriptions = {
        revision.change_id: _run_description_command(
            command=describe_with,
            kind="pr",
            repo_root=repo_root,
            revset=revision.change_id,
        )
        for revision in revisions
    }
    generated_stack_description = None
    if len(revisions) > 1:
        stack_input = _build_stack_description_input(
            generated_descriptions=generated_descriptions,
            repo_root=repo_root,
            revisions=revisions,
        )
        with tempfile.TemporaryDirectory(prefix="jj-review-describe-with-") as tempdir:
            stack_input_path = Path(tempdir) / "stack-input.json"
            stack_input_path.write_text(json.dumps(stack_input), encoding="utf-8")
            generated_stack_description = _run_description_command(
                command=describe_with,
                extra_env={
                    _DESCRIBE_WITH_STACK_INPUT_ENV: str(stack_input_path),
                },
                kind="stack",
                repo_root=repo_root,
                revset=selected_revset,
            )
    return generated_descriptions, generated_stack_description


def _build_stack_description_input(
    *,
    generated_descriptions: dict[str, GeneratedDescription],
    repo_root: Path,
    revisions: tuple[Any, ...],
) -> dict[str, Any]:
    return {
        "revisions": [
            {
                "body": generated_descriptions[revision.change_id].body,
                "change_id": revision.change_id,
                "diffstat": _describe_with_diffstat(
                    repo_root=repo_root,
                    revset=revision.change_id,
                ),
                "title": generated_descriptions[revision.change_id].title,
            }
            for revision in revisions
        ]
    }


def _describe_with_diffstat(*, repo_root: Path, revset: str) -> str:
    try:
        completed = subprocess.run(
            ["jj", "show", "--stat", "-r", revset],
            capture_output=True,
            check=False,
            cwd=repo_root,
            text=True,
        )
    except OSError as error:
        raise SubmitDescriptionCommandError(
            f"Could not collect diffstat for --stack {revset!r}: {error}"
        ) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or "unknown jj failure"
        raise SubmitDescriptionCommandError(
            f"Could not collect diffstat for --stack {revset!r}: {detail}"
        )

    lines = completed.stdout.rstrip().splitlines()
    diffstat_lines: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            if diffstat_lines:
                break
            continue
        diffstat_lines.append(line)
    return "\n".join(reversed(diffstat_lines))


def _run_description_command(
    *,
    command: str,
    extra_env: dict[str, str] | None = None,
    kind: Literal["pr", "stack"],
    repo_root: Path,
    revset: str,
) -> GeneratedDescription:
    try:
        completed = subprocess.run(
            [command, f"--{kind}", revset],
            capture_output=True,
            check=False,
            cwd=repo_root,
            env=(
                None
                if extra_env is None
                else {
                    **os.environ,
                    **extra_env,
                }
            ),
            text=True,
        )
    except FileNotFoundError as error:
        raise SubmitDescriptionCommandError(
            f"Describe helper {command!r} was not found."
        ) from error
    except OSError as error:
        raise SubmitDescriptionCommandError(
            f"Could not run describe helper {command!r}: {error}"
        ) from error

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if not detail:
            detail = f"exit status {completed.returncode}"
        raise SubmitDescriptionCommandError(
            f"Describe helper {command!r} failed for --{kind} {revset!r}: {detail}"
        )

    output = completed.stdout.strip()
    if not output:
        raise SubmitDescriptionCommandError(
            f"Describe helper {command!r} produced no JSON for --{kind} {revset!r}."
        )

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise SubmitDescriptionCommandError(
            f"Describe helper {command!r} returned invalid JSON for --{kind} "
            f"{revset!r}: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise SubmitDescriptionCommandError(
            f"Describe helper {command!r} must return a JSON object for --{kind} "
            f"{revset!r}."
        )

    title = payload.get("title")
    body = payload.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        raise SubmitDescriptionCommandError(
            f"Describe helper {command!r} must return string `title` and `body` "
            f"fields for --{kind} {revset!r}."
        )

    return GeneratedDescription(body=body, title=title)


async def _run_bounded_submit_tasks(
    *,
    concurrency: int,
    items: tuple[_TaskItemT, ...],
    run_item: Callable[[_TaskItemT], Coroutine[Any, Any, _TaskResultT]],
    on_success: Callable[[int, _TaskResultT], None],
) -> list[_TaskResultT]:
    if not items:
        return []

    item_iter = iter(enumerate(items))
    in_flight: dict[asyncio.Task[_TaskResultT], int] = {}
    results: list[_TaskResultT | None] = [None] * len(items)
    first_failure: tuple[int, Exception] | None = None

    def start_next() -> bool:
        try:
            index, item = next(item_iter)
        except StopIteration:
            return False
        in_flight[asyncio.create_task(run_item(item))] = index
        return True

    for _ in range(min(concurrency, len(items))):
        start_next()

    while in_flight:
        done, _ = await asyncio.wait(
            tuple(in_flight),
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            index = in_flight.pop(task)
            try:
                result = task.result()
            except Exception as error:
                if first_failure is None or index < first_failure[0]:
                    first_failure = (index, error)
                continue

            results[index] = result
            try:
                on_success(index, result)
            except Exception as error:
                if first_failure is None or index < first_failure[0]:
                    first_failure = (index, error)

        while first_failure is None and len(in_flight) < concurrency:
            if not start_next():
                break

    if first_failure is not None:
        raise first_failure[1]

    completed_results: list[_TaskResultT] = []
    for result in results:
        if result is None:
            raise AssertionError("Submit task runner completed without a task result.")
        completed_results.append(result)
    return completed_results


async def _sync_pull_requests(
    *,
    draft_mode: SubmitDraftMode,
    discovered_pull_requests: dict[str, GithubPullRequest | None],
    dry_run: bool,
    generated_descriptions: dict[str, GeneratedDescription],
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    labels: list[str],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    reviewers: list[str],
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
    team_reviewers: list[str],
    trunk_branch: str,
) -> tuple[SubmittedRevision, ...]:
    pending = tuple(
        PendingPullRequestSync(
            base_branch=prepared_revisions[index - 1].bookmark if index > 0 else trunk_branch,
            discovered_pull_request=discovered_pull_requests[prepared_revision.bookmark],
            generated_description=generated_descriptions[prepared_revision.change_id],
            prepared_revision=prepared_revision,
        )
        for index, prepared_revision in enumerate(prepared_revisions)
    )
    submitted_revisions = await _run_bounded_submit_tasks(
        concurrency=_GITHUB_INSPECTION_CONCURRENCY,
        items=pending,
        run_item=lambda pending_sync: _sync_pull_request_task(
            draft_mode=draft_mode,
            dry_run=dry_run,
            github_client=github_client,
            github_repository=github_repository,
            labels=labels,
            pending_sync=pending_sync,
            reviewers=reviewers,
            state=state,
            team_reviewers=team_reviewers,
        ),
        on_success=lambda _index, submitted: _record_pull_request_success(
            dry_run=dry_run,
            state=state,
            state_changes=state_changes,
            state_store=state_store,
            submitted=submitted,
        ),
    )
    return tuple(submitted.submitted_revision for submitted in submitted_revisions)


def _record_pull_request_success(
    *,
    dry_run: bool,
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
    submitted: SubmittedPullRequestSync,
) -> None:
    if submitted.cached_change is not None:
        state_changes[submitted.submitted_revision.change_id] = submitted.cached_change
    _save_submit_state_checkpoint(
        dry_run=dry_run,
        state=state,
        state_changes=state_changes,
        state_store=state_store,
    )


async def _sync_pull_request_task(
    *,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    labels: list[str],
    pending_sync: PendingPullRequestSync,
    reviewers: list[str],
    state: ReviewState,
    team_reviewers: list[str],
) -> SubmittedPullRequestSync:
    prepared_revision = pending_sync.prepared_revision
    pull_request_result = await _sync_pull_request(
        base_branch=pending_sync.base_branch,
        bookmark=prepared_revision.bookmark,
        change_id=prepared_revision.change_id,
        discovered_pull_request=pending_sync.discovered_pull_request,
        draft_mode=draft_mode,
        dry_run=dry_run,
        generated_description=pending_sync.generated_description,
        github_client=github_client,
        github_repository=github_repository,
        labels=labels,
        reviewers=reviewers,
        revision=prepared_revision.revision,
        state=state,
        team_reviewers=team_reviewers,
    )
    return SubmittedPullRequestSync(
        cached_change=pull_request_result.cached_change,
        submitted_revision=SubmittedRevision(
            bookmark=prepared_revision.bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            change_id=prepared_revision.change_id,
            local_action=prepared_revision.local_action,
            pull_request_action=pull_request_result.action,
            pull_request_is_draft=(
                pull_request_result.pull_request.is_draft
                if pull_request_result.pull_request is not None
                else None
            ),
            pull_request_number=(
                pull_request_result.pull_request.number
                if pull_request_result.pull_request is not None
                else None
            ),
            pull_request_url=(
                pull_request_result.pull_request.html_url
                if pull_request_result.pull_request is not None
                else None
            ),
            remote_action=prepared_revision.remote_action,
            subject=prepared_revision.revision.subject,
        ),
    )


async def _sync_pull_request(
    *,
    base_branch: str,
    bookmark: str,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    generated_description: GeneratedDescription,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    labels: list[str],
    reviewers: list[str],
    revision: Any,
    state: ReviewState,
    team_reviewers: list[str],
) -> PullRequestSyncResult:
    cached_change = state.changes.get(change_id)
    _ensure_pull_request_link_is_consistent(
        bookmark=bookmark,
        cached_change=cached_change,
        change_id=change_id,
        discovered_pull_request=discovered_pull_request,
    )

    title = generated_description.title
    body = generated_description.body
    if discovered_pull_request is None:
        pull_request = None
        if not dry_run:
            pull_request = await _create_pull_request(
                base_branch=base_branch,
                body=body,
                draft=(draft_mode in ("draft", "draft_all")),
                github_client=github_client,
                github_repository=github_repository,
                head_branch=bookmark,
                title=title,
            )
        action: PullRequestAction = "created"
    elif _pull_request_matches(
        base_branch=base_branch,
        body=body,
        pull_request=discovered_pull_request,
        title=title,
    ):
        pull_request = discovered_pull_request
        action = "unchanged"
    else:
        pull_request = discovered_pull_request
        if not dry_run:
            pull_request = await _update_pull_request(
                base_branch=base_branch,
                body=body,
                github_client=github_client,
                github_repository=github_repository,
                pull_request=discovered_pull_request,
                title=title,
            )
        action = "updated"

    if (
        pull_request is not None
        and pull_request.state == "open"
    ):
        if draft_mode == "publish" and pull_request.is_draft:
            if not dry_run:
                pull_request = await _mark_pull_request_ready_for_review(
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request=pull_request,
                )
            action = "updated"
        elif draft_mode == "draft_all" and not pull_request.is_draft:
            if not dry_run:
                pull_request = await _convert_pull_request_to_draft(
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request=pull_request,
                )
            action = "updated"

    if (
        not dry_run
        and pull_request is not None
        and _should_sync_pull_request_metadata(
            action=action,
            cached_change=cached_change,
        )
    ):
        await _sync_pull_request_metadata(
            github_client=github_client,
            github_repository=github_repository,
            labels=labels,
            pull_request_number=pull_request.number,
            reviewers=reviewers,
            team_reviewers=team_reviewers,
        )

    next_cached_change: CachedChange | None = None
    if pull_request is not None:
        next_cached_change = _updated_cached_change(
            bookmark=bookmark,
            cached_change=cached_change,
            commit_id=revision.commit_id,
            pull_request=pull_request,
        )
    return PullRequestSyncResult(
        action=action,
        cached_change=next_cached_change,
        pull_request=pull_request,
    )


def _should_sync_pull_request_metadata(
    *,
    action: PullRequestAction,
    cached_change: CachedChange | None,
) -> bool:
    if action != "unchanged":
        return True
    if cached_change is None:
        return True
    return cached_change.pr_number is None and cached_change.pr_url is None
def _select_discovered_pull_request(
    *,
    head_label: str,
    pull_requests: tuple[GithubPullRequest, ...],
) -> GithubPullRequest | None:
    if len(pull_requests) > 1:
        raise SubmitPullRequestResolutionError(
            f"GitHub reports multiple pull requests for head branch {head_label!r}. "
            "Inspect the PR link with `status --fetch` and repair it with `relink` "
            "before submitting again."
        )
    if not pull_requests:
        return None
    pull_request = pull_requests[0]
    if pull_request.state != "open":
        raise SubmitPullRequestResolutionError(
            f"GitHub reports pull request #{pull_request.number} for head branch "
            f"{head_label!r} in state {pull_request.state!r}. Inspect the PR link with "
            "`status --fetch` and repair it with `relink` before submitting again."
        )
    return pull_request


def _ensure_pull_request_link_is_consistent(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
) -> None:
    _ensure_change_is_not_detached(
        cached_change=cached_change,
        change_id=change_id,
    )
    if cached_change is None or (
        cached_change.pr_number is None and cached_change.pr_url is None
    ):
        return
    if discovered_pull_request is None:
        raise SubmitPullRequestResolutionError(
            f"Cached pull request link exists for bookmark {bookmark!r}, but GitHub "
            "no longer reports a PR for that head branch. Inspect the PR link with "
            "`status --fetch` and repair it with `relink` before submitting again."
        )
    if cached_change.pr_number not in (None, discovered_pull_request.number):
        raise SubmitPullRequestResolutionError(
            f"Cached pull request #{cached_change.pr_number} does not match the PR "
            f"GitHub reports for bookmark {bookmark!r} "
            f"(#{discovered_pull_request.number}). Inspect the PR link with "
            "`status --fetch` and repair it with `relink` before submitting again."
        )
    if cached_change.pr_url not in (None, discovered_pull_request.html_url):
        raise SubmitPullRequestResolutionError(
            f"Cached pull request URL for bookmark {bookmark!r} does not match "
            "GitHub. Inspect the PR link with `status --fetch` and repair it with "
            "`relink` before submitting again."
        )


def _ensure_change_is_not_detached(
    *,
    cached_change: CachedChange | None,
    change_id: str,
) -> None:
    if cached_change is None or not cached_change.is_detached:
        return
    raise SubmitDetachedChangeError(
        f"Change {change_id[:8]} is detached from managed review. Run `relink` to "
        "reattach it before submitting again."
    )


async def _create_pull_request(
    *,
    base_branch: str,
    body: str,
    draft: bool,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    head_branch: str,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.create_pull_request(
            github_repository.owner,
            github_repository.repo,
            base=base_branch,
            body=body,
            draft=draft,
            head=head_branch,
            title=title,
        )
    except GithubClientError as error:
        raise SubmitPullRequestResolutionError(
            f"Could not create a pull request for branch {head_branch!r}: {error}"
        ) from error


async def _sync_pull_request_metadata(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    labels: list[str],
    pull_request_number: int,
    reviewers: list[str],
    team_reviewers: list[str],
) -> None:
    try:
        if reviewers or team_reviewers:
            await github_client.request_reviewers(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request_number,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        if labels:
            await github_client.add_labels(
                github_repository.owner,
                github_repository.repo,
                issue_number=pull_request_number,
                labels=labels,
            )
    except GithubClientError as error:
        raise SubmitPullRequestResolutionError(
            f"Could not synchronize metadata for pull request #{pull_request_number}: "
            f"{error}"
        ) from error


async def _mark_pull_request_ready_for_review(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise SubmitPullRequestResolutionError(
            f"Could not publish draft pull request #{pull_request.number} for "
            f"{github_repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.mark_pull_request_ready_for_review(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise SubmitPullRequestResolutionError(
            f"Could not publish draft pull request #{pull_request.number} for "
            f"{github_repository.full_name}: {error}"
        ) from error


async def _convert_pull_request_to_draft(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise SubmitPullRequestResolutionError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.convert_pull_request_to_draft(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise SubmitPullRequestResolutionError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_repository.full_name}: {error}"
        ) from error


async def _update_pull_request(
    *,
    base_branch: str,
    body: str,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_request: GithubPullRequest,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.update_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request.number,
            base=base_branch,
            body=body,
            title=title,
        )
    except GithubClientError as error:
        raise SubmitPullRequestResolutionError(
            f"Could not update pull request #{pull_request.number}: {error}"
        ) from error


async def _sync_stack_comments(
    *,
    dry_run: bool,
    generated_stack_description: GeneratedDescription | None,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    revisions: tuple[SubmittedRevision, ...],
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
    trunk_branch: str,
) -> None:
    if len(revisions) <= 1:
        return

    pending: list[PendingStackCommentSync] = []
    for index, revision in enumerate(revisions):
        if revision.pull_request_number is None:
            continue
        cached_change = state_changes.get(revision.change_id) or state.changes.get(
            revision.change_id
        )
        if cached_change is None:
            if dry_run:
                continue
            raise AssertionError("Stack comments require cached pull request link.")
        comment_body = _render_stack_comment(
            current=revision,
            next_revision=revisions[index + 1] if index + 1 < len(revisions) else None,
            previous=revisions[index - 1] if index > 0 else None,
            stack_description=generated_stack_description,
            trunk_branch=trunk_branch,
        )
        pending.append(
            PendingStackCommentSync(
                cached_change=cached_change,
                change_id=revision.change_id,
                comment_body=comment_body,
                pull_request_number=revision.pull_request_number,
            )
        )
    if not pending:
        return
    await _run_bounded_submit_tasks(
        concurrency=_GITHUB_INSPECTION_CONCURRENCY,
        items=tuple(pending),
        run_item=lambda pending_sync: _sync_stack_comment_task(
            dry_run=dry_run,
            github_client=github_client,
            github_repository=github_repository,
            pending_sync=pending_sync,
        ),
        on_success=lambda _index, result: _record_stack_comment_success(
            dry_run=dry_run,
            result=result,
            state=state,
            state_changes=state_changes,
            state_store=state_store,
        ),
    )


def _record_stack_comment_success(
    *,
    dry_run: bool,
    result: tuple[str, CachedChange, GithubIssueComment | None],
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
) -> None:
    change_id, cached_change, comment = result
    if comment is not None:
        updated_change = cached_change.model_copy(update={"stack_comment_id": comment.id})
        if state_changes.get(change_id) != updated_change:
            state_changes[change_id] = updated_change
            _save_submit_state_checkpoint(
                dry_run=dry_run,
                state=state,
                state_changes=state_changes,
                state_store=state_store,
            )


async def _sync_stack_comment_task(
    *,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pending_sync: PendingStackCommentSync,
) -> tuple[str, CachedChange, GithubIssueComment | None]:
    comment = await _upsert_stack_comment(
        cached_change=pending_sync.cached_change,
        comment_body=pending_sync.comment_body,
        dry_run=dry_run,
        github_client=github_client,
        github_repository=github_repository,
        pull_request_number=pending_sync.pull_request_number,
    )
    return pending_sync.change_id, pending_sync.cached_change, comment


async def _upsert_stack_comment(
    *,
    cached_change: CachedChange,
    comment_body: str,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_request_number: int,
) -> GithubIssueComment | None:
    comments = await _list_issue_comments(
        github_client=github_client,
        github_repository=github_repository,
        pull_request_number=pull_request_number,
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
                raise SubmitStackCommentError(
                    f"Cached stack comment #{cached_change.stack_comment_id} for pull "
                    f"request #{pull_request_number} is not managed by `jj-review`. "
                    "Inspect the PR link with `status --fetch` or delete the cached "
                    "comment ID before submitting again."
                )
            if cached_comment.body == comment_body:
                return cached_comment
            if dry_run:
                return cached_comment
            return await _update_stack_comment(
                comment_body=comment_body,
                comment_id=cached_change.stack_comment_id,
                github_client=github_client,
                github_repository=github_repository,
            )

    discovered_comment = await _discover_stack_comment(
        comments=comments,
    )
    if discovered_comment is None:
        if dry_run:
            return None
        return await _create_stack_comment(
            comment_body=comment_body,
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pull_request_number,
        )
    if discovered_comment.body == comment_body:
        return discovered_comment
    if dry_run:
        return discovered_comment
    return await _update_stack_comment(
        comment_body=comment_body,
        comment_id=discovered_comment.id,
        github_client=github_client,
        github_repository=github_repository,
    )


async def _list_issue_comments(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_request_number: int,
) -> tuple[GithubIssueComment, ...]:
    try:
        return await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise SubmitStackCommentError(
            f"Could not list stack comments for pull request #{pull_request_number}: {error}"
        ) from error


async def _discover_stack_comment(
    *,
    comments: tuple[GithubIssueComment, ...],
) -> GithubIssueComment | None:
    matching_comments = [
        comment for comment in comments if _STACK_COMMENT_MARKER in comment.body
    ]
    if not matching_comments:
        return None
    if len(matching_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in matching_comments)
        raise SubmitStackCommentError(
            "GitHub reports multiple `jj-review` stack comments for the same pull "
            f"request: {comment_ids}. Inspect the PR link with `status --fetch` or "
            "delete the extra stack comments before submitting again."
        )
    return matching_comments[0]


async def _create_stack_comment(
    *,
    comment_body: str,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_request_number: int,
) -> GithubIssueComment:
    try:
        return await github_client.create_issue_comment(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
            body=comment_body,
        )
    except GithubClientError as error:
        raise SubmitStackCommentError(
            f"Could not create a stack comment for pull request #{pull_request_number}: "
            f"{error}"
        ) from error


async def _update_stack_comment(
    *,
    comment_body: str,
    comment_id: int,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
) -> GithubIssueComment:
    try:
        return await github_client.update_issue_comment(
            github_repository.owner,
            github_repository.repo,
            comment_id=comment_id,
            body=comment_body,
        )
    except GithubClientError as error:
        raise SubmitStackCommentError(
            f"Could not update stack comment #{comment_id}: {error}"
        ) from error


def _render_stack_comment(
    *,
    current: SubmittedRevision,
    next_revision: SubmittedRevision | None,
    previous: SubmittedRevision | None,
    stack_description: GeneratedDescription | None,
    trunk_branch: str,
) -> str:
    lines = [_STACK_COMMENT_MARKER]
    description_lines = _render_generated_stack_description(stack_description)
    if description_lines:
        lines.extend(description_lines)
        lines.extend(("", "---"))
    lines.extend(
        [
            "This pull request is part of a stack managed by `jj-review`.",
            "",
            f"Previous: {_render_stack_neighbor(previous, fallback=f'trunk `{trunk_branch}`')}",
            f"Current: {_render_pull_request_reference(current)}",
            f"Next: {_render_stack_neighbor(next_revision, fallback='none')}",
        ]
    )
    return "\n".join(lines)


def _render_generated_stack_description(
    stack_description: GeneratedDescription | None,
) -> list[str]:
    if stack_description is None:
        return []

    lines: list[str] = []
    if stack_description.title:
        lines.append(f"## {stack_description.title}")
    if stack_description.body:
        if lines:
            lines.append("")
        lines.extend(stack_description.body.splitlines())
    return lines


def _render_stack_neighbor(
    revision: SubmittedRevision | None,
    *,
    fallback: str,
) -> str:
    if revision is None:
        return fallback
    return _render_pull_request_reference(revision)


def _render_pull_request_reference(revision: SubmittedRevision) -> str:
    return f"[#{revision.pull_request_number}]({revision.pull_request_url}) {revision.subject}"


def _pull_request_body(description: str) -> str:
    lines = description.splitlines()
    if len(lines) < 2:
        return ""
    return "\n".join(lines[1:]).strip()


def _pull_request_matches(
    *,
    base_branch: str,
    body: str,
    pull_request: GithubPullRequest,
    title: str,
) -> bool:
    return (
        pull_request.base.ref == base_branch
        and (pull_request.body or "") == body
        and pull_request.title == title
    )


def _updated_cached_change(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    commit_id: str,
    pull_request: GithubPullRequest,
) -> CachedChange:
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            last_submitted_commit_id=commit_id,
            pr_is_draft=pull_request.is_draft,
            pr_number=pull_request.number,
            pr_state=pull_request.state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "last_submitted_commit_id": commit_id,
            "pr_is_draft": pull_request.is_draft,
            "pr_number": pull_request.number,
            "pr_state": pull_request.state,
            "pr_url": pull_request.html_url,
        }
    )


def _build_github_client(*, base_url: str) -> GithubClient:
    return GithubClient(
        base_url=base_url,
        token=_github_token_for_base_url(base_url),
    )


def _github_token_from_env() -> str | None:
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        return token
    token = os.environ.get("GH_TOKEN")
    if token:
        return token
    return None


def _github_token_for_base_url(base_url: str) -> str | None:
    token = _github_token_from_env()
    if token is not None:
        return token
    hostname = _github_hostname_from_api_base_url(base_url)
    if hostname is None:
        return None
    return _github_token_from_gh_cli(hostname)


def _github_hostname_from_api_base_url(base_url: str) -> str | None:
    hostname = urlparse(base_url).hostname
    if hostname is None:
        return None
    if hostname == "api.github.com":
        return "github.com"
    if hostname.startswith("api."):
        return hostname[4:]
    return hostname


def _github_token_from_gh_cli(hostname: str) -> str | None:
    try:
        completed = subprocess.run(
            ["gh", "auth", "token", "--hostname", hostname],
            capture_output=True,
            check=False,
            text=True,
        )
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    token = completed.stdout.strip()
    if not token:
        return None
    return token


def _discover_bookmarks_for_revisions(
    *,
    bookmark_states: dict[str, BookmarkState],
    remote_name: str,
    revisions: tuple[Any, ...],
) -> dict[str, str]:
    discovered: dict[str, str] = {}
    for revision in revisions:
        candidates = [
            bookmark
            for bookmark, bookmark_state in bookmark_states.items()
            if _bookmark_matches_generated_change_id(bookmark, revision.change_id)
            and _bookmark_state_is_discoverable(bookmark_state, remote_name)
        ]
        if not candidates:
            continue
        unique_candidates = sorted(set(candidates))
        if len(unique_candidates) > 1:
            raise SubmitBookmarkResolutionError(
                f"Could not safely rediscover the review bookmark for change "
                f"{revision.change_id}: multiple existing bookmarks match its stable "
                f"change-ID suffix: {', '.join(unique_candidates)}."
            )
        discovered[revision.change_id] = unique_candidates[0]
    return discovered


def _bookmark_matches_generated_change_id(bookmark: str, change_id: str) -> bool:
    return bookmark.startswith("review/") and bookmark.endswith(f"-{change_id[:8]}")


def _bookmark_state_is_discoverable(bookmark_state: BookmarkState, remote_name: str) -> bool:
    if bookmark_state.local_targets:
        return True
    remote_state = bookmark_state.remote_target(remote_name)
    return remote_state is not None and bool(remote_state.targets)


def _remote_bookmarks_pointing_at_trunk(
    *,
    client: BookmarkStateReader,
    remote_name: str,
    trunk_commit_id: str,
) -> tuple[str, ...]:
    states = client.list_bookmark_states()
    matches = [
        name
        for name, bookmark_state in states.items()
        if (remote_state := bookmark_state.remote_target(remote_name)) is not None
        and remote_state.target == trunk_commit_id
    ]
    return tuple(sorted(matches))


def _parse_remote_url(url: str) -> ParsedRemoteUrl | None:
    parsed = urlparse(url)
    if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname:
        return _build_parsed_remote_url(parsed.hostname, parsed.path)
    if parsed.scheme == "" and ":" in url and "@" in url.partition(":")[0]:
        host, _, path = url.partition(":")
        return _build_parsed_remote_url(host.rsplit("@", maxsplit=1)[-1], path)
    return None


def _build_parsed_remote_url(host: str, raw_path: str) -> ParsedRemoteUrl | None:
    normalized_path = raw_path.lstrip("/").removesuffix(".git")
    parts = [part for part in normalized_path.split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    return ParsedRemoteUrl(host=host, owner=owner, repo=repo)
