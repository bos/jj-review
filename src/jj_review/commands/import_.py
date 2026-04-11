"""Set up local jj-review tracking for an existing reviewed stack.

By default, `import` tries to match the current stack headed by `@-` to the
existing pull requests for that stack.

Use `--pull-request` to select a specific reviewed stack, or `--revset` to
select a different local stack. Use `--fetch` when the stack is not available
locally yet; for an explicit pull request, this fetches the needed review
branches first and then imports the stack.

`import` does not rewrite commits, restack changes, or modify GitHub.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

from jj_review.bookmarks import (
    bookmark_matches_generated_change_id,
    discover_bookmarks_for_revisions,
)
from jj_review.bootstrap import bootstrap_context
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClientError, build_github_client
from jj_review.github.resolution import (
    ParsedGithubRepo,
    require_github_repo,
    select_submit_remote,
)
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.cache import CachedChange
from jj_review.models.github import GithubPullRequest
from jj_review.pull_request_references import parse_repository_pull_request_reference
from jj_review.review_inspection import (
    PreparedStatus,
    StatusResult,
    prepare_status,
    stream_status_async,
)

HELP = "Set up local jj-review tracking for an existing stack"

_DISPLAY_CHANGE_ID_LENGTH = 8
ImportActionStatus = Literal["applied"]


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
    fetched_tip_commit: str | None
    github_error: str | None
    github_repository: str | None
    remote: GitRemote | None
    remote_error: str | None
    reviewable_revision_count: int
    selected_revset: str
    selector: str


@dataclass(frozen=True, slots=True)
class _Selection:
    default_current_stack: bool
    fetched_tip_commit: str | None
    selector: str
    head_bookmark: str | None
    selected_revset: str | None


@dataclass(frozen=True, slots=True)
class _PlannedImport:
    bookmark: str
    track_remote: bool
    update_local_bookmark: bool
    update_local_target: str


class _RevisionWithChangeId(Protocol):
    @property
    def change_id(self) -> str: ...


def import_(
    *,
    config_path: Path | None,
    debug: bool,
    fetch: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `import`."""

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    result = asyncio.run(
        _run_import_async(
            change_overrides=context.config.change,
            config=context.config.repo,
            fetch=fetch,
            pull_request_reference=pull_request,
            repo_root=context.repo_root,
            revset=revset,
        )
    )
    print(f"Selected selector: {result.selector}")
    print(f"Selected revset: {result.selected_revset}")
    if result.fetched_tip_commit is not None:
        print(f"Fetched tip commit: {result.fetched_tip_commit}")
    if result.remote is None:
        if result.remote_error is None:
            print("Selected remote: unavailable")
        else:
            print(f"Selected remote: unavailable ({result.remote_error})")
    else:
        print(f"Selected remote: {result.remote.name}")
    if result.github_repository is None:
        if result.github_error is None:
            print("GitHub: unavailable")
        else:
            print(f"GitHub: unavailable ({result.github_error})")
    else:
        print(f"GitHub: {result.github_repository}")
    if result.actions:
        print("Updated local jj-review tracking:")
        for action in result.actions:
            print(f"- [{action.status}] {action.kind}: {action.message}")
    else:
        if result.reviewable_revision_count:
            print("Local jj-review tracking is already up to date for the selected stack.")
        else:
            print("No reviewable commits between the selected revision and `trunk()`.")
    return 0


