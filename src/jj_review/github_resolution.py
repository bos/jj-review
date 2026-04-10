"""Shared Git remote and GitHub target resolution helpers."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

from jj_review.config import RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient
from jj_review.models.bookmarks import BookmarkState, GitRemote
from jj_review.models.github import GithubRepository

_DEFAULT_GITHUB_HOST = "github.com"


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


def select_submit_remote(
    config: RepoConfig,
    remotes: tuple[GitRemote, ...],
) -> GitRemote:
    """Resolve the Git remote used by review commands."""

    remotes_by_name = {remote.name: remote for remote in remotes}
    if config.remote:
        remote = remotes_by_name.get(config.remote)
        if remote is None:
            raise CliError(
                f"Configured remote {config.remote!r} is not defined in this repository."
            )
        return remote
    if "origin" in remotes_by_name:
        return remotes_by_name["origin"]
    if len(remotes) == 1:
        return remotes[0]
    raise CliError(
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
    raise CliError(
        f"Could not determine the GitHub repository for remote {remote.name!r}. "
        "Configure `repo.github_owner` and `repo.github_repo`, or use a GitHub remote URL."
    )


def try_resolve_github_repository(
    config: RepoConfig,
    remote: GitRemote | None,
) -> tuple[ResolvedGithubRepository | None, str | None]:
    if remote is None:
        return None, None
    try:
        return resolve_github_repository(config, remote), None
    except CliError as error:
        return None, str(error)


def resolve_trunk_branch(
    *,
    client: BookmarkStateReader,
    config: RepoConfig,
    github_repository_state: GithubRepository,
    remote: GitRemote,
    stack: StackWithTrunk,
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
        raise CliError(
            "Could not determine the trunk branch because multiple remote bookmarks on "
            f"{remote.name!r} point at `trunk()`: {', '.join(remote_bookmarks)}."
        )
    raise CliError(
        f"Could not determine the trunk branch for remote {remote.name!r}. Configure "
        "`repo.trunk_branch`, ensure the GitHub repository exposes a default branch, or "
        "create one remote bookmark that points at `trunk()`."
    )


def build_github_client(*, base_url: str) -> GithubClient:
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
