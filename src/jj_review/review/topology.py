"""Per-change submitted-state comparisons and orphan-record discovery.

`submitted_state_disagreement` walks each stack and reports the change_ids whose
saved submitted commit or parent/head pointers no longer match the live DAG. The
comparison is per change — aggregating into a stack-level verdict would miss
same-position rewrites, inserts, and abandon-mid-stack rewrites.

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


@dataclass(frozen=True, slots=True)
class SubmittedStateDisagreement:
    """One tracked change whose saved submit baseline no longer matches the DAG."""

    change_id: str
    commit_changed: bool = False
    parent_changed: bool = False
    stack_head_changed: bool = False


_OPEN_PR_STATES_FOR_ORPHANS = frozenset({"open", "draft"})


def is_open_pr_record(cached_change: CachedChange) -> bool:
    """Whether a saved record's PR is still open from the tool's perspective.

    Saved-state-only predicate: actively tracked link_state, has_review_identity
    populated, a saved PR number, and pr_state is open or draft (None falls
    back to "still open" only when a saved PR number makes the record
    actionable).
    Does not check whether the change is gone from live stacks — callers that
    care about that half of the orphan predicate must filter first.
    """

    if not cached_change.is_tracked:
        return False
    if cached_change.pr_number is None:
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
    - the record has a saved PR number, and
    - the saved PR state is `open`/`draft` or unknown (treat None as still open).

    Records with a saved PR state of `closed` or `merged` are excluded — the PR
    is no longer live, so the user does not need to act on it as an orphan.
    Records without a saved PR number are excluded too: there is no concrete PR
    identity for `close --cleanup --pull-request` to retire. Unlinked records
    are excluded too: the user already detached them.
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


def submitted_state_disagreement(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[str, ...]:
    return tuple(
        disagreement.change_id
        for disagreement in submitted_state_disagreements(state, local_stacks)
    )


def submitted_state_disagreements(
    state: ReviewState,
    local_stacks: Sequence[LocalStack],
) -> tuple[SubmittedStateDisagreement, ...]:
    """Return change_ids whose saved submitted state disagrees with the live DAG.

    For each revision on each stack, compare the saved record's
    `last_submitted_commit_id`, `last_submitted_parent_change_id`, and
    `last_submitted_stack_head_change_id` to the live values implied by the
    current stack walk. A revision is reported when the saved commit id differs
    from the current commit, or when at least one saved topology pointer is
    populated and either pointer differs.

    Records with no submitted commit or pointers, unlinked records, and
    revisions whose change_id has no saved record are skipped — lack of a saved
    submitted baseline is lack of evidence of staleness, not evidence of
    staleness.
    """

    disagreements: list[SubmittedStateDisagreement] = []
    for stack in local_stacks:
        if not stack.revisions:
            continue
        live_head = stack.revisions[-1].change_id
        for index, revision in enumerate(stack.revisions):
            cached = state.changes.get(revision.change_id)
            if cached is None or cached.is_unlinked:
                continue
            commit_changed = _submitted_commit_disagrees(
                cached,
                revision_commit_id=revision.commit_id,
            )
            saved_parent = cached.last_submitted_parent_change_id
            saved_head = cached.last_submitted_stack_head_change_id
            parent_changed = False
            stack_head_changed = False
            if saved_parent is not None or saved_head is not None:
                live_parent = _live_parent_change_id(stack, index=index)
                parent_changed = saved_parent != live_parent
                stack_head_changed = saved_head != live_head
            if not commit_changed and not parent_changed and not stack_head_changed:
                continue
            disagreements.append(
                SubmittedStateDisagreement(
                    change_id=revision.change_id,
                    commit_changed=commit_changed,
                    parent_changed=parent_changed,
                    stack_head_changed=stack_head_changed,
                )
            )
    return tuple(disagreements)


def _submitted_commit_disagrees(
    cached_change: CachedChange,
    *,
    revision_commit_id: str,
) -> bool:
    saved_commit_id = cached_change.last_submitted_commit_id
    return saved_commit_id is not None and saved_commit_id != revision_commit_id


def _live_parent_change_id(stack: LocalStack, *, index: int) -> str | None:
    """Return the live review parent change_id for one stack revision."""

    if index > 0:
        return stack.revisions[index - 1].change_id

    base_parent = stack.base_parent
    if stack.base_parent_is_trunk_ancestor or base_parent.commit_id == stack.trunk.commit_id:
        return None
    if not base_parent.is_reviewable(allow_divergent=True):
        return None
    return base_parent.change_id