async def _run_import_async(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    fetch: bool,
    pull_request_reference: str | None,
    repo_root: Path,
    revset: str | None,
) -> ImportResult:
    client = JjClient(repo_root)
    selection = await _resolve_selection(
        client=client,
        fetch=fetch,
        pull_request_reference=pull_request_reference,
        revset=revset,
    )
    if (
        not fetch
        and selection.head_bookmark is not None
        and selection.selected_revset is not None
        and not client.query_revisions(selection.selected_revset, limit=1)
    ):
        raise CliError(
            f"Branch {selection.head_bookmark!r} is not present locally. Re-run "
            "`import --fetch` to fetch that stack before importing."
        )
    prepared_status = prepare_status(
        change_overrides=change_overrides,
        config=config,
        fetch_remote_state=fetch and selection.head_bookmark is None,
        persist_bookmarks=False,
        repo_root=repo_root,
        revset=selection.selected_revset,
    )
    if (
        selection.default_current_stack
        and selection.head_bookmark is None
        and not _prepared_status_has_discoverable_remote_link(prepared_status)
    ):
        raise CliError(
            "`import` cannot proceed because the current stack has no matching "
            "remote pull request."
        )
    print("Inspecting GitHub pull requests and branches...")
    status_result = await stream_status_async(
        persist_cache_updates=False,
        prepared_status=prepared_status,
        on_github_status=None,
        on_revision=None,
    )
    _ensure_selected_head_has_pull_request(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    prepared = prepared_status.prepared
    bookmark_states = prepared.client.list_bookmark_states()
    authoritative_remote_targets: dict[str, str] = {}
    if fetch and selection.head_bookmark is not None and prepared.remote is not None:
        authoritative_remote_targets = _fetch_selected_stack_bookmarks(
            client=prepared.client,
            explicit_head_bookmark=selection.head_bookmark,
            remote=prepared.remote,
            revisions=prepared.stack.revisions,
        )
        bookmark_states = _apply_authoritative_remote_targets(
            bookmark_states=prepared.client.list_bookmark_states(),
            authoritative_remote_targets=authoritative_remote_targets,
            remote_name=prepared.remote.name,
            relevant_bookmarks={
                prepared_revision.bookmark
                for prepared_revision in prepared.status_revisions
            },
        )
    bookmark_by_change_id: dict[str, str] = {}
    if prepared.remote is not None:
        bookmark_by_change_id.update(
            discover_bookmarks_for_revisions(
                bookmark_states=bookmark_states,
                remote_name=prepared.remote.name,
                revisions=prepared.stack.revisions,
            )
        )
    if selection.head_bookmark is not None and prepared_status.prepared.status_revisions:
        head_revision = prepared_status.prepared.status_revisions[-1]
        bookmark_by_change_id[head_revision.revision.change_id] = selection.head_bookmark

    actions = _import_local_state(
        client=prepared_status.prepared.client,
        prepared_status=prepared_status,
        status_result=status_result,
        bookmark_by_change_id=bookmark_by_change_id,
        bookmark_states=bookmark_states,
    )
    return ImportResult(
        actions=actions,
        fetched_tip_commit=selection.fetched_tip_commit,
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
    fetch: bool,
    pull_request_reference: str | None,
    revset: str | None,
) -> _Selection:
    selector_count = sum(
        1
        for present in (
            pull_request_reference is not None,
            revset is not None,
        )
        if present
    )
    if selector_count > 1:
        raise CliError(
            "`import` accepts at most one selector: `--pull-request` or `--revset`."
        )

    if selector_count == 0:
        return _Selection(
            default_current_stack=True,
            fetched_tip_commit=None,
            selector="default current stack (@-)",
            head_bookmark=None,
            selected_revset="@-",
        )
    if revset is not None:
        return _Selection(
            default_current_stack=False,
            fetched_tip_commit=None,
            selector=f"--revset {revset}",
            head_bookmark=None,
            selected_revset=revset,
        )
    if pull_request_reference is not None:
        return await _resolve_pull_request_selection(
            client=client,
            fetch=fetch,
            pull_request_reference=pull_request_reference,
        )
    raise AssertionError("One selector is always required.")


async def _resolve_pull_request_selection(
    *,
    client: JjClient,
    fetch: bool,
    pull_request_reference: str,
) -> _Selection:
    remotes = client.list_git_remotes()
    remote = select_submit_remote(remotes)
    github_repository = require_github_repo(remote)
    pull_request = await _load_pull_request(
        github_repository=github_repository,
        pull_request_reference=pull_request_reference,
    )
    head = pull_request.head.ref
    if fetch:
        client.fetch_remote(remote=remote.name, branches=(head,))

    pull_requests = await _list_pull_requests_by_head(
        github_repository=github_repository,
        head=head,
    )
    if len(pull_requests) != 1:
        if not pull_requests:
            raise CliError(
                f"GitHub no longer reports a pull request for head branch "
                f"{github_repository.owner}:{head}. Inspect the PR link with "
                "`status --fetch` and repair it with `relink` before importing again."
            )
        numbers = ", ".join(str(pull_request.number) for pull_request in pull_requests)
        raise CliError(
            f"GitHub reports multiple pull requests for head branch "
            f"{github_repository.owner}:{head}: {numbers}. Inspect the PR link with "
            "`status --fetch` and repair it with `relink` before importing again."
        )
    pull_request = pull_requests[0]
    if pull_request.head.label != f"{github_repository.owner}:{head}":
        raise CliError(
            f"Pull request #{pull_request.number} head {pull_request.head.label!r} does "
            f"not belong to {github_repository.full_name}. Import only supports "
            "same-repository pull request branches."
        )

    remote_state = client.get_bookmark_state(head).remote_target(remote.name)
    selected_revset = _remote_bookmark_commit_id(
        fetch=fetch,
        remote=remote,
        remote_state=remote_state,
        head=head,
    )
    return _Selection(
        default_current_stack=False,
        fetched_tip_commit=selected_revset if fetch else None,
        selector=f"--pull-request {pull_request_reference}",
        head_bookmark=head,
        selected_revset=selected_revset,
    )


def _fetch_selected_stack_bookmarks(
    *,
    client: JjClient,
    explicit_head_bookmark: str,
    remote: GitRemote,
    revisions: Sequence[_RevisionWithChangeId],
) -> dict[str, str]:
    head_change_id = revisions[-1].change_id if revisions else None
    patterns = tuple(
        sorted({
            f"refs/heads/{explicit_head_bookmark}",
            *(
                "refs/heads/review/*-"
                f"{revision.change_id[:_DISPLAY_CHANGE_ID_LENGTH]}"
                for revision in revisions
            ),
        })
    )
    remote_branches = client.list_remote_branches(remote=remote.name, patterns=patterns)
    if explicit_head_bookmark not in remote_branches:
        raise CliError(
            f"Remote bookmark {explicit_head_bookmark!r}@{remote.name} does not exist. "
            "Fetch and retry once that branch is visible on the selected remote."
        )
    selected_branch_targets = {
        explicit_head_bookmark: remote_branches[explicit_head_bookmark],
    }
    for revision in revisions:
        change_id = revision.change_id
        if change_id == head_change_id:
            continue
        candidates = sorted(
            name
            for name in remote_branches
            if bookmark_matches_generated_change_id(name, change_id)
        )
        if len(candidates) > 1:
            raise CliError(
                "Could not safely import the selected stack because "
                f"{change_id[:_DISPLAY_CHANGE_ID_LENGTH]} matches multiple remote review "
                f"branches on {remote.name}: {', '.join(candidates)}."
            )
        if len(candidates) == 1:
            selected_branch_targets[candidates[0]] = remote_branches[candidates[0]]

    bookmark_states = client.list_bookmark_states(tuple(sorted(selected_branch_targets)))
    branches_to_fetch = tuple(
        bookmark
        for bookmark, target in sorted(selected_branch_targets.items())
        if (
            (
                remote_state := bookmark_states.get(
                    bookmark,
                    BookmarkState(name=bookmark),
                ).remote_target(remote.name)
            )
            is None
            or remote_state.target != target
        )
    )
    if branches_to_fetch:
        client.fetch_remote(remote=remote.name, branches=branches_to_fetch)
    return selected_branch_targets


def _apply_authoritative_remote_targets(
    *,
    bookmark_states: dict[str, BookmarkState],
    authoritative_remote_targets: dict[str, str],
    remote_name: str,
    relevant_bookmarks: set[str],
) -> dict[str, BookmarkState]:
    if not authoritative_remote_targets:
        return bookmark_states

    updated_states = dict(bookmark_states)
    for bookmark in sorted(relevant_bookmarks | set(authoritative_remote_targets)):
        bookmark_state = updated_states.get(bookmark, BookmarkState(name=bookmark))
        existing_remote_state = bookmark_state.remote_target(remote_name)
        other_remote_targets = tuple(
            remote_state
            for remote_state in bookmark_state.remote_targets
            if remote_state.remote != remote_name
        )
        authoritative_target = authoritative_remote_targets.get(bookmark)
        if authoritative_target is None:
            updated_states[bookmark] = bookmark_state.model_copy(
                update={"remote_targets": other_remote_targets}
            )
            continue
        if (
            existing_remote_state is not None
            and existing_remote_state.target == authoritative_target
        ):
            updated_states[bookmark] = bookmark_state
            continue
        updated_states[bookmark] = bookmark_state.model_copy(
            update={
                "remote_targets": other_remote_targets
                + (
                    RemoteBookmarkState(
                        remote=remote_name,
                        targets=(authoritative_target,),
                        tracking_targets=(
                            ()
                            if existing_remote_state is None
                            else existing_remote_state.tracking_targets
                        ),
                    ),
                )
            }
        )
    return updated_states


async def _load_pull_request(
    *,
    github_repository: ParsedGithubRepo,
    pull_request_reference: str,
) -> GithubPullRequest:
    pull_request_number = parse_repository_pull_request_reference(
        reference=pull_request_reference,
        github_repository=github_repository,
        invalid_reference_message=(
            f"Pull request reference {pull_request_reference!r} is not a PR number or URL."
        ),
    )
    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        try:
            pull_request = await github_client.get_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request_number,
            )
        except GithubClientError as error:
            raise CliError(
                f"Could not load pull request #{pull_request_number}: {error}"
            ) from error

    if pull_request.head.label != f"{github_repository.owner}:{pull_request.head.ref}":
        raise CliError(
            f"Pull request #{pull_request.number} head {pull_request.head.label!r} does not "
            f"belong to {github_repository.full_name}. Import only supports same-repository "
            "pull request branches."
        )
    return pull_request


