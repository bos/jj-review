"""Load and synchronize the managed comments produced by submit."""

from __future__ import annotations

import jj_stack.console as console
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import STACK_OVERVIEW_COMMENT_MARKER

from .models import GeneratedDescription
from .overview_comments import sync_stack_overview_comments
from .revision_comments import (
    REVISION_HISTORY_COMMENT_MARKER,
    REVISION_HISTORY_VERSION_LIMIT,
    SubmittedForcePush,
    sync_revision_history_comments,
)


async def sync_submit_comments(
    *,
    concurrency: int,
    generated_stack_description: GeneratedDescription | None,
    github_client: GithubClient,
    pr_numbers: tuple[int, ...],
    submitted_force_pushes_by_pr: dict[int, SubmittedForcePush],
) -> None:
    """Observe both comment kinds in one batch, then synchronize them."""

    if not pr_numbers:
        return
    with console.spinner(description="Loading pull request comments"):
        try:
            (
                comments_by_marker,
                revisions_by_pr,
            ) = await github_client.find_issue_comments_and_revisions(
                body_markers=(
                    STACK_OVERVIEW_COMMENT_MARKER,
                    REVISION_HISTORY_COMMENT_MARKER,
                ),
                pr_numbers=pr_numbers,
                revision_limit=REVISION_HISTORY_VERSION_LIMIT,
            )
        except GithubClientError as error:
            raise CliError("Could not load pull request comments") from error

    await sync_stack_overview_comments(
        comments_by_pr_number=comments_by_marker[STACK_OVERVIEW_COMMENT_MARKER],
        concurrency=concurrency,
        generated_stack_description=generated_stack_description,
        github_client=github_client,
        pr_numbers=pr_numbers,
    )
    await sync_revision_history_comments(
        comments_by_pr_number=comments_by_marker[REVISION_HISTORY_COMMENT_MARKER],
        concurrency=concurrency,
        github_client=github_client,
        pr_numbers=pr_numbers,
        revisions_by_pr=revisions_by_pr,
        submitted_force_pushes_by_pr=submitted_force_pushes_by_pr,
    )
