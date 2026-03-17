"""Explicit PR-linkage repair for existing review branches."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
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
from jj_review.jj import JjClient
from jj_review.models.cache import CachedChange, ReviewState

_PULL_REQUEST_URL_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[0-9]+)/?$"
)


class AdoptResolutionError(CliError):
    """Raised when `adopt` cannot safely bind a PR to a local change."""


@dataclass(frozen=True, slots=True)
class AdoptResult:
    """Explicit review adoption result for one local revision."""

    bookmark: str
    change_id: str
    github_repository: str
    pull_request_number: int
    remote_name: str
    selected_revset: str
    subject: str


def run_adopt(
    *,
    config: RepoConfig,
    pull_request_reference: str,
    repo_root: Path,
    revset: str | None,
) -> AdoptResult:
    """Associate an existing pull request with one local reviewable change."""

    return asyncio.run(
        _run_adopt_async(
            config=config,
            pull_request_reference=pull_request_reference,
            repo_root=repo_root,
            revset=revset,
        )
    )


async def _run_adopt_async(
    *,
    config: RepoConfig,
    pull_request_reference: str,
    repo_root: Path,
    revset: str | None,
) -> AdoptResult:
    client = JjClient(repo_root)
    stack = client.discover_review_stack(revset)
    if not stack.revisions:
        raise AdoptResolutionError(
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
            raise AdoptResolutionError(
                f"Could not load pull request #{pull_request_number}: {error}"
            ) from error

    if pull_request.state != "open":
        raise AdoptResolutionError(
            f"Pull request #{pull_request.number} is not open; cannot adopt "
            f"{pull_request.state!r} PRs."
        )

    bookmark = pull_request.head.ref
    expected_head_label = f"{github_repository.owner}:{bookmark}"
    if pull_request.head.label != expected_head_label:
        raise AdoptResolutionError(
            f"Pull request #{pull_request.number} head {pull_request.head.label!r} does not "
            f"belong to {github_repository.full_name}. Adopt only supports same-repository "
            "review branches."
        )

    bookmark_state = client.get_bookmark_state(bookmark)
    if len(bookmark_state.local_targets) > 1:
        raise AdoptResolutionError(
            f"Local bookmark {bookmark!r} is conflicted. Resolve it before adopting."
        )
    if (
        bookmark_state.local_target is not None
        and bookmark_state.local_target != revision.commit_id
    ):
        raise AdoptResolutionError(
            f"Local bookmark {bookmark!r} already points to a different revision. "
            "Move or forget it explicitly before adopting."
        )
    remote_state = bookmark_state.remote_target(remote.name)
    if remote_state is None or not remote_state.targets:
        raise AdoptResolutionError(
            f"Remote bookmark {bookmark!r}@{remote.name} does not exist. Fetch "
            "and retry once the PR head branch is visible on the selected remote."
        )
    if len(remote_state.targets) > 1:
        raise AdoptResolutionError(
            f"Remote bookmark {bookmark!r}@{remote.name} is conflicted. Resolve it before "
            "adopting."
        )

    state_store = ReviewStateStore.for_repo(repo_root)
    state = state_store.load()
    _ensure_adoptable_cached_linkage(
        bookmark=bookmark,
        change_id=revision.change_id,
        pull_request_number=pull_request.number,
        state=state,
    )

    client.set_bookmark(bookmark, revision.change_id)

    cached_change = state.changes.get(revision.change_id)
    updated_change = (cached_change or CachedChange()).model_copy(
        update={
            "bookmark": bookmark,
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

    return AdoptResult(
        bookmark=bookmark,
        change_id=revision.change_id,
        github_repository=github_repository.full_name,
        pull_request_number=pull_request.number,
        remote_name=remote.name,
        selected_revset=selected_revset,
        subject=revision.subject,
    )


def _ensure_adoptable_cached_linkage(
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
            raise AdoptResolutionError(
                f"Bookmark {bookmark!r} is already cached for change {cached_change_id[:12]}. "
                "Clear or repair that linkage before adopting it elsewhere."
            )
        if cached_change.pr_number == pull_request_number:
            raise AdoptResolutionError(
                f"Pull request #{pull_request_number} is already cached for change "
                f"{cached_change_id[:12]}. Clear or repair that linkage before adopting it "
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
        raise AdoptResolutionError(
            f"Pull request reference {reference!r} is not a PR number or URL."
        )
    if parsed.hostname != github_repository.host:
        raise AdoptResolutionError(
            f"Pull request URL {reference!r} does not match configured host "
            f"{github_repository.host!r}."
        )
    match = _PULL_REQUEST_URL_RE.fullmatch(parsed.path)
    if match is None:
        raise AdoptResolutionError(
            f"Pull request URL {reference!r} is not a valid pull request URL."
        )
    if (
        match.group("owner") != github_repository.owner
        or match.group("repo") != github_repository.repo
    ):
        raise AdoptResolutionError(
            f"Pull request URL {reference!r} does not match configured repository "
            f"{github_repository.full_name!r}."
        )
    return int(match.group("number"))