async def _list_pull_requests_by_head(
    *,
    github_repository: ParsedGithubRepo,
    head: str,
) -> tuple[GithubPullRequest, ...]:
    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        try:
            pull_requests = await github_client.list_pull_requests(
                github_repository.owner,
                github_repository.repo,
                head=f"{github_repository.owner}:{head}",
                state="all",
            )
        except GithubClientError as error:
            raise CliError(
                f"Could not list pull requests for head {head!r}: {error}"
            ) from error
    return tuple(pull_requests)


def _remote_bookmark_commit_id(
    *,
    fetch: bool,
    remote: GitRemote,
    remote_state: RemoteBookmarkState | None,
    head: str,
) -> str:
    if remote_state is None or not remote_state.targets:
        if not fetch:
            raise CliError(
                f"Remote bookmark {head!r}@{remote.name} is not available in remembered "
                "local remote state. Re-run `import --fetch` to fetch that branch "
                "before importing."
            )
        raise CliError(
            f"Remote bookmark {head!r}@{remote.name} does not exist. Fetch and retry once "
            "that branch is visible on the selected remote."
        )
    if len(remote_state.targets) > 1:
        raise CliError(
            f"Remote bookmark {head!r}@{remote.name} is conflicted. Resolve it before "
            "importing."
        )
    commit_id = remote_state.target
    if commit_id is None:
        raise CliError(
            f"Remote bookmark {head!r}@{remote.name} is ambiguous. Import requires one "
            "exact branch."
        )
    return commit_id


