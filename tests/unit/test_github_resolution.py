from __future__ import annotations

from types import SimpleNamespace

import pytest

import jj_review.github.client as github_client_module
from jj_review.errors import CliError
from jj_review.github.client import (
    _github_hostname_from_api_base_url,
    _github_token_for_base_url,
    _github_token_from_gh_cli,
    build_github_client,
    github_token_from_env,
)
from jj_review.github.resolution import (
    parse_github_repo,
    resolve_trunk_branch,
    select_submit_remote,
)
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import GithubRepository
from jj_review.models.stack import LocalRevision, LocalStack


def test_select_submit_remote_uses_origin_when_multiple_remotes_exist() -> None:
    remote = select_submit_remote(
        (
            GitRemote(name="origin", url="git@example.com:org/repo.git"),
            GitRemote(name="backup", url="git@example.com:org/repo.git"),
        ),
    )

    assert remote.name == "origin"


def test_select_submit_remote_uses_only_remote_when_unambiguous() -> None:
    remote = select_submit_remote(
        (GitRemote(name="upstream", url="git@example.com:org/repo.git"),)
    )

    assert remote.name == "upstream"


def test_select_submit_remote_rejects_ambiguous_remote_set_without_origin() -> None:
    with pytest.raises(
        CliError,
        match="Could not determine which Git remote to use for submit",
    ):
        select_submit_remote(
            (
                GitRemote(name="backup", url="git@example.com:org/repo.git"),
                GitRemote(name="upstream", url="git@example.com:org/repo.git"),
            ),
        )


def test_select_submit_remote_rejects_empty_remote_list() -> None:
    with pytest.raises(
        CliError,
        match="Could not determine which Git remote to use for submit",
    ):
        select_submit_remote(())


def test_parse_github_repo_parses_https_remote_url() -> None:
    repository = parse_github_repo(
        GitRemote(
            name="origin",
            url="https://github.test/octo-org/stacked-review.git",
        ),
    )

    assert repository is not None
    assert repository.host == "github.test"
    assert repository.owner == "octo-org"
    assert repository.repo == "stacked-review"


def test_parse_github_repo_returns_none_for_unparseable_remote() -> None:
    assert parse_github_repo(GitRemote(name="origin", url="/tmp/remote.git")) is None


def test_github_token_from_env_prefers_github_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("GH_TOKEN", "gh-token")

    assert github_token_from_env() == "github-token"


def test_github_token_from_env_falls_back_to_gh_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gh-token")

    assert github_token_from_env() == "gh-token"


def test_build_github_client_reads_token_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")

    client = build_github_client(base_url="https://api.github.test")
    try:
        assert client._client.headers["Authorization"] == "Bearer github-token"
    finally:
        import asyncio

        asyncio.run(client.aclose())


def test_github_hostname_from_api_base_url_maps_public_github() -> None:
    assert _github_hostname_from_api_base_url("https://api.github.com") == "github.com"


def test_github_hostname_from_api_base_url_strips_api_prefix() -> None:
    assert (
        _github_hostname_from_api_base_url("https://api.github.example.com")
        == "github.example.com"
    )


def test_github_token_from_gh_cli_returns_none_when_gh_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(github_client_module.subprocess, "run", raise_missing)

    assert _github_token_from_gh_cli("github.com") is None


def test_github_token_for_base_url_falls_back_to_gh_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    calls: list[list[str]] = []

    def fake_run(command, *, capture_output, check, text):
        calls.append(list(command))
        return SimpleNamespace(returncode=0, stdout="gh-token\n")

    monkeypatch.setattr(github_client_module.subprocess, "run", fake_run)

    assert _github_token_for_base_url("https://api.github.com") == "gh-token"
    assert calls == [["gh", "auth", "token", "--hostname", "github.com"]]


def test_github_token_for_base_url_prefers_environment_over_gh_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("gh auth token should not be called when env token exists")

    monkeypatch.setattr(github_client_module.subprocess, "run", fail_if_called)

    assert _github_token_for_base_url("https://api.github.com") == "github-token"


def test_resolve_trunk_branch_uses_repository_default_branch() -> None:
    branch = resolve_trunk_branch(
        client=_FakeJjClient({}),
        github_repository_state=_github_repository(default_branch="main"),
        remote=GitRemote(name="origin", url="git@example.com:org/repo.git"),
        stack=_stack("trunk123"),
    )

    assert branch == "main"


def test_resolve_trunk_branch_falls_back_to_unique_remote_bookmark() -> None:
    branch = resolve_trunk_branch(
        client=_FakeJjClient(
            {
                "main": BookmarkState(
                    name="main",
                    remote_targets=(RemoteBookmarkState(remote="origin", targets=("trunk123",)),),
                )
            }
        ),
        github_repository_state=_github_repository(default_branch=""),
        remote=GitRemote(name="origin", url="git@example.com:org/repo.git"),
        stack=_stack("trunk123"),
    )

    assert branch == "main"


def test_resolve_trunk_branch_rejects_ambiguous_remote_bookmarks() -> None:
    with pytest.raises(
        CliError,
        match="multiple remote bookmarks",
    ):
        resolve_trunk_branch(
            client=_FakeJjClient(
                {
                    "main": BookmarkState(
                        name="main",
                        remote_targets=(
                            RemoteBookmarkState(remote="origin", targets=("trunk123",)),
                        ),
                    ),
                    "stable": BookmarkState(
                        name="stable",
                        remote_targets=(
                            RemoteBookmarkState(remote="origin", targets=("trunk123",)),
                        ),
                    ),
                }
            ),
            github_repository_state=_github_repository(default_branch=""),
            remote=GitRemote(name="origin", url="git@example.com:org/repo.git"),
            stack=_stack("trunk123"),
        )


class _FakeJjClient:
    def __init__(self, states: dict[str, BookmarkState]) -> None:
        self._states = states

    def list_bookmark_states(self) -> dict[str, BookmarkState]:
        return self._states


def _github_repository(default_branch: str) -> GithubRepository:
    return GithubRepository(
        clone_url="https://github.test/octo-org/repo.git",
        default_branch=default_branch,
        full_name="octo-org/repo",
        html_url="https://github.test/octo-org/repo",
        name="repo",
        private=True,
        url="https://api.github.test/repos/octo-org/repo",
    )


def _stack(trunk_commit_id: str) -> LocalStack:
    trunk = LocalRevision(
        change_id="trunk-change",
        commit_id=trunk_commit_id,
        current_working_copy=False,
        description="trunk",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("root",),
    )
    return LocalStack(
        head=trunk,
        revisions=(),
        selected_revset="trunk()",
        trunk=trunk,
    )
