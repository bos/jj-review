"""Submit command support for remote bookmark and pull request projection."""

from __future__ import annotations

import asyncio
import os
import subprocess
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import urlparse

from jj_review.bookmarks import BookmarkResolver, BookmarkSource, ResolvedBookmark
from jj_review.cache import ReviewStateStore
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubIssueComment, GithubPullRequest, GithubRepository


class SubmitRemoteResolutionError(CliError):
    """Raised when `submit` cannot resolve which Git remote to use."""


class SubmitBookmarkCollisionError(CliError):
    """Raised when multiple review units resolve to the same bookmark."""


class SubmitBookmarkConflictError(CliError):
    """Raised when a local bookmark has multiple conflicting targets."""


class SubmitBookmarkResolutionError(CliError):
    """Raised when `submit` cannot safely rediscover review bookmark linkage."""


class SubmitRemoteBookmarkConflictError(CliError):
    """Raised when the selected remote bookmark is conflicted."""


class SubmitRemoteBookmarkOwnershipError(CliError):
    """Raised when `submit` cannot prove an existing remote branch belongs to it."""


class SubmitGithubResolutionError(CliError):
    """Raised when `submit` cannot resolve GitHub repository information."""


class SubmitPullRequestResolutionError(CliError):
    """Raised when `submit` cannot safely resolve a pull request."""


class SubmitStackCommentError(CliError):
    """Raised when `submit` cannot create or update stack metadata comments."""


LocalBookmarkAction = Literal["created", "moved", "unchanged"]
PullRequestAction = Literal["created", "unchanged", "updated"]
RemoteBookmarkAction = Literal["pushed", "up to date"]
_DEFAULT_GITHUB_HOST = "github.com"
_STACK_COMMENT_MARKER = "<!-- jj-review-stack -->"


