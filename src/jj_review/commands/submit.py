"""Submit command support for remote bookmark projection."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from jj_review.bookmarks import BookmarkResolver, BookmarkSource, ResolvedBookmark
from jj_review.cache import ReviewStateStore
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import ReviewState


class SubmitRemoteResolutionError(CliError):
    """Raised when `submit` cannot resolve which Git remote to use."""


class SubmitBookmarkCollisionError(CliError):
    """Raised when multiple review units resolve to the same bookmark."""


class SubmitBookmarkConflictError(CliError):
    """Raised when a local bookmark has multiple conflicting targets."""


class SubmitRemoteBookmarkConflictError(CliError):
    """Raised when the selected remote bookmark is conflicted."""


class SubmitRemoteBookmarkOwnershipError(CliError):
    """Raised when `submit` cannot prove an existing remote branch belongs to it."""


LocalBookmarkAction = Literal["created", "moved", "unchanged"]
RemoteBookmarkAction = Literal["pushed", "up to date"]


@dataclass(frozen=True, slots=True)
class SubmittedRevision:
    """Remote projection result for one revision in the submitted stack."""

    bookmark: str
    bookmark_source: BookmarkSource
    change_id: str
    local_action: LocalBookmarkAction
    remote_action: RemoteBookmarkAction
    subject: str


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Projected remote bookmark state for the selected stack."""

    remote: GitRemote
    revisions: tuple[SubmittedRevision, ...]
    selected_revset: str
    trunk_subject: str


def run_submit(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> SubmitResult:
    """Project the selected local stack to synthetic review bookmarks."""

    client = JjClient(repo_root)
    stack = client.discover_review_stack(revset)
    remotes = client.list_git_remotes()
    remote = select_submit_remote(config, remotes)
    state_store = ReviewStateStore.for_repo(repo_root)
    state = state_store.load()
    bookmark_result = BookmarkResolver(state, change_overrides).pin_revisions(stack.revisions)
    _ensure_unique_bookmarks(bookmark_result.resolutions)

    revisions: list[SubmittedRevision] = []
    for resolution, revision in zip(
        bookmark_result.resolutions,
        stack.revisions,
        strict=True,
    ):
        bookmark_state = client.get_bookmark_state(resolution.bookmark)
        local_action = _resolve_local_action(
            resolution.bookmark, bookmark_state.local_targets, revision.commit_id
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
            state=state,
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
                    raise AssertionError("Checked remote bookmark target must be unambiguous.")
                client.update_untracked_remote_bookmark(
                    remote=remote.name,
                    bookmark=resolution.bookmark,
                    desired_target=revision.commit_id,
                    expected_remote_target=expected_remote_target,
                )
            else:
                client.push_bookmark(remote=remote.name, bookmark=resolution.bookmark)
            remote_action = "pushed"

        revisions.append(
            SubmittedRevision(
                bookmark=resolution.bookmark,
                bookmark_source=resolution.source,
                change_id=revision.change_id,
                local_action=local_action,
                remote_action=remote_action,
                subject=revision.subject,
            )
        )

    if bookmark_result.changed:
        state_store.save(bookmark_result.state)

    return SubmitResult(
        remote=remote,
        revisions=tuple(revisions),
        selected_revset=stack.selected_revset,
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
            f"Remote bookmark {bookmark!r}@{remote} is conflicted. Resolve it with `jj git "
            "fetch` and retry."
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
        f"{collision_descriptions}. Configure distinct bookmark names before submitting."
    )
