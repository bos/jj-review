"""Stop tracking one local change with jj-review while leaving the rest of the
stack alone.

Later jj-review commands will ignore that change unless you link it again.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from jj_review.command_ui import resolve_selected_revset
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.models.cache import CachedChange
from jj_review.review_inspection import _stream_status_async, prepare_status

HELP = "Stop managing one local change as part of review"


class UnlinkError(CliError):
    """Raised when `unlink` cannot safely remove active tracking."""


@dataclass(frozen=True, slots=True)
class UnlinkResult:
    """Rendered unlink result for one selected local revision."""

    already_unlinked: bool
    bookmark: str | None
    change_id: str
    selected_revset: str
    subject: str


def unlink(
    *,
    config_path: Path | None,
    current: bool,
    debug: bool,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `unlink`."""

    from jj_review.bootstrap import bootstrap_context
    from jj_review.commands.review_state import display_change_id

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    result = asyncio.run(
        _run_unlink_async(
            change_overrides=context.config.change,
            config=context.config.repo,
            repo_root=context.repo_root,
            revset=resolve_selected_revset(
                command_label="unlink",
                current=current,
                require_explicit=True,
                revset=revset,
            ),
        )
    )
    print(f"Selected revset: {result.selected_revset}")
    if result.already_unlinked:
        print(
            f"{result.subject} [{display_change_id(result.change_id)}] is already "
            "unlinked from review tracking."
        )
        return 0
    if result.bookmark is None:
        print(
            f"Stopped review tracking for {result.subject} "
            f"[{display_change_id(result.change_id)}]."
        )
    else:
        print(
            f"Stopped review tracking for {result.subject} "
            f"[{display_change_id(result.change_id)}], preserving {result.bookmark}."
        )
    return 0


async def _run_unlink_async(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
) -> UnlinkResult:
    prepared_status = prepare_status(
        change_overrides=change_overrides,
        config=config,
        fetch_remote_state=True,
        persist_bookmarks=False,
        repo_root=repo_root,
        revset=revset,
    )
    prepared = prepared_status.prepared
    if not prepared.status_revisions:
        raise UnlinkError("No reviewable commits between the selected revision and `trunk()`.")

    status_result = await _stream_status_async(
        persist_cache_updates=False,
        prepared_status=prepared_status,
        on_github_status=None,
        on_revision=None,
    )
    prepared_revision = prepared.status_revisions[-1]
    status_revision = _status_revision_for_change(
        status_result=status_result,
        change_id=prepared_revision.revision.change_id,
    )
    state_store = prepared.state_store
    state = state_store.load()
    cached_change = state.changes.get(prepared_revision.revision.change_id)
    bookmark = _resolved_unlink_bookmark(
        cached_change=cached_change,
        prepared_revision=prepared_revision,
        status_revision=status_revision,
    )
    if cached_change is not None and cached_change.is_unlinked:
        return UnlinkResult(
            already_unlinked=True,
            bookmark=bookmark,
            change_id=prepared_revision.revision.change_id,
            selected_revset=prepared_status.selected_revset,
            subject=prepared_revision.revision.subject,
        )

    if not _revision_has_active_review_link(
        bookmark=bookmark,
        cached_change=cached_change,
        prepared_client=prepared.client,
        prepared_revision=prepared_revision,
        status_revision=status_revision,
    ):
        raise UnlinkError(
            "The selected change has no active review tracking link to unlink. "
            "Use `relink` only when you need to attach an existing PR intentionally."
        )

    updated_change = (cached_change or CachedChange(bookmark=bookmark)).model_copy(
        update={
            "bookmark": bookmark,
            "unlinked_at": datetime.now(UTC).isoformat(),
            "link_state": "unlinked",
            "pr_number": None,
            "pr_review_decision": None,
            "pr_state": None,
            "pr_url": None,
            "stack_comment_id": None,
        }
    )
    next_state = state.model_copy(
        update={
            "changes": {
                **state.changes,
                prepared_revision.revision.change_id: updated_change,
            }
        }
    )
    state_store.save(next_state)
    return UnlinkResult(
        already_unlinked=False,
        bookmark=bookmark,
        change_id=prepared_revision.revision.change_id,
        selected_revset=prepared_status.selected_revset,
        subject=prepared_revision.revision.subject,
    )


def _resolved_unlink_bookmark(*, cached_change, prepared_revision, status_revision) -> str | None:
    if cached_change is not None and cached_change.bookmark is not None:
        return cached_change.bookmark
    pull_request_lookup = status_revision.pull_request_lookup
    if pull_request_lookup is not None and pull_request_lookup.pull_request is not None:
        return pull_request_lookup.pull_request.head.ref
    if prepared_revision.bookmark_source != "generated":
        return prepared_revision.bookmark
    return None


def _revision_has_active_review_link(
    *,
    bookmark: str | None,
    cached_change,
    prepared_client,
    prepared_revision,
    status_revision,
) -> bool:
    if cached_change is not None and not cached_change.is_unlinked and (
        cached_change.bookmark is not None
        or cached_change.pr_number is not None
        or cached_change.pr_url is not None
        or cached_change.pr_state is not None
        or cached_change.stack_comment_id is not None
    ):
        return True
    if bookmark is not None:
        bookmark_state = prepared_client.get_bookmark_state(bookmark)
        if bookmark_state.local_target == prepared_revision.revision.commit_id:
            return True
    remote_state = status_revision.remote_state
    if remote_state is not None and remote_state.targets:
        return True
    pull_request_lookup = status_revision.pull_request_lookup
    return pull_request_lookup is not None and pull_request_lookup.pull_request is not None


def _status_revision_for_change(*, status_result, change_id: str):
    for revision in status_result.revisions:
        if revision.change_id == change_id:
            return revision
    raise AssertionError("Selected unlink change is missing from the status result.")