@dataclass(frozen=True, slots=True)
class SubmittedRevision:
    """Remote and GitHub projection result for one revision in the submitted stack."""

    bookmark: str
    bookmark_source: BookmarkSource
    change_id: str
    local_action: LocalBookmarkAction
    pull_request_action: PullRequestAction
    pull_request_number: int
    pull_request_url: str
    remote_action: RemoteBookmarkAction
    subject: str


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Projected remote bookmark and pull request state for the selected stack."""

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
    pull_request: GithubPullRequest


class BookmarkStateReader(Protocol):
    """Subset of the jj client interface needed for trunk-branch fallback."""

    def list_bookmark_states(self) -> dict[str, BookmarkState]:
        """Return bookmark state keyed by bookmark name."""


def run_submit(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> SubmitResult:
    """Project the selected local stack to synthetic review bookmarks and PRs."""

    return asyncio.run(
        _run_submit_async(
            change_overrides=change_overrides,
            config=config,
            repo_root=repo_root,
            revset=revset,
        )
    )


async def _run_submit_async(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> SubmitResult:
    client = JjClient(repo_root)
    stack = client.discover_review_stack(revset)
    remotes = client.list_git_remotes()
    remote = select_submit_remote(config, remotes)
    state_store = ReviewStateStore.for_repo(repo_root)
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

    if not stack.revisions:
        if bookmark_result.changed:
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
        return SubmitResult(
            remote=remote,
            revisions=(),
            selected_revset=stack.selected_revset,
            trunk_branch=trunk_branch or stack.trunk.subject,
            trunk_subject=stack.trunk.subject,
        )

    github_repository = resolve_github_repository(config, remote)
    state_changes = dict(bookmark_result.state.changes)

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

        revisions: list[SubmittedRevision] = []
        for index, (resolution, revision) in enumerate(
            zip(
                bookmark_result.resolutions,
                stack.revisions,
                strict=True,
            )
        ):
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

            if local_action != "unchanged":
                client.set_bookmark(resolution.bookmark, revision.commit_id)

            if _remote_is_up_to_date(remote_state, revision.commit_id):
                remote_action = "up to date"
            else:
                if _should_update_untracked_remote_with_git(remote_state, revision.commit_id):
                    if remote_state is None:
                        raise AssertionError("Checked remote bookmark state must exist.")
                    expected_remote_target = remote_state.target
                    if expected_remote_target is None:
                        raise AssertionError("Checked remote target must be unambiguous.")
                    client.update_untracked_remote_bookmark(
                        remote=remote.name,
                        bookmark=resolution.bookmark,
                        desired_target=revision.commit_id,
                        expected_remote_target=expected_remote_target,
                    )
                else:
                    client.push_bookmark(remote=remote.name, bookmark=resolution.bookmark)
                remote_action = "pushed"

            base_branch = revisions[index - 1].bookmark if index > 0 else trunk_branch
            pull_request_result = await _sync_pull_request(
                base_branch=base_branch,
                bookmark=resolution.bookmark,
                change_id=revision.change_id,
                github_client=github_client,
                github_repository=github_repository,
                revision=revision,
                state=bookmark_result.state,
                state_changes=state_changes,
            )

            revisions.append(
                SubmittedRevision(
                    bookmark=resolution.bookmark,
                    bookmark_source=resolution.source,
                    change_id=revision.change_id,
                    local_action=local_action,
                    pull_request_action=pull_request_result.action,
                    pull_request_number=pull_request_result.pull_request.number,
                    pull_request_url=pull_request_result.pull_request.html_url,
                    remote_action=remote_action,
                    subject=revision.subject,
                )
            )

        await _sync_stack_comments(
            github_client=github_client,
            github_repository=github_repository,
            revisions=tuple(revisions),
            state=state,
            state_changes=state_changes,
            trunk_branch=trunk_branch,
        )

    next_state = bookmark_result.state.model_copy(update={"changes": state_changes})
    if bookmark_result.changed or next_state != state:
        state_store.save(next_state)

    return SubmitResult(
        remote=remote,
        revisions=tuple(revisions),
        selected_revset=stack.selected_revset,
        trunk_branch=trunk_branch,
        trunk_subject=stack.trunk.subject,
    )


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
    if _bookmark_linkage_is_proven(
        bookmark=bookmark,
        bookmark_source=bookmark_source,
        bookmark_state=bookmark_state,
        change_id=change_id,
        state=state,
    ):
        return
    raise SubmitRemoteBookmarkOwnershipError(
        f"Remote bookmark {bookmark!r}@{remote} already exists and points elsewhere. "
        "Submit will not take over an existing remote branch unless its linkage is "
        "already proven by local state, cached state, or explicit adoption."
    )


def _bookmark_linkage_is_proven(
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
    return cached_change is not None and cached_change.bookmark == bookmark


def _should_update_untracked_remote_with_git(
    remote_state: RemoteBookmarkState | None,
    desired_target: str,
) -> bool:
    if remote_state is None or remote_state.is_tracked:
        return False
    if len(remote_state.targets) != 1:
        return False
    return remote_state.target != desired_target


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


async def _sync_pull_request(
    *,
    base_branch: str,
    bookmark: str,
    change_id: str,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    revision: Any,
    state: ReviewState,
    state_changes: dict[str, CachedChange],
) -> PullRequestSyncResult:
    head_label = f"{github_repository.owner}:{bookmark}"
    discovered_pull_request = await _discover_pull_request(
        github_client=github_client,
        github_repository=github_repository,
        head_label=head_label,
    )
    cached_change = state.changes.get(change_id)
    _ensure_pull_request_linkage_is_consistent(
        bookmark=bookmark,
        cached_change=cached_change,
        discovered_pull_request=discovered_pull_request,
    )

    title = revision.subject
    body = _pull_request_body(revision.description)
    if discovered_pull_request is None:
        pull_request = await _create_pull_request(
            base_branch=base_branch,
            body=body,
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
        pull_request = await _update_pull_request(
            base_branch=base_branch,
            body=body,
            github_client=github_client,
            github_repository=github_repository,
            pull_request=discovered_pull_request,
            title=title,
        )
        action = "updated"

    state_changes[change_id] = _updated_cached_change(
        bookmark=bookmark,
        cached_change=cached_change,
        pull_request=pull_request,
    )
    return PullRequestSyncResult(action=action, pull_request=pull_request)


async def _discover_pull_request(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    head_label: str,
) -> GithubPullRequest | None:
    try:
        pull_requests = await github_client.list_pull_requests(
            github_repository.owner,
            github_repository.repo,
            head=head_label,
        )
    except GithubClientError as error:
        raise SubmitPullRequestResolutionError(
            f"Could not list pull requests for head {head_label!r}: {error}"
        ) from error

    if len(pull_requests) > 1:
        raise SubmitPullRequestResolutionError(
            f"GitHub reports multiple pull requests for head branch {head_label!r}. "
            "Repair the linkage with `sync` or `adopt` before submitting again."
        )
    if not pull_requests:
        return None
    pull_request = pull_requests[0]
    if pull_request.state != "open":
        raise SubmitPullRequestResolutionError(
            f"GitHub reports pull request #{pull_request.number} for head branch "
            f"{head_label!r} in state {pull_request.state!r}. Repair the linkage with "
            "`sync` or `adopt` before submitting again."
        )
    return pull_request


def _ensure_pull_request_linkage_is_consistent(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    discovered_pull_request: GithubPullRequest | None,
) -> None:
    if cached_change is None or (
        cached_change.pr_number is None and cached_change.pr_url is None
    ):
        return
    if discovered_pull_request is None:
        raise SubmitPullRequestResolutionError(
            f"Cached pull request linkage exists for bookmark {bookmark!r}, but GitHub "
            "no longer reports a PR for that head branch. Repair the linkage with "
            "`sync` or `adopt` before submitting again."
        )
    if cached_change.pr_number not in (None, discovered_pull_request.number):
        raise SubmitPullRequestResolutionError(
            f"Cached pull request #{cached_change.pr_number} does not match the PR "
            f"GitHub reports for bookmark {bookmark!r} "
            f"(#{discovered_pull_request.number}). Repair the linkage with `sync` or "
            "`adopt` before submitting again."
        )
    if cached_change.pr_url not in (None, discovered_pull_request.html_url):
        raise SubmitPullRequestResolutionError(
            f"Cached pull request URL for bookmark {bookmark!r} does not match "
            "GitHub. Repair the linkage with `sync` or `adopt` before submitting "
            "again."
        )


async def _create_pull_request(
    *,
    base_branch: str,
    body: str,
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
            head=head_branch,
            title=title,
        )
    except GithubClientError as error:
        raise SubmitPullRequestResolutionError(
            f"Could not create a pull request for branch {head_branch!r}: {error}"
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
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    revisions: tuple[SubmittedRevision, ...],
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    trunk_branch: str,
) -> None:
    for index, revision in enumerate(revisions):
        cached_change = state_changes.get(revision.change_id) or state.changes.get(
            revision.change_id
        )
        if cached_change is None:
            raise AssertionError("Stack comments require cached pull request linkage.")
        comment_body = _render_stack_comment(
            current=revision,
            next_revision=revisions[index + 1] if index + 1 < len(revisions) else None,
            previous=revisions[index - 1] if index > 0 else None,
            trunk_branch=trunk_branch,
        )
        comment = await _upsert_stack_comment(
            cached_change=cached_change,
            comment_body=comment_body,
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=revision.pull_request_number,
        )
        state_changes[revision.change_id] = cached_change.model_copy(
            update={"stack_comment_id": comment.id}
        )


async def _upsert_stack_comment(
    *,
    cached_change: CachedChange,
    comment_body: str,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_request_number: int,
) -> GithubIssueComment:
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
                    "Repair the linkage with `sync` or delete the cached comment ID "
                    "before submitting again."
                )
            if cached_comment.body == comment_body:
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
        return await _create_stack_comment(
            comment_body=comment_body,
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pull_request_number,
        )
    if discovered_comment.body == comment_body:
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
            f"request: {comment_ids}. Repair the linkage with `sync` or delete the extra "
            "stack comments before submitting again."
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
    trunk_branch: str,
) -> str:
    return "\n".join(
        [
            _STACK_COMMENT_MARKER,
            "This pull request is part of a stack managed by `jj-review`.",
            "",
            f"Previous: {_render_stack_neighbor(previous, fallback=f'trunk `{trunk_branch}`')}",
            f"Current: {_render_pull_request_reference(current)}",
            f"Next: {_render_stack_neighbor(next_revision, fallback='none')}",
        ]
    )


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
    pull_request: GithubPullRequest,
) -> CachedChange:
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            pr_number=pull_request.number,
            pr_state=pull_request.state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
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
