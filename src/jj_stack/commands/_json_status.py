"""JSON projections for user-facing stack status."""

from __future__ import annotations

from typing import assert_never

from jj_stack.models.github import GithubPR
from jj_stack.models.tracking import PRIdentity
from jj_stack.stack.change_state import (
    BranchClaimed,
    ChangeState,
    Closed,
    CompetingOpenPR,
    Landed,
    LookupFailed,
    Merged,
    NotInspected,
    PRAmbiguous,
    PRHeadMoved,
    PRMissing,
    Queued,
    Unpublished,
    UntrackedPRExists,
    WithPR,
)
from jj_stack.stack.status import StackStatusChange


def stack_change_json(
    change: StackStatusChange,
    *,
    current: bool = False,
) -> dict[str, object]:
    """Return the public JSON shape for one stack change."""

    payload: dict[str, object] = {
        "change_id": change.change_id,
        "status": _change_status(change.state),
        "subject": change.subject,
    }
    if change.branch is not None:
        payload["branch"] = change.branch
    if current:
        payload["current"] = True
    pr = pr_json(change)
    if pr is not None:
        payload["pr"] = pr
    return payload


def pr_json(
    change: StackStatusChange,
) -> dict[str, object] | None:
    pr = change.pr
    if pr is not None:
        return _json_object(
            {
                "checks": pr.check_rollup_status,
                "number": pr.number,
                "url": pr.html_url,
            }
        )
    return saved_pr_json(change.tracked.pr_identity if change.tracked is not None else None)


def saved_pr_json(
    pr_identity: PRIdentity | None,
) -> dict[str, object] | None:
    if pr_identity is None:
        return None
    payload = _json_object({"number": pr_identity.pr_number})
    return payload or None


def _change_status(state: ChangeState) -> str:
    if state.divergent:
        return "divergent"
    match state:
        case PRHeadMoved():
            return "branch_moved"
        case PRAmbiguous():
            return "ambiguous"
        case CompetingOpenPR(ambiguous=True):
            return "ambiguous"
        case PRMissing():
            return "missing"
        case LookupFailed():
            return "unknown"
        case Landed() | Merged():
            return "merged"
        case Closed():
            return "closed"
        case Queued():
            return "queued"
        case NotInspected():
            return "submitted"
        case Unpublished() | UntrackedPRExists() | BranchClaimed():
            return "unsubmitted"
        case WithPR(pr=pr):
            return _live_pr_status(pr)
        case _:
            assert_never(state)


def _live_pr_status(pr: GithubPR) -> str:
    if pr.state != "open":
        return pr.state
    if pr.is_queued:
        return "queued"
    if pr.is_draft:
        return "draft"
    if pr.review_decision in {"approved", "changes_requested"}:
        return pr.review_decision
    return "open"


def _json_object(values: dict[str, object | None]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}
