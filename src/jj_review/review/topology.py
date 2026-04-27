"""Per-change topology pointer comparisons and orphan-record discovery.

`pointer_disagreement` walks each stack and reports the change_ids whose saved
parent/head pointers no longer match the live DAG. The comparison is per change
— aggregating into a stack-level verdict would miss insert and abandon-mid-stack
rewrites.

`enumerate_orphaned_records` complements that: it finds saved records that have
fallen out of every live stack while their PR is still open. Those PRs have
become orphans the user must close explicitly through
`close --cleanup --pull-request <pr>`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalStack


@dataclass(frozen=True, slots=True)
class OrphanedRecord:
    """A saved tracking record whose change has left every live stack."""

    change_id: str
    cached_change: CachedChange


_OPEN_PR_STATES_FOR_ORPHANS = frozenset({"open", "draft"})


def is_open_pr_record(cached_change: CachedChange) -> bool:
    """Whether a saved record's PR is still open from the tool's perspective.

    Saved-state-only predicate: actively tracked link_state, has_review_identity
    populated, and pr_state is open or draft (None falls back to "still open").
    Does not check whether the change is gone from live stacks — callers that
    care about that half of the orphan predicate must filter first.
    """

    if not cached_change.is_tracked:
        return False
    pr_state = cached_change.pr_state
    if pr_state is None:
        return True
    return pr_state in _OPEN_PR_STATES_FOR_ORPHANS


def enumerate_orphaned_records(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[OrphanedRecord, ...]:
    """Return saved records whose change is no longer in any live stack.

    A record is reported when:

    - it has review identity (PR fields populated) and is link_state=active, and
    - its change_id does not appear in any of the supplied local stacks, and
    - the saved PR state is `open`/`draft` or unknown (treat None as still open).

    Records with a saved PR state of `closed` or `merged` are excluded — the PR
    is no longer live, so the user does not need to act on it as an orphan.
    Unlinked records are excluded too: the user already detached them.
    """

    live_change_ids: set[str] = set()
    for stack in local_stacks:
        for revision in stack.revisions:
            live_change_ids.add(revision.change_id)

    orphans: list[OrphanedRecord] = []
    for change_id, cached_change in state.changes.items():
        if change_id in live_change_ids:
            continue
        if not is_open_pr_record(cached_change):
            continue
        orphans.append(OrphanedRecord(change_id=change_id, cached_change=cached_change))
    return tuple(orphans)


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
