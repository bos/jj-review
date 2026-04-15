"""Shared user-facing messages for status preparation failures."""

from __future__ import annotations

from jj_review.jj import UnsupportedStackError


def describe_status_preparation_error(error: UnsupportedStackError) -> str:
    """Describe an unsupported local stack shape for users."""

    if error.reason == "divergent_change" and error.change_id is not None:
        return (
            "Could not inspect review status because local history no longer forms a "
            f"supported linear stack. {error} Inspect the divergent revisions with "
            f"`jj log -r 'change_id({error.change_id})'` and reconcile them before "
            "retrying. "
            "This can happen after `status --fetch` or another fetch imports remote "
            "bookmark updates for merged PRs."
        )
    return (
        "Could not inspect review status because local history no longer forms a "
        f"supported linear stack. {error}"
    )
