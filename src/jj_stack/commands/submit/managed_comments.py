"""Shared synchronization for comments managed by submit."""

from __future__ import annotations

from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_number
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.models.github import GithubIssueComment


async def upsert_managed_comment(
    *,
    body: str,
    existing_comment: GithubIssueComment | None,
    github_client: GithubClient,
    label: str,
    pr_number: int,
) -> GithubIssueComment:
    if existing_comment is not None and existing_comment.body == body:
        return existing_comment
    action = "create" if existing_comment is None else "update"
    try:
        if existing_comment is None:
            return await github_client.create_issue_comment(
                issue_number=pr_number,
                body=body,
            )
        return await github_client.update_issue_comment(
            comment_id=existing_comment.id,
            body=body,
        )
    except GithubClientError as error:
        article = "a " if action == "create" else ""
        pr_label = format_pr_number(pr_number, repo=github_client.repo)
        raise CliError(
            f"Could not {action} {article}{label} for pull request {pr_label}"
        ) from error
