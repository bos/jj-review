"""Shared Git remote and GitHub target resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from jj_review.errors import CliError
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.github import GithubRepository

_DEFAULT_GITHUB_HOST = "github.com"


@dataclass(frozen=True, slots=True)
class ParsedGithubRepo:
    """GitHub repository coordinates parsed from a Git remote URL."""

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


class BookmarkStateReader(Protocol):
    """Minimal bookmark reader needed for trunk resolution."""

    def list_bookmark_states(self) -> dict[str, BookmarkState]: ...


class _TrunkRevisionLike(Protocol):
    """Minimal trunk revision shape needed for base-branch fallback."""

    @property
    def commit_id(self) -> str: ...


class StackWithTrunk(Protocol):
    """Minimal stack shape needed for resolving the trunk bookmark."""

    @property
    def trunk(self) -> _TrunkRevisionLike: ...


def select_submit_remote(remotes: tuple[GitRemote, ...]) -> GitRemote:
    """Resolve the Git remote used by review commands."""

    remotes_by_name = {remote.name: remote for remote in remotes}
    if "origin" in remotes_by_name:
        return remotes_by_name["origin"]
    if len(remotes) == 1:
        return remotes[0]
    raise CliError(
        "Could not determine which Git remote to use for submit. Add an `origin` "
        "remote or leave exactly one remote."
    )


def parse_github_repo(remote: GitRemote) -> ParsedGithubRepo | None:
    """Parse a GitHub repository target from a Git remote URL."""

    parsed = urlparse(remote.url)
    if parsed.scheme in {"http", "https", "ssh"} and parsed.hostname:
        host = parsed.hostname
        raw_path = parsed.path
    elif parsed.scheme == "" and ":" in remote.url and "@" in remote.url.partition(":")[0]:
        host, _, raw_path = remote.url.partition(":")
        host = host.rsplit("@", maxsplit=1)[-1]
    else:
        return None

    normalized_path = raw_path.lstrip("/").removesuffix(".git")
    parts = [part for part in normalized_path.split("/") if part]
    if len(parts) != 2:
        return None
    owner, repo = parts
    return ParsedGithubRepo(host=host, owner=owner, repo=repo)


def resolve_trunk_branch(
    *,
    client: BookmarkStateReader,
    github_repository_state: GithubRepository,
    remote: GitRemote,
    stack: StackWithTrunk,
) -> str:
    """Resolve the GitHub base branch used for bottom-of-stack pull requests."""

    if github_repository_state.default_branch:
        return github_repository_state.default_branch

    remote_bookmarks = remote_bookmarks_pointing_at_trunk(
        client=client,
        remote_name=remote.name,
        trunk_commit_id=stack.trunk.commit_id,
    )
    if len(remote_bookmarks) == 1:
        return remote_bookmarks[0]
    if len(remote_bookmarks) > 1:
        raise CliError(
            "Could not determine the trunk branch because multiple remote bookmarks on "
            f"{remote.name!r} point at `trunk()`: {', '.join(remote_bookmarks)}."
        )
    raise CliError(
        f"Could not determine the trunk branch for remote {remote.name!r}. Ensure the "
        "GitHub repository exposes a default branch or create one remote bookmark that "
        "points at `trunk()`."
    )

def remote_bookmarks_pointing_at_trunk(
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
