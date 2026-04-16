"""Shared user-facing messages for status preparation failures."""

from __future__ import annotations

from jj_review import ui
from jj_review.jj import UnsupportedStackError
from jj_review.ui import Message


def describe_status_preparation_error(error: UnsupportedStackError) -> Message:
    """Describe an unsupported local stack shape for users."""

    if error.reason == "divergent_change" and error.change_id is not None:
        return t"Could not inspect review status because local history no longer forms a " \
            t"supported linear stack. {error} Inspect the divergent revisions with " \
            t"{ui.cmd('jj log -r')} {ui.revset(f'change_id({error.change_id})')} and " \
            t"reconcile them before retrying. This can happen after " \
            t"{ui.cmd('status --fetch')} or another fetch imports remote bookmark " \
            t"updates for merged PRs."
    return (
        t"Could not inspect review status because local history no longer forms a "
        t"supported linear stack. {error}"
    )
