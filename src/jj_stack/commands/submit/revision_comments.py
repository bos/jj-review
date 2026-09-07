"""Maintain a readable history of force-pushed pull request revisions."""

from __future__ import annotations

import jj_stack.console as console
from jj_stack.concurrency import run_bounded_tasks
from jj_stack.github.client import GithubClient
from jj_stack.identifiers import CommitId, short_commit_id
from jj_stack.models.github import GithubIssueComment, GithubPRRevision

from .managed_comments import upsert_managed_comment

REVISION_HISTORY_COMMENT_LABEL = "revision history comment"
REVISION_HISTORY_COMMENT_MARKER = "<!-- jj-stack-revision-history -->"
REVISION_HISTORY_VERSION_LIMIT = 20
type SubmittedForcePush = tuple[CommitId, CommitId]


async def sync_revision_history_comments(
    *,
    comments_by_pr_number: dict[int, GithubIssueComment | None],
    concurrency: int,
    revisions_by_pr: dict[int, tuple[GithubPRRevision, ...]],
    github_client: GithubClient,
    pr_numbers: tuple[int, ...],
    submitted_force_pushes_by_pr: dict[int, SubmittedForcePush],
) -> None:
    """Rebuild each selected PR's managed revision history from GitHub's timeline."""

    with console.progress(
        description="Syncing pull request revision history",
        total=len(pr_numbers),
    ) as progress:
        await run_bounded_tasks(
            concurrency=concurrency,
            items=pr_numbers,
            run_item=lambda pr_number: _sync_revision_history_comment(
                existing_comment=comments_by_pr_number[pr_number],
                github_client=github_client,
                pr_number=pr_number,
                revisions=_include_submitted_force_push(
                    revisions_by_pr[pr_number],
                    submitted_force_pushes_by_pr.get(pr_number),
                ),
            ),
            on_success=progress.advance,
        )


async def _sync_revision_history_comment(
    *,
    existing_comment: GithubIssueComment | None,
    revisions: tuple[GithubPRRevision, ...],
    github_client: GithubClient,
    pr_number: int,
) -> None:
    if not revisions:
        return
    body = _revision_history_body(
        revisions=revisions,
        repo_full_name=github_client.repo.full_name,
    )
    await upsert_managed_comment(
        body=body,
        existing_comment=existing_comment,
        github_client=github_client,
        label=REVISION_HISTORY_COMMENT_LABEL,
        pr_number=pr_number,
    )


def _include_submitted_force_push(
    revisions: tuple[GithubPRRevision, ...],
    submitted_force_push: SubmittedForcePush | None,
) -> tuple[GithubPRRevision, ...]:
    """Fill a just-pushed revision that GitHub has not indexed yet."""

    if submitted_force_push is None:
        return revisions
    before_commit_id, commit_id = submitted_force_push
    if any(
        revision.before_commit_id == before_commit_id and revision.commit_id == commit_id
        for revision in revisions
    ):
        return revisions
    if revisions and revisions[-1].commit_id != before_commit_id:
        return revisions
    prior_revisions = tuple(
        revision.model_copy(update={"is_current": False}) for revision in revisions
    )
    return (
        *prior_revisions,
        GithubPRRevision(
            before_commit_id=before_commit_id,
            commit_id=commit_id,
            is_current=True,
            version=revisions[-1].version + 1 if revisions else 2,
        ),
    )


def _revision_history_body(
    *,
    revisions: tuple[GithubPRRevision, ...],
    repo_full_name: str,
) -> str:
    revisions = revisions[-REVISION_HISTORY_VERSION_LIMIT:]
    repo_url = f"https://github.com/{repo_full_name}"
    rows = [
        REVISION_HISTORY_COMMENT_MARKER,
        "## Revision history",
        "",
        "| Version | Changes from previous version | Submitted commit |",
        "| --- | --- | --- |",
    ]
    for revision in reversed(revisions):
        version = f"{revision.version}{' (current)' if revision.is_current else ''}"
        changes = _markdown_link(
            f"{short_commit_id(revision.before_commit_id)}.."
            f"{short_commit_id(revision.commit_id)}",
            f"{repo_url}/compare/{revision.before_commit_id}..{revision.commit_id}",
        )
        submitted = _markdown_link(
            short_commit_id(revision.commit_id),
            f"{repo_url}/commit/{revision.commit_id}",
        )
        rows.append(f"| {version} | {changes} | {submitted} |")
    if revisions[0].version == 2 and len(revisions) < REVISION_HISTORY_VERSION_LIMIT:
        initial_commit = revisions[0].before_commit_id
        submitted = _markdown_link(
            short_commit_id(initial_commit),
            f"{repo_url}/commit/{initial_commit}",
        )
        rows.append(f"| 1 | Initial version | {submitted} |")
    history_note = (
        f"Showing the {REVISION_HISTORY_VERSION_LIMIT} most recent available versions. "
        if revisions[0].version > 2 or len(revisions) == REVISION_HISTORY_VERSION_LIMIT
        else ""
    )
    rows.extend(
        (
            "",
            f"<sub>{history_note}Generated by jj-stack from this pull request's "
            "force-push history.</sub>",
        )
    )
    return "\n".join(rows)


def _markdown_link(text: str, url: str) -> str:
    return f"[`{text}`]({url})"
