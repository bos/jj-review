"""Shared types and rendering helpers for close command action rows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jj_review import ui
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github.error_messages import summarize_github_error_reason
from jj_review.github.resolution import ParsedGithubRepo
from jj_review.github.stack_comments import (
    StackCommentKind,
    is_navigation_comment,
    is_overview_comment,
    stack_comment_label,
)
from jj_review.models.github import GithubIssueComment
from jj_review.models.review_state import CachedChange
from jj_review.ui import Message, plain_text

CloseActionStatus = Literal["applied", "blocked", "planned"]
type CloseActionBody = Message


@dataclass(frozen=True, slots=True)
class BookmarkCleanupPlan:
    """Resolved bookmark cleanup actions for one cached change."""

    local_forget: bool
    remote_delete: bool


@dataclass(frozen=True, slots=True)
class CloseAction:
    """One close action that was planned, applied, or blocked."""

    kind: str
    status: CloseActionStatus
    body: CloseActionBody

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


NAVIGATION_COMMENT_KIND = stack_comment_label("navigation")
OVERVIEW_COMMENT_KIND = stack_comment_label("overview")


def comment_matches_kind(*, body: str, kind: StackCommentKind) -> bool:
    if kind == "navigation":
        return is_navigation_comment(body)
    return is_overview_comment(body)


async def find_managed_comment(
    *,
    cached_comment_id: int | None,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    kind: StackCommentKind,
    pull_request_number: int,
) -> tuple[GithubIssueComment | None, CloseAction | None]:
    """Resolve the saved jj-review stack comment for a PR, if any."""

    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            if cached_comment_id is None:
                return None, None
            try:
                cached_comment = await github_client.get_issue_comment(
                    github_repository.owner,
                    github_repository.repo,
                    comment_id=cached_comment_id,
                )
            except GithubClientError as cached_comment_error:
                if cached_comment_error.status_code == 404:
                    return None, None
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot inspect saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id}: "
                            f"{summarize_github_error_reason(cached_comment_error)}"
                        ),
                        status="blocked",
                    ),
                )
            if not comment_matches_kind(body=cached_comment.body, kind=kind):
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot delete saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id} because it does not belong to "
                            "jj-review"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None
        return (
            None,
            CloseAction(
                kind=stack_comment_label(kind),
                body=(
                    f"cannot inspect {stack_comment_label(kind)}s for PR "
                    f"#{pull_request_number}: {summarize_github_error_reason(error)}"
                ),
                status="blocked",
            ),
        )

    if cached_comment_id is not None:
        cached_comment = next(
            (comment for comment in comments if comment.id == cached_comment_id),
            None,
        )
        if cached_comment is not None:
            if not comment_matches_kind(body=cached_comment.body, kind=kind):
                return (
                    None,
                    CloseAction(
                        kind=stack_comment_label(kind),
                        body=(
                            f"cannot delete saved {stack_comment_label(kind)} "
                            f"#{cached_comment_id} because it does not belong to "
                            "jj-review"
                        ),
                        status="blocked",
                    ),
                )
            return cached_comment, None

    matching_comments = [
        comment for comment in comments if comment_matches_kind(body=comment.body, kind=kind)
    ]
    if len(matching_comments) > 1:
        return (
            None,
            CloseAction(
                kind=stack_comment_label(kind),
                body=(
                    f"cannot delete {stack_comment_label(kind)}s because GitHub reports "
                    f"multiple candidates on PR #{pull_request_number}"
                ),
                status="blocked",
            ),
        )
    if not matching_comments:
        return None, None
    return matching_comments[0], None


def render_close_action_message(action: CloseAction) -> CloseActionBody:
    if action.kind == "tracking":
        return action.body
    return (ui.semantic_text(action.kind, "prefix"), ": ", action.body)


def close_action_presentation(
    status: CloseActionStatus,
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...] | None]:
    if status == "applied":
        return (
            "  ✓",
            ("signature status good",),
            None,
        )
    if status == "planned":
        return (
            "  ~",
            ("hint heading",),
            None,
        )
    if status == "blocked":
        return (
            "  ✗",
            ("error heading",),
            ("warning heading",),
        )
    return ("  ?", None, None)


def retire_cached_change(
    cached_change: CachedChange,
    *,
    pr_state: str,
) -> CachedChange:
    # Closed changes remain "active" unless they were explicitly unlinked. The saved
    # jj-review data still needs the last known review identity so later cleanup or
    # status refresh can reason about the already-closed stack without reattaching it.
    updates: dict[str, object] = {
        "pr_review_decision": None,
        "pr_state": pr_state,
    }
    return cached_change.model_copy(update=updates)
