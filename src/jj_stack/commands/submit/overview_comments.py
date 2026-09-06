"""Synchronize submit stack overview comments on GitHub pull requests."""

from __future__ import annotations

import jj_stack.console as console
from jj_stack.concurrency import run_bounded_tasks
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_LABEL,
    STACK_OVERVIEW_COMMENT_MARKER,
    delete_stack_overview_comment,
)
from jj_stack.models.github import GithubIssueComment

from .managed_comments import upsert_managed_comment
from .models import GeneratedDescription


async def sync_stack_overview_comments(
    *,
    base_is_another_pr: bool,
    comments_by_pr_number: dict[int, GithubIssueComment | None],
    concurrency: int,
    generated_stack_description: GeneratedDescription | None,
    github_client: GithubClient,
    pr_numbers: tuple[int, ...],
) -> None:
    """Synchronize the supplied stack-overview responsibilities."""

    overview_bodies = _stack_overview_comment_bodies(
        base_is_another_pr=base_is_another_pr,
        comments_by_pr_number=comments_by_pr_number,
        generated_stack_description=generated_stack_description,
        pr_numbers=pr_numbers,
    )
    head_pr_number = pr_numbers[-1]
    with console.progress(
        description="Syncing stack overview comments",
        total=len(pr_numbers),
    ) as progress:
        await _sync_overview_comment(
            comment_body=overview_bodies[head_pr_number],
            existing_comment=comments_by_pr_number[head_pr_number],
            github_client=github_client,
            pr_number=head_pr_number,
        )
        progress.advance()
        await run_bounded_tasks(
            concurrency=concurrency,
            items=pr_numbers[:-1],
            run_item=lambda pr_number: _sync_overview_comment(
                comment_body=overview_bodies[pr_number],
                existing_comment=comments_by_pr_number[pr_number],
                github_client=github_client,
                pr_number=pr_number,
            ),
            on_success=progress.advance,
        )


def _stack_overview_comment_bodies(
    *,
    base_is_another_pr: bool,
    comments_by_pr_number: dict[int, GithubIssueComment | None],
    generated_stack_description: GeneratedDescription | None,
    pr_numbers: tuple[int, ...],
) -> dict[int, str | None]:
    head_pr_number = pr_numbers[-1]
    overview_body = _stack_overview_body(
        comments_by_pr_number=comments_by_pr_number,
        generated_stack_description=generated_stack_description,
        head_pr_number=head_pr_number,
        # Only the selected pull requests are synchronized, so a single selected PR
        # stacked on another PR is still part of a larger stack.
        is_lone_pr=len(pr_numbers) <= 1 and not base_is_another_pr,
    )
    return {
        pr_number: overview_body if pr_number == head_pr_number else None
        for pr_number in pr_numbers
    }


def _stack_overview_body(
    *,
    comments_by_pr_number: dict[int, GithubIssueComment | None],
    generated_stack_description: GeneratedDescription | None,
    head_pr_number: int,
    is_lone_pr: bool,
) -> str | None:
    if is_lone_pr:
        return None
    if generated_stack_description is not None:
        description_lines = _render_generated_stack_description(generated_stack_description)
        return (
            "\n".join([STACK_OVERVIEW_COMMENT_MARKER, *description_lines])
            if description_lines
            else None
        )

    head_comment = comments_by_pr_number[head_pr_number]
    if head_comment is not None:
        return head_comment.body

    existing_bodies = {
        comment.body for comment in comments_by_pr_number.values() if comment is not None
    }
    if len(existing_bodies) > 1:
        raise CliError(
            "Could not preserve the stack overview because the selected pull requests "
            "have different managed comments."
        )
    return next(iter(existing_bodies), None)


async def _sync_overview_comment(
    *,
    comment_body: str | None,
    existing_comment: GithubIssueComment | None,
    github_client: GithubClient,
    pr_number: int,
) -> None:
    if comment_body is None:
        if existing_comment is None:
            return
        await delete_stack_overview_comment(
            comment_id=existing_comment.id,
            github_client=github_client,
        )
        return
    await upsert_managed_comment(
        body=comment_body,
        existing_comment=existing_comment,
        github_client=github_client,
        label=STACK_OVERVIEW_COMMENT_LABEL,
        pr_number=pr_number,
    )


def _render_generated_stack_description(
    stack_description: GeneratedDescription,
) -> list[str]:
    lines: list[str] = []
    if stack_description.title:
        lines.append(f"## {stack_description.title}")
    if stack_description.body:
        if lines:
            lines.append("")
        lines.extend(stack_description.body.splitlines())
    return lines
