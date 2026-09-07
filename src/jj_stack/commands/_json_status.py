"""JSON projections for user-facing stack status."""

from __future__ import annotations

from jj_stack.models.tracking import PRIdentity
from jj_stack.stack.reporting import report_change
from jj_stack.stack.status import StackStatusChange


def stack_change_json(
    change: StackStatusChange,
    *,
    current: bool = False,
) -> dict[str, object]:
    """Return the public JSON shape for one stack change."""

    payload: dict[str, object] = {
        "change_id": change.change_id,
        "status": report_change(change.state).status,
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
    return saved_pr_json(change.tracked.pr_identity) if change.tracked is not None else None


def saved_pr_json(
    pr_identity: PRIdentity,
) -> dict[str, object]:
    return {"number": pr_identity.pr_number}


def _json_object(values: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in values.items() if value is not None}