def _import_local_state(
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
    planned_imports: list[_PlannedImport] = []

    seen_bookmarks: set[str] = set()
    for prepared_revision in prepared.status_revisions:
        bookmark = _resolve_import_bookmark(
            bookmark_by_change_id=bookmark_by_change_id,
            bookmark_states=bookmark_states,
            prepared_revision=prepared_revision,
            selected_remote_name=selected_remote_name,
        )
        if bookmark in seen_bookmarks:
            raise CliError(
                "Selected stack resolves multiple changes to the same "
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
        planned_imports.append(
            _PlannedImport(
                bookmark=bookmark,
                track_remote=track_remote,
                update_local_bookmark=(
                    bookmark_state.local_target != prepared_revision.revision.commit_id
                ),
                update_local_target=prepared_revision.revision.commit_id,
            )
        )

    for planned in planned_imports:
        if planned.update_local_bookmark:
            client.set_bookmark(planned.bookmark, planned.update_local_target)
            actions.append(
                ImportAction(
                    kind="bookmark",
                    message=(
                        f"set local bookmark {planned.bookmark} -> "
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
                        f"track remote branch {planned.bookmark}"
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
                kind="tracking",
                message="update saved jj-review data for the selected stack",
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
        raise CliError(
            f"Local bookmark {bookmark!r} is conflicted. Resolve it before importing."
        )
    if (
        bookmark_state.local_target is not None
        and bookmark_state.local_target != desired_commit_id
    ):
        raise CliError(
            f"Local bookmark {bookmark!r} already points to a different revision. Move "
            "or forget it explicitly before importing."
        )
    if selected_remote_name is None:
        return
    remote_state = bookmark_state.remote_target(selected_remote_name)
    if remote_state is None:
        return
    if len(remote_state.targets) > 1:
        raise CliError(
            f"Remote bookmark {bookmark!r}@{selected_remote_name} is conflicted. Resolve "
            "it before importing."
        )
    if remote_state.target is not None and remote_state.target != desired_commit_id:
        raise CliError(
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
    if cached_change.is_unlinked:
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


def _prepared_status_has_discoverable_remote_link(
    prepared_status: PreparedStatus,
) -> bool:
    prepared = prepared_status.prepared
    remote = prepared.remote
    if remote is None:
        return False
    bookmark_states = prepared.client.list_bookmark_states(
        [revision.bookmark for revision in prepared.status_revisions]
    )
    for revision in prepared.status_revisions:
        remote_state = bookmark_states.get(
            revision.bookmark,
            BookmarkState(name=revision.bookmark),
        ).remote_target(remote.name)
        if remote_state is not None and remote_state.targets:
            return True
    return False


def _ensure_selected_head_has_pull_request(
    *,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
) -> None:
    if not status_result.revisions:
        return

    selected_head_change_id = prepared_status.prepared.stack.head.change_id
    selected_head = next(
        (
            revision
            for revision in status_result.revisions
            if revision.change_id == selected_head_change_id
        ),
        None,
    )
    if selected_head is None:
        raise AssertionError("Selected import head is missing from the status result.")
    lookup = selected_head.pull_request_lookup
    if lookup is not None and lookup.pull_request is not None:
        return

    raise CliError(
        "`import` only supports stacks whose selected head already has a pull "
        "request. Missing pull request for: "
        f"{selected_head.subject} [{selected_head.change_id[:_DISPLAY_CHANGE_ID_LENGTH]}]."
    )


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
            raise CliError(
                "Could not safely import the selected stack because "
                f"{prepared_revision.revision.change_id[:_DISPLAY_CHANGE_ID_LENGTH]} has no "
                "matching pull request on the selected remote. Refresh with "
                "`status --fetch` or select an exact pull request."
            )
    if selected_remote_name is None:
        return bookmark
    bookmark_state = bookmark_states.get(bookmark, BookmarkState(name=bookmark))
    remote_state = bookmark_state.remote_target(selected_remote_name)
    if remote_state is None or remote_state.target is None:
        raise CliError(
            "Could not safely import the selected stack because "
            f"saved branch {bookmark!r} for "
            f"{prepared_revision.revision.change_id[:_DISPLAY_CHANGE_ID_LENGTH]} is not "
            "present on the selected remote. Refresh with `status --fetch` or select "
            "an exact pull request."
        )
    if remote_state.target != prepared_revision.revision.commit_id:
        raise CliError(
            "Could not safely import the selected stack because "
            f"saved branch {bookmark!r} for "
            f"{prepared_revision.revision.change_id[:_DISPLAY_CHANGE_ID_LENGTH]} points "
            "to a different revision on the selected remote. Refresh with "
            "`status --fetch` or repair the stale remote match before importing again."
        )
    return bookmark
