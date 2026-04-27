"""Per-change topology pointer comparisons against the live DAG.

The pointers are written by `submit` and `land` (see CachedChange) and describe the
parent and head of the chain on the most recent successful submission. This module
compares them against the current stack walk one change at a time so that callers
can surface a `needs submit` advisory.

The comparison is intentionally per change, never aggregated into a stack-level
verdict — aggregation would miss the insert and abandon-mid-stack rewrites that
the design specifically targets.
"""

from __future__ import annotations

from collections.abc import Sequence

from jj_review.models.review_state import ReviewState
from jj_review.models.stack import LocalStack


def pointer_disagreement(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[str, ...]:
    """Return change_ids whose saved pointers disagree with their live position.

    For each revision on each stack, compare the saved record's
    `last_submitted_parent_change_id` and `last_submitted_stack_head_change_id`
    to the live values implied by the current stack walk. A revision is reported
    when at least one pointer is populated in saved state and either differs.

    Records with both pointers unset, unlinked records, and revisions whose
    change_id has no saved record are skipped — lack of pointers is lack of
    evidence of staleness, not evidence of staleness.
    """

    disagreements: list[str] = []
    for stack in local_stacks:
        if not stack.revisions:
            continue
        live_head = stack.revisions[-1].change_id
        for index, revision in enumerate(stack.revisions):
            cached = state.changes.get(revision.change_id)
            if cached is None or cached.is_unlinked:
                continue
            saved_parent = cached.last_submitted_parent_change_id
            saved_head = cached.last_submitted_stack_head_change_id
            if saved_parent is None and saved_head is None:
                continue
            live_parent = (
                stack.revisions[index - 1].change_id if index > 0 else None
            )
            if saved_parent != live_parent or saved_head != live_head:
                disagreements.append(revision.change_id)
    return tuple(disagreements)
