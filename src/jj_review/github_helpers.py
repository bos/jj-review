"""Shared command-side wrappers around GitHub client operations."""

from __future__ import annotations

from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github_resolution import ResolvedGithubRepository
from jj_review.models.github import GithubIssueComment, GithubRepository


async def load_github_repository(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
) -> GithubRepository:
    try:
        return await github_client.get_repository(
            github_repository.owner,
            github_repository.repo,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not load GitHub repository {github_repository.full_name}: {error}"
        ) from error


async def list_pull_request_issue_comments(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
    pull_request_number: int,
) -> tuple[GithubIssueComment, ...]:
    try:
        return await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not list stack summary comments for pull request "
            f"#{pull_request_number}: {error}"
        ) from error
