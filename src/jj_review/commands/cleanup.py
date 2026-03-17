"""Conservative cleanup of stale local and remote review state."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jj_review.cache import ReviewStateStore
from jj_review.commands.submit import (
    _STACK_COMMENT_MARKER,
    _build_github_client,
    resolve_github_repository,
    select_submit_remote,
)
from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.jj import JjClient
from jj_review.jj.client import UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.github import GithubIssueComment, GithubPullRequest

CleanupActionStatus = Literal["applied", "blocked", "planned"]


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


def run_cleanup(
    *,
    apply: bool,
    config: RepoConfig,
    repo_root: Path,
) -> CleanupResult:
    """Plan or apply conservative sparse-state cleanup actions."""

    return asyncio.run(
        _run_cleanup_async(
            apply=apply,
            config=config,
            repo_root=repo_root,
        )
    )


async def _run_cleanup_async(
    *,
    apply: bool,
    config: RepoConfig,
    repo_root: Path,
) -> CleanupResult:
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

    next_changes = dict(state.changes)
    actions: list[CleanupAction] = []

    if github_repository is None:
        for change_id, cached_change in state.changes.items():
            stale_reason = _stale_change_reason(
                change_id=change_id,
                jj_client=jj_client,
            )
            if stale_reason is None:
                continue
            actions.append(
                _cache_action(
                    change_id=change_id,
                    reason=stale_reason,
                    status="applied" if apply else "planned",
                )
            )
            if apply:
                next_changes.pop(change_id, None)

            remote_action = _plan_remote_branch_cleanup(
                bookmark_state=bookmark_states.get(
                    cached_change.bookmark or "",
                    BookmarkState(name=cached_change.bookmark or ""),
                ),
                cached_change=cached_change,
                remote=remote,
            )
            if remote_action is not None:
                actions.append(remote_action)
                if apply and remote_action.status == "planned" and remote is not None:
                    jj_client.delete_remote_bookmark(
                        remote=remote.name,
                        bookmark=cached_change.bookmark or "",
                    )
                    actions[-1] = CleanupAction(
                        kind=remote_action.kind,
                        message=remote_action.message,
                        status="applied",
                    )

        if apply and next_changes != state.changes:
            state_store.save(state.model_copy(update={"changes": next_changes}))

        return CleanupResult(
            actions=tuple(actions),
            applied=apply,
            github_error=github_error,
            github_repository=(
                github_repository.full_name if github_repository is not None else None
            ),
            remote=remote,
            remote_error=remote_error,
        )

    async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
        for change_id, cached_change in state.changes.items():
            stale_reason = _stale_change_reason(
                change_id=change_id,
                jj_client=jj_client,
            )
            if stale_reason is not None:
                actions.append(
                    _cache_action(
                        change_id=change_id,
                        reason=stale_reason,
                        status="applied" if apply else "planned",
                    )
                )
                if apply:
                    next_changes.pop(change_id, None)

                remote_action = _plan_remote_branch_cleanup(
                    bookmark_state=bookmark_states.get(
                        cached_change.bookmark or "",
                        BookmarkState(name=cached_change.bookmark or ""),
                    ),
                    cached_change=cached_change,
                    remote=remote,
                )
                if remote_action is not None:
                    actions.append(remote_action)
                    if apply and remote_action.status == "planned" and remote is not None:
                        jj_client.delete_remote_bookmark(
                            remote=remote.name,
                            bookmark=cached_change.bookmark or "",
                        )
                        actions[-1] = CleanupAction(
                            kind=remote_action.kind,
                            message=remote_action.message,
                            status="applied",
                        )

            comment_action = await _plan_stack_comment_cleanup(
                cached_change=cached_change,
                github_client=github_client,
                github_repository=github_repository,
            )
            if comment_action is None:
                continue
            actions.append(comment_action)
            if (
                apply
                and comment_action.status == "planned"
                and cached_change.stack_comment_id is not None
            ):
                await _delete_issue_comment(
                    comment_id=cached_change.stack_comment_id,
                    github_client=github_client,
                    github_repository=github_repository,
                )
                if change_id in next_changes:
                    next_changes[change_id] = next_changes[change_id].model_copy(
                        update={"stack_comment_id": None}
                    )
                actions[-1] = CleanupAction(
                    kind=comment_action.kind,
                    message=comment_action.message,
                    status="applied",
                )

    if apply and next_changes != state.changes:
        state_store.save(state.model_copy(update={"changes": next_changes}))

    return CleanupResult(
        actions=tuple(actions),
        applied=apply,
        github_error=github_error,
        github_repository=github_repository.full_name,
        remote=remote,
        remote_error=remote_error,
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
) -> CleanupAction | None:
    bookmark = cached_change.bookmark
    if remote is None or bookmark is None or not bookmark.startswith("review/"):
        return None

    remote_state = bookmark_state.remote_target(remote.name)
    if remote_state is None or not remote_state.targets:
        return None

    branch_label = f"{bookmark}@{remote.name}"
    if bookmark_state.local_targets:
        return CleanupAction(
            kind="remote branch",
            message=(
                f"cannot delete remote review branch {branch_label} while the "
                f"local bookmark {bookmark!r} still exists"
            ),
            status="blocked",
        )
    if len(remote_state.targets) > 1:
        return CleanupAction(
            kind="remote branch",
            message=(
                f"cannot delete remote review branch {branch_label} because the "
                "remote bookmark is conflicted"
            ),
            status="blocked",
        )

    return CleanupAction(
        kind="remote branch",
        message=f"delete remote review branch {branch_label}",
        status="planned",
    )


async def _plan_stack_comment_cleanup(
    *,
    cached_change: CachedChange,
    github_client: GithubClient,
    github_repository,
) -> CleanupAction | None:
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
        return managed_comment
    if managed_comment is None:
        return None

    return CleanupAction(
        kind="stack comment",
        message=(
            "delete managed stack comment "
            f"#{managed_comment.id} from PR #{cached_change.pr_number}"
        ),
        status="planned",
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
