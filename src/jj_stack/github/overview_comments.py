"""Shared helpers for GitHub stack overview comments."""

from __future__ import annotations

from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError

STACK_OVERVIEW_COMMENT_LABEL = "stack overview comment"
STACK_OVERVIEW_COMMENT_MARKER = "<!-- jj-stack-overview -->"


async def delete_stack_overview_comment(
    *,
    comment_id: int,
    github_client: GithubClient,
) -> bool:
    """Delete one planned managed comment, accepting an already-absent target."""

    try:
        await github_client.delete_issue_comment(
            comment_id=comment_id,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return False
        raise CliError(
            f"Could not delete {STACK_OVERVIEW_COMMENT_LABEL} #{comment_id}"
        ) from error
    return True
