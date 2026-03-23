"""Import sparse local review state for an exact stack selector."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from jj_review.commands.review_state import (
    PreparedStatus,
    StatusResult,
    _stream_status_async,
    prepare_status,
)
from jj_review.commands.submit import (
    ResolvedGithubRepository,
    _build_github_client,
    _discover_bookmarks_for_revisions,
    resolve_github_repository,
    select_submit_remote,
)
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClientError
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange
from jj_review.models.github import GithubPullRequest

_DISPLAY_CHANGE_ID_LENGTH = 8
_PULL_REQUEST_URL_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[0-9]+)/?$"
)
ImportActionStatus = Literal["applied"]


class ImportResolutionError(CliError):
    """Raised when `import` cannot safely materialize a review stack."""


@dataclass(frozen=True, slots=True)
class ImportAction:
    """One applied import action."""

    kind: str
    message: str
    status: ImportActionStatus


@dataclass(frozen=True, slots=True)
class ImportResult:
    """Rendered import result for the selected repository."""

    actions: tuple[ImportAction, ...]
    github_error: str | None
    github_repository: str | None
    remote: GitRemote | None
    remote_error: str | None
    reviewable_revision_count: int
    selected_revset: str
    selector: str


@dataclass(frozen=True, slots=True)
class _Selection:
    selector: str
    head_bookmark: str | None
    selected_revset: str | None


@dataclass(frozen=True, slots=True)
class _PlannedMaterialization:
    bookmark: str
    track_remote: bool
    update_local_bookmark: bool
    update_local_target: str


def run_import(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    current: bool,
    head: str | None,
    pull_request_reference: str | None,
    repo_root: Path,
    revset: str | None,
) -> ImportResult:
    """Materialize sparse review state for one exact selected stack."""

    return asyncio.run(
        _run_import_async(
            change_overrides=change_overrides,
            config=config,
            current=current,
            head=head,
            pull_request_reference=pull_request_reference,
            repo_root=repo_root,
            revset=revset,
        )
    )


async def _run_import_async(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    current: bool,
    head: str | None,
    pull_request_reference: str | None,
    repo_root: Path,
    revset: str | None,
) -> ImportResult:
    client = JjClient(repo_root)
    selection = await _resolve_selection(
        client=client,
        config=config,
        current=current,
        head=head,
        pull_request_reference=pull_request_reference,
        revset=revset,
    )
    prepared_status = prepare_status(
        change_overrides=change_overrides,
        config=config,
        fetch_remote_state=True,
        persist_bookmarks=False,
        repo_root=repo_root,
        revset=selection.selected_revset,
    )
    status_result = await _stream_status_async(
        persist_cache_updates=False,
        prepared_status=prepared_status,
        on_github_status=None,
        on_revision=None,
    )
    if current and not _status_has_discoverable_linkage(status_result):
        raise ImportResolutionError(
            "`import --current` cannot proceed because the current local path has no "
            "discoverable remote review linkage."
        )

    prepared = prepared_status.prepared
    bookmark_states = prepared.client.list_bookmark_states()
    bookmark_by_change_id: dict[str, str] = {}
    if prepared.remote is not None:
        bookmark_by_change_id.update(
            _discover_bookmarks_for_revisions(
                bookmark_states=bookmark_states,
                remote_name=prepared.remote.name,
                revisions=prepared.stack.revisions,
            )
        )
    if selection.head_bookmark is not None and prepared_status.prepared.status_revisions:
        head_revision = prepared_status.prepared.status_revisions[-1]
        bookmark_by_change_id[head_revision.revision.change_id] = selection.head_bookmark

    actions = _materialize_local_state(
        client=prepared_status.prepared.client,
        prepared_status=prepared_status,
        status_result=status_result,
        bookmark_by_change_id=bookmark_by_change_id,
        bookmark_states=bookmark_states,
    )
    return ImportResult(
        actions=actions,
        github_error=status_result.github_error,
        github_repository=prepared_status.github_repository.full_name
        if prepared_status.github_repository is not None
        else None,
        remote=prepared_status.prepared.remote,
        remote_error=prepared_status.prepared.remote_error,
        reviewable_revision_count=len(prepared_status.prepared.status_revisions),
        selected_revset=prepared_status.selected_revset,
        selector=selection.selector,
    )


async def _resolve_selection(
    *,
    client: JjClient,
    config: RepoConfig,
    current: bool,
    head: str | None,
    pull_request_reference: str | None,
    revset: str | None,
) -> _Selection:
    selector_count = sum(
        1
        for present in (
            current,
            head is not None,
            pull_request_reference is not None,
            revset is not None,
        )
        if present
    )
    if selector_count != 1:
        raise ImportResolutionError(
            "`import` requires exactly one selector: `--pull-request`, `--head`, "
            "`--current`, or `--revset`."
        )

    if current:
        return _Selection(
            selector="--current",
            head_bookmark=None,
            selected_revset=None,
        )
    if revset is not None:
        return _Selection(
            selector=f"--revset {revset}",
            head_bookmark=None,
            selected_revset=revset,
        )
    if pull_request_reference is not None:
        return await _resolve_remote_head(
            client=client,
            config=config,
            pull_request_reference=pull_request_reference,
        )
    if head is not None:
        return await _resolve_remote_head(
            client=client,
            config=config,
            head=head,
        )
    raise AssertionError("One selector is always required.")


async def _resolve_remote_head(
    *,
    client: JjClient,
    config: RepoConfig,
    head: str | None = None,
    pull_request_reference: str | None = None,
) -> _Selection:
    remotes = client.list_git_remotes()
    remote = select_submit_remote(config, remotes)
    client.fetch_remote(remote=remote.name)
    github_repository = resolve_github_repository(config, remote)

    if pull_request_reference is not None:
        pull_request = await _load_pull_request(
            github_repository=github_repository,
            pull_request_reference=pull_request_reference,
        )
        head = pull_request.head.ref

    if head is None:
        raise AssertionError("A remote head bookmark must be selected.")

    pull_requests = await _list_pull_requests_by_head(
        github_repository=github_repository,
        head=head,
    )
    if pull_request_reference is not None and len(pull_requests) != 1:
        if not pull_requests:
            raise ImportResolutionError(
                f"GitHub no longer reports a pull request for head branch "
                f"{github_repository.owner}:{head}. Inspect the linkage with "
                "`status --fetch` and repair it with `relink` before importing again."
            )
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        raise ImportResolutionError(
            f"GitHub reports multiple pull requests for head branch "
            f"{github_repository.owner}:{head}: {numbers}. Inspect the linkage with "
            "`status --fetch` and repair it with `relink` before importing again."
        )
    if pull_request_reference is None and len(pull_requests) > 1:
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        raise ImportResolutionError(
            f"GitHub reports multiple pull requests for head branch "
            f"{github_repository.owner}:{head}: {numbers}. Inspect the linkage with "
            "`status --fetch` and repair it with `relink` before importing again."
        )
    if len(pull_requests) == 1:
        pull_request = pull_requests[0]
        if pull_request.head.label != f"{github_repository.owner}:{head}":
            raise ImportResolutionError(
                f"Pull request #{pull_request.number} head {pull_request.head.label!r} does "
                f"not belong to {github_repository.full_name}. Import only supports "
                "same-repository review branches."
            )

    remote_state = client.get_bookmark_state(head).remote_target(remote.name)
    selected_revset = _remote_bookmark_commit_id(
        remote=remote,
        remote_state=remote_state,
        head=head,
    )
    return _Selection(
        selector=(
            f"--pull-request {pull_request_reference}"
            if pull_request_reference is not None
            else f"--head {head}"
        ),
        head_bookmark=head,
        selected_revset=selected_revset,
    )


async def _load_pull_request(
    *,
    github_repository: ResolvedGithubRepository,
    pull_request_reference: str,
) -> GithubPullRequest:
    pull_request_number = _parse_pull_request_reference(
        reference=pull_request_reference,
        github_repository=github_repository,
    )
    async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
        try:
            pull_request = await github_client.get_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request_number,
            )
        except GithubClientError as error:
            raise ImportResolutionError(
                f"Could not load pull request #{pull_request_number}: {error}"
            ) from error

    if pull_request.head.label != f"{github_repository.owner}:{pull_request.head.ref}":
        raise ImportResolutionError(
            f"Pull request #{pull_request.number} head {pull_request.head.label!r} does not "
            f"belong to {github_repository.full_name}. Import only supports same-repository "
            "review branches."
        )
    return pull_request


async def _list_pull_requests_by_head(
    *,
    github_repository: ResolvedGithubRepository,
    head: str,
) -> tuple[GithubPullRequest, ...]:
    async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
        try:
            pull_requests = await github_client.list_pull_requests(
                github_repository.owner,
                github_repository.repo,
                head=f"{github_repository.owner}:{head}",
                state="all",
            )
        except GithubClientError as error:
            raise ImportResolutionError(
                f"Could not list pull requests for head {head!r}: {error}"
            ) from error
    return tuple(pull_requests)


def _remote_bookmark_commit_id(
    *,
    remote: GitRemote,
    remote_state: RemoteBookmarkState | None,
    head: str,
) -> str:
    if remote_state is None or not remote_state.targets:
        raise ImportResolutionError(
            f"Remote bookmark {head!r}@{remote.name} does not exist. Fetch and retry once "
            "the review branch is visible on the selected remote."
        )
    if len(remote_state.targets) > 1:
        raise ImportResolutionError(
            f"Remote bookmark {head!r}@{remote.name} is conflicted. Resolve it before "
            "importing."
        )
    commit_id = remote_state.target
    if commit_id is None:
        raise ImportResolutionError(
            f"Remote bookmark {head!r}@{remote.name} is ambiguous. Import requires one "
            "exact review branch."
        )
    return commit_id


def _materialize_local_state(
    *,
    client: JjClient,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
    bookmark_by_change_id: dict[str, str],
    bookmark_states: dict[str, BookmarkState],
) -> tuple[ImportAction, ...]:
    prepared = prepared_status.prepared
    state_store = prepared.state_store
    current_state = state_store.load()
    next_changes = dict(current_state.changes)
    actions: list[ImportAction] = []
    selected_remote_name = (
        prepared.remote.name if prepared.remote is not None else None
    )
    planned_materializations: list[_PlannedMaterialization] = []

    seen_bookmarks: set[str] = set()
    for prepared_revision in prepared.status_revisions:
        bookmark = _resolve_import_bookmark(
            bookmark_by_change_id=bookmark_by_change_id,
            bookmark_states=bookmark_states,
            prepared_revision=prepared_revision,
            selected_remote_name=selected_remote_name,
        )
        if bookmark in seen_bookmarks:
            raise ImportResolutionError(
                "Selected stack resolves multiple review units to the same "
                f"bookmark {bookmark!r}."
            )
        seen_bookmarks.add(bookmark)

        bookmark_state = bookmark_states.get(bookmark, BookmarkState(name=bookmark))
        _validate_bookmark_state(
            bookmark=bookmark,
            bookmark_state=bookmark_state,
            desired_commit_id=prepared_revision.revision.commit_id,
            selected_remote_name=selected_remote_name,
        )
        remote_state = (
            bookmark_state.remote_target(prepared.remote.name)
            if prepared.remote is not None
            else None
        )
        track_remote = (
            prepared.remote is not None
            and remote_state is not None
            and remote_state.target == prepared_revision.revision.commit_id
            and not remote_state.is_tracked
        )

        existing_change = (
            next_changes.get(prepared_revision.revision.change_id)
            or current_state.changes.get(prepared_revision.revision.change_id)
        )
        cached_change = existing_change or CachedChange(bookmark=bookmark)
        updated_change = _update_cached_change_from_status(
            cached_change=cached_change,
            bookmark=bookmark,
            status_revision=_find_status_revision(
                status_result.revisions, prepared_revision.revision.change_id
            ),
        )
        if existing_change is None or updated_change != cached_change:
            next_changes[prepared_revision.revision.change_id] = updated_change
        planned_materializations.append(
            _PlannedMaterialization(
                bookmark=bookmark,
                track_remote=track_remote,
                update_local_bookmark=(
                    bookmark_state.local_target != prepared_revision.revision.commit_id
                ),
                update_local_target=prepared_revision.revision.commit_id,
            )
        )

    for planned in planned_materializations:
        if planned.update_local_bookmark:
            client.set_bookmark(planned.bookmark, planned.update_local_target)
            actions.append(
                ImportAction(
                    kind="bookmark",
                    message=(
                        f"set local review bookmark {planned.bookmark} -> "
                        f"{planned.update_local_target[:_DISPLAY_CHANGE_ID_LENGTH]}"
                    ),
                    status="applied",
                )
            )
        if planned.track_remote:
            if prepared.remote is None:
                raise AssertionError("Tracking requires a selected remote.")
            client.track_bookmark(remote=prepared.remote.name, bookmark=planned.bookmark)
            actions.append(
                ImportAction(
                    kind="bookmark tracking",
                    message=(
                        f"track remote review branch {planned.bookmark}"
                        f"@{prepared.remote.name}"
                    ),
                    status="applied",
                )
            )

    next_state = current_state.model_copy(update={"changes": next_changes})
    if next_state != current_state:
        state_store.save(next_state)
        actions.append(
            ImportAction(
                kind="cache",
                message="refresh sparse review cache for the selected stack",
                status="applied",
            )
        )
    return tuple(actions)


def _validate_bookmark_state(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    desired_commit_id: str,
    selected_remote_name: str | None,
) -> None:
    if len(bookmark_state.local_targets) > 1:
        raise ImportResolutionError(
            f"Local bookmark {bookmark!r} is conflicted. Resolve it before importing."
        )
    if (
        bookmark_state.local_target is not None
        and bookmark_state.local_target != desired_commit_id
    ):
        raise ImportResolutionError(
            f"Local bookmark {bookmark!r} already points to a different revision. Move "
            "or forget it explicitly before importing."
        )
    if selected_remote_name is None:
        return
    remote_state = bookmark_state.remote_target(selected_remote_name)
    if remote_state is None:
        return
    if len(remote_state.targets) > 1:
        raise ImportResolutionError(
            f"Remote bookmark {bookmark!r}@{selected_remote_name} is conflicted. Resolve "
            "it before importing."
        )
    if remote_state.target is not None and remote_state.target != desired_commit_id:
        raise ImportResolutionError(
            f"Remote bookmark {bookmark!r}@{selected_remote_name} already points to a "
            "different revision. Import will not overwrite a stale remote identity."
        )


def _find_status_revision(
    revisions: Sequence[object],
    change_id: str,
):
    for revision in revisions:
        if getattr(revision, "change_id", None) == change_id:
            return revision
    raise AssertionError("Status revision for imported change was not found.")


def _update_cached_change_from_status(
    *,
    cached_change: CachedChange,
    bookmark: str,
    status_revision,
) -> CachedChange:
    updated_change = cached_change.model_copy(update={"bookmark": bookmark})
    if cached_change.is_detached:
        return updated_change
    pull_request_lookup = getattr(status_revision, "pull_request_lookup", None)
    if pull_request_lookup is not None:
        if pull_request_lookup.state == "missing":
            updated_change = updated_change.model_copy(
                update={
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
                    "pr_number": pull_request.number,
                    "pr_state": pull_request.state,
                    "pr_url": pull_request.html_url,
                }
            )
            if getattr(pull_request_lookup, "review_decision_error", None) is None:
                updated_change = updated_change.model_copy(
                    update={
                        "pr_review_decision": getattr(
                            pull_request_lookup,
                            "review_decision",
                            None,
                        )
                    }
                )
            if pull_request_lookup.state != "open":
                updated_change = updated_change.model_copy(update={"stack_comment_id": None})

    stack_comment_lookup = getattr(status_revision, "stack_comment_lookup", None)
    if stack_comment_lookup is not None:
        if stack_comment_lookup.state == "present":
            comment = getattr(stack_comment_lookup, "comment", None)
            if comment is not None:
                updated_change = updated_change.model_copy(
                    update={"stack_comment_id": comment.id}
                )
        elif stack_comment_lookup.state == "missing":
            updated_change = updated_change.model_copy(update={"stack_comment_id": None})
    return updated_change


def _status_has_discoverable_linkage(status_result: StatusResult) -> bool:
    for revision in status_result.revisions:
        remote_state = revision.remote_state
        if remote_state is not None and remote_state.targets:
            return True
        pull_request_lookup = revision.pull_request_lookup
        if pull_request_lookup is not None and pull_request_lookup.pull_request is not None:
            return True
    return False


def _resolve_import_bookmark(
    *,
    bookmark_by_change_id: dict[str, str],
    bookmark_states: dict[str, BookmarkState],
    prepared_revision,
    selected_remote_name: str | None,
) -> str:
    exact_bookmark = bookmark_by_change_id.get(prepared_revision.revision.change_id)
    if exact_bookmark is not None:
        if selected_remote_name is None:
            return exact_bookmark
        bookmark = exact_bookmark
    else:
        bookmark = prepared_revision.bookmark
        if prepared_revision.bookmark_source == "generated":
            raise ImportResolutionError(
                "Could not safely import the selected stack because "
                f"{prepared_revision.revision.change_id[:_DISPLAY_CHANGE_ID_LENGTH]} has no "
                "discoverable review bookmark on the selected remote. Refresh with "
                "`status --fetch` or select an exact review branch or pull request."
            )
    if selected_remote_name is None:
        return bookmark
    bookmark_state = bookmark_states.get(bookmark, BookmarkState(name=bookmark))
    remote_state = bookmark_state.remote_target(selected_remote_name)
    if remote_state is None or remote_state.target is None:
        raise ImportResolutionError(
            "Could not safely import the selected stack because "
            f"cached review bookmark {bookmark!r} for "
            f"{prepared_revision.revision.change_id[:_DISPLAY_CHANGE_ID_LENGTH]} is not "
            "present on the selected remote. Refresh with `status --fetch` or "
            "select an exact review branch or pull request."
        )
    if remote_state.target != prepared_revision.revision.commit_id:
        raise ImportResolutionError(
            "Could not safely import the selected stack because "
            f"cached review bookmark {bookmark!r} for "
            f"{prepared_revision.revision.change_id[:_DISPLAY_CHANGE_ID_LENGTH]} points "
            "to a different revision on the selected remote. Refresh with "
            "`status --fetch` or repair the stale remote linkage before importing "
            "again."
        )
    return bookmark


def _parse_pull_request_reference(
    *,
    reference: str,
    github_repository: ResolvedGithubRepository,
) -> int:
    if reference.isdigit():
        return int(reference)
    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ImportResolutionError(
            f"Pull request reference {reference!r} is not a PR number or URL."
        )
    if parsed.hostname != github_repository.host:
        raise ImportResolutionError(
            f"Pull request URL {reference!r} does not match configured host "
            f"{github_repository.host!r}."
        )
    match = _PULL_REQUEST_URL_RE.fullmatch(parsed.path)
    if match is None:
        raise ImportResolutionError(
            f"Pull request URL {reference!r} is not a valid pull request URL."
        )
    if (
        match.group("owner") != github_repository.owner
        or match.group("repo") != github_repository.repo
    ):
        raise ImportResolutionError(
            f"Pull request URL {reference!r} does not match configured repository "
            f"{github_repository.full_name!r}."
        )
    return int(match.group("number"))
