"""Explicit PR-linkage repair for existing review branches."""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from jj_review.cache import ReviewStateStore
from jj_review.commands.submit import (
    ResolvedGithubRepository,
    _build_github_client,
    resolve_github_repository,
    select_submit_remote,
)
from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClientError
from jj_review.intent import check_same_kind_intent, delete_intent, write_intent
from jj_review.jj import JjClient
from jj_review.models.cache import CachedChange, ReviewState
from jj_review.models.intent import RelinkIntent

_PULL_REQUEST_URL_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[0-9]+)/?$"
)
_DISPLAY_CHANGE_ID_LENGTH = 8


class RelinkResolutionError(CliError):
    """Raised when `relink` cannot safely bind a PR to a local change."""


@dataclass(frozen=True, slots=True)
class RelinkResult:
    """Explicit review relink result for one local revision."""

    bookmark: str
    change_id: str
    github_repository: str
    pull_request_number: int
    remote_name: str
    selected_revset: str
    subject: str


def run_relink(
    *,
    config: RepoConfig,
    pull_request_reference: str,
    repo_root: Path,
    revset: str | None,
) -> RelinkResult:
    """Reassociate an existing pull request with one local reviewable change."""

    return asyncio.run(
        _run_relink_async(
            config=config,
            pull_request_reference=pull_request_reference,
            repo_root=repo_root,
            revset=revset,
        )
    )


async def _run_relink_async(
    *,
    config: RepoConfig,
    pull_request_reference: str,
    repo_root: Path,
    revset: str | None,
) -> RelinkResult:
    client = JjClient(repo_root)
    state_store = ReviewStateStore.for_repo(repo_root)
    state_dir = state_store.require_writable()

    stack = client.discover_review_stack(revset)
    if not stack.revisions:
        raise RelinkResolutionError(
            "No reviewable commits between the selected revision and `trunk()`."
        )
    revision = stack.head
    selected_revset = stack.selected_revset

    remotes = client.list_git_remotes()
    remote = select_submit_remote(config, remotes)
    client.fetch_remote(remote=remote.name)
    github_repository = resolve_github_repository(config, remote)
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
            raise RelinkResolutionError(
                f"Could not load pull request #{pull_request_number}: {error}"
            ) from error

    if pull_request.state != "open":
        raise RelinkResolutionError(
            f"Pull request #{pull_request.number} is not open; cannot relink "
            f"{pull_request.state!r} PRs."
        )

    bookmark = pull_request.head.ref
    expected_head_label = f"{github_repository.owner}:{bookmark}"
    if pull_request.head.label != expected_head_label:
        raise RelinkResolutionError(
            f"Pull request #{pull_request.number} head {pull_request.head.label!r} does not "
            f"belong to {github_repository.full_name}. Relink only supports same-repository "
            "review branches."
        )

    bookmark_state = client.get_bookmark_state(bookmark)
    if len(bookmark_state.local_targets) > 1:
        raise RelinkResolutionError(
            f"Local bookmark {bookmark!r} is conflicted. Resolve it before relinking."
        )
    if (
        bookmark_state.local_target is not None
        and bookmark_state.local_target != revision.commit_id
    ):
        raise RelinkResolutionError(
            f"Local bookmark {bookmark!r} already points to a different revision. "
            "Move or forget it explicitly before relinking."
        )
    remote_state = bookmark_state.remote_target(remote.name)
    if remote_state is None or not remote_state.targets:
        raise RelinkResolutionError(
            f"Remote bookmark {bookmark!r}@{remote.name} does not exist. Fetch "
            "and retry once the PR head branch is visible on the selected remote."
        )
    if len(remote_state.targets) > 1:
        raise RelinkResolutionError(
            f"Remote bookmark {bookmark!r}@{remote.name} is conflicted. Resolve it before "
            "relinking."
        )

    state = state_store.load()
    _ensure_relinkable_cached_linkage(
        bookmark=bookmark,
        change_id=revision.change_id,
        pull_request_number=pull_request.number,
        state=state,
    )

    # Write intent before the first mutation
    intent = RelinkIntent(
        kind="relink",
        pid=os.getpid(),
        label=f"relink for {revision.change_id[:8]}",
        change_id=revision.change_id,
        started_at=datetime.now(UTC).isoformat(),
    )
    stale_intents = check_same_kind_intent(state_dir, intent)
    for loaded in stale_intents:
        print(f"Warning: a previous relink was interrupted ({loaded.intent.label})")
    intent_path = write_intent(state_dir, intent)

    _relink_succeeded = False
    try:
        client.set_bookmark(bookmark, revision.change_id)

        cached_change = state.changes.get(revision.change_id)
        updated_change = (cached_change or CachedChange()).model_copy(
            update={
                "bookmark": bookmark,
                "detached_at": None,
                "link_state": "active",
                "pr_number": pull_request.number,
                "pr_review_decision": None,
                "pr_state": pull_request.state,
                "pr_url": pull_request.html_url,
                "stack_comment_id": None,
            }
        )
        state_store.save(
            state.model_copy(
                update={
                    "changes": {
                        **state.changes,
                        revision.change_id: updated_change,
                    }
                }
            )
        )
        _relink_succeeded = True
        return RelinkResult(
            bookmark=bookmark,
            change_id=revision.change_id,
            github_repository=github_repository.full_name,
            pull_request_number=pull_request.number,
            remote_name=remote.name,
            selected_revset=selected_revset,
            subject=revision.subject,
        )
    finally:
        if _relink_succeeded:
            delete_intent(intent_path)


def _ensure_relinkable_cached_linkage(
    *,
    bookmark: str,
    change_id: str,
    pull_request_number: int,
    state: ReviewState,
) -> None:
    for cached_change_id, cached_change in state.changes.items():
        if cached_change_id == change_id:
            continue
        if cached_change.bookmark == bookmark:
            raise RelinkResolutionError(
                "Bookmark "
                f"{bookmark!r} is already cached for change "
                f"{cached_change_id[:_DISPLAY_CHANGE_ID_LENGTH]}. "
                "Clear or repair that linkage before relinking it elsewhere."
            )
        if cached_change.pr_number == pull_request_number:
            raise RelinkResolutionError(
                f"Pull request #{pull_request_number} is already cached for change "
                f"{cached_change_id[:_DISPLAY_CHANGE_ID_LENGTH]}. Clear or repair that linkage "
                "before relinking it "
                "elsewhere."
            )


def _parse_pull_request_reference(
    *,
    reference: str,
    github_repository: ResolvedGithubRepository,
) -> int:
    if reference.isdigit():
        return int(reference)

    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise RelinkResolutionError(
            f"Pull request reference {reference!r} is not a PR number or URL."
        )
    if parsed.hostname != github_repository.host:
        raise RelinkResolutionError(
            f"Pull request URL {reference!r} does not match configured host "
            f"{github_repository.host!r}."
        )
    match = _PULL_REQUEST_URL_RE.fullmatch(parsed.path)
    if match is None:
        raise RelinkResolutionError(
            f"Pull request URL {reference!r} is not a valid pull request URL."
        )
    if (
        match.group("owner") != github_repository.owner
        or match.group("repo") != github_repository.repo
    ):
        raise RelinkResolutionError(
            f"Pull request URL {reference!r} does not match configured repository "
            f"{github_repository.full_name!r}."
        )
    return int(match.group("number"))


run_adopt = run_relink
