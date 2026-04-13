"""Check jj-review's configuration and connectivity.

Runs a series of read-only checks and prints a status line for each. Nothing
is changed. Exit status is 0 if all checks pass or warn; 1 if any check fails.

Checks run in order:

- a Git remote can be resolved to a single remote
- the remote URL points at a GitHub repository
- a GitHub token is available via environment variable or the gh CLI
- jj-review can reach the GitHub API and access the repository
- the trunk branch can be determined from the GitHub repository
- no interrupted operations are waiting for recovery
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Any, Literal

from jj_review import ui
from jj_review.bootstrap import bootstrap_context
from jj_review.cache import ReviewStateStore
from jj_review.errors import CliError
from jj_review.github.client import (
    GithubClient,
    _github_token_for_base_url,
    github_token_from_env,
)
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    select_submit_remote,
)
from jj_review.intent import describe_intent, pid_is_alive
from jj_review.jj import JjClient
from jj_review.models.bookmarks import GitRemote
from jj_review.models.github import GithubRepository

HELP = "check GitHub auth, remote resolution, and local state"
_STATUS_STYLES: dict[str, str] = {
    "ok": "green",
    "warn": "yellow",
    "fail": "red",
    "skip": "dim",
}


@dataclass(slots=True, frozen=True)
class CheckResult:
    label: str
    status: Literal["ok", "warn", "fail", "skip"]
    detail: str


def doctor(
    *,
    config_path: Path | None,
    debug: bool,
    repository: Path | None,
) -> int:
    """CLI entrypoint for `doctor`."""
    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    results = asyncio.run(_run_checks(repo_root=context.repo_root))
    ui.output(_results_table(results))
    return 1 if any(r.status == "fail" for r in results) else 0


async def _run_checks(*, repo_root: Path) -> list[CheckResult]:
    results: list[CheckResult] = []

    # Check 1: Git remote selection
    jj_client = JjClient(repo_root)
    remote_result, selected_remote = _check_git_remote(jj_client)
    results.append(remote_result)

    if selected_remote is None:
        results.extend(_skipped("GitHub remote", "GitHub auth", "connectivity", "trunk branch"))
        results.append(_check_interruptions(ReviewStateStore.for_repo(repo_root)))
        return results

    # Check 2: GitHub remote parsing
    github_result, parsed_repo = _check_github_remote(selected_remote)
    results.append(github_result)

    if parsed_repo is None:
        results.extend(_skipped("GitHub auth", "connectivity", "trunk branch"))
        results.append(_check_interruptions(ReviewStateStore.for_repo(repo_root)))
        return results

    # Check 3: GitHub auth
    auth_result, token = _check_github_auth(parsed_repo.api_base_url)
    results.append(auth_result)

    if token is None:
        results.extend(_skipped("connectivity", "trunk branch"))
        results.append(_check_interruptions(ReviewStateStore.for_repo(repo_root)))
        return results

    # Checks 4 & 5: Connectivity and trunk branch
    connectivity_result, github_repo = await _check_github_connectivity(
        parsed_repo=parsed_repo,
        token=token,
    )
    results.append(connectivity_result)

    if github_repo is not None:
        results.append(_check_trunk_branch(github_repo))
    else:
        results.append(CheckResult("trunk branch", "skip", "connectivity failed"))

    # Check 6: Interrupted operations
    results.append(_check_interruptions(ReviewStateStore.for_repo(repo_root)))
    return results


def _skipped(*labels: str) -> list[CheckResult]:
    return [CheckResult(label, "skip", "prior check failed") for label in labels]


def _check_git_remote(jj_client: JjClient) -> tuple[CheckResult, GitRemote | None]:
    try:
        remotes = jj_client.list_git_remotes()
    except Exception as error:
        return CheckResult("remote", "fail", f"could not list remotes: {error}"), None

    if not remotes:
        return (
            CheckResult(
                "remote",
                "fail",
                "no Git remotes configured; run `jj git remote add origin <url>` to add one",
            ),
            None,
        )

    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        return CheckResult("remote", "fail", str(error)), None

    return CheckResult("remote", "ok", remote.name), remote


def _check_github_remote(remote: GitRemote) -> tuple[CheckResult, ParsedGithubRepo | None]:
    parsed = parse_github_repo(remote)
    if parsed is None:
        return (
            CheckResult(
                "GitHub remote",
                "fail",
                f"remote {remote.name!r} does not look like a GitHub URL: {remote.url}"
                "; use a GitHub HTTPS or SSH remote URL",
            ),
            None,
        )
    return CheckResult("GitHub remote", "ok", f"{parsed.host}/{parsed.full_name}"), parsed


def _check_github_auth(base_url: str) -> tuple[CheckResult, str | None]:
    env_token = github_token_from_env()
    if env_token:
        env_var = "GITHUB_TOKEN" if os.environ.get("GITHUB_TOKEN") else "GH_TOKEN"
        return CheckResult("GitHub auth", "ok", f"token found ({env_var})"), env_token

    # Env vars not set — try the gh CLI
    token = _github_token_for_base_url(base_url)
    if token:
        return CheckResult("GitHub auth", "ok", "token found (gh CLI)"), token

    return (
        CheckResult(
            "GitHub auth",
            "fail",
            "no token found; set GITHUB_TOKEN or run `gh auth login`",
        ),
        None,
    )


async def _check_github_connectivity(
    *,
    parsed_repo: ParsedGithubRepo,
    token: str,
) -> tuple[CheckResult, GithubRepository | None]:
    # Use the token already resolved by the auth check rather than re-invoking
    # the gh CLI. GithubClient is a module-level name so tests can patch it.
    async with GithubClient(base_url=parsed_repo.api_base_url, token=token) as client:
        try:
            github_repo = await client.get_repository(parsed_repo.owner, parsed_repo.repo)
        except Exception as error:
            return (
                CheckResult(
                    "connectivity",
                    "fail",
                    f"could not reach {parsed_repo.host}/{parsed_repo.full_name}: {error}"
                    "; check network connectivity and that the repository exists",
                ),
                None,
            )
    return (
        CheckResult(
            "connectivity",
            "ok",
            f"reached {parsed_repo.host}/{parsed_repo.full_name}",
        ),
        github_repo,
    )


def _check_trunk_branch(github_repo: GithubRepository) -> CheckResult:
    if github_repo.default_branch:
        return CheckResult("trunk branch", "ok", github_repo.default_branch)
    return CheckResult(
        "trunk branch",
        "warn",
        "GitHub repository has no default branch set"
        "; set a default branch on GitHub or configure `trunk()` in jj",
    )


def _check_interruptions(state_store: ReviewStateStore) -> CheckResult:
    if not state_store.state_dir.exists():
        return CheckResult("interruptions", "ok", "none")
    try:
        all_intents = state_store.list_intents()
    except Exception as error:
        return CheckResult("interruptions", "warn", f"could not check: {error}")

    # Ignore intents whose process is still alive — those are active operations,
    # not interrupted ones. Only dead-PID intents need recovery.
    interrupted = [
        loaded for loaded in all_intents if not pid_is_alive(loaded.intent.pid)
    ]

    if not interrupted:
        return CheckResult("interruptions", "ok", "none")

    labels = [describe_intent(loaded.intent) for loaded in interrupted]
    count = len(labels)
    noun = "interrupted operation" if count == 1 else "interrupted operations"
    return CheckResult(
        "interruptions",
        "warn",
        f"{count} {noun}: {', '.join(labels)}"
        "; run `jj-review abort --dry-run` to preview recovery",
    )


def _results_table(results: list[CheckResult]) -> Any:
    table_cls = import_module("rich.table").Table
    text_cls = import_module("rich.text").Text
    table = table_cls(
        box=import_module("rich.box").SIMPLE,
        show_header=True,
        header_style="bold",
    )
    table.add_column("Check")
    table.add_column("Status", no_wrap=True)
    table.add_column("Detail")

    for result in results:
        table.add_row(
            result.label,
            text_cls(result.status, style=_STATUS_STYLES[result.status]),
            result.detail,
        )
    return table
