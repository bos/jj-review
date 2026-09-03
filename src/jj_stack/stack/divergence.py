"""Shared recovery guidance for divergent jj changes."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.ui import Message


def divergence_recovery_hint(
    change_id: str,
    *,
    retry: Message | None = None,
) -> Message:
    """Tell the user how to converge one divergent change."""

    command = ui.cmd(f"jj converge -r 'change_id({change_id})'")
    action = t"Run {command} to resolve the divergence"
    if retry is None:
        return t"{action}."
    return t"{action}, then {retry}."
