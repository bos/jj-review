"""Discover tracked review stacks from saved state plus the live `jj` DAG.

Both `status` (for the per-other-stack `needs submit` advisory) and `list` (for the
per-stack rendering and for orphan-row enumeration) need the same primitive: walk
every change `jj-review` has tracked, project it onto the current DAG, and return
the linear stacks those changes participate in.

The walk tolerates rewrite-heavy state — divergent and immutable copies appear in
`jj`'s `descendants()` after fetching merged PR branches — and skips revisions
that no longer satisfy `is_reviewable`. Empty results mean either no tracked
records or every tracked record has been removed from the DAG entirely.
"""

from __future__ import annotations

from dataclasses import dataclass

from jj_review import ui
from jj_review.errors import CliError
from jj_review.jj import JjClient
from jj_review.models.review_state import ReviewState
from jj_review.models.stack import LocalRevision, LocalStack


@dataclass(frozen=True, slots=True)
class DiscoveredTrackedStacks:
    """Result of `discover_tracked_stacks`."""

    current_commit_id: str | None
    stacks: tuple[LocalStack, ...]


def discover_tracked_stacks(
    *,
    jj_client: JjClient,
    state: ReviewState,
) -> DiscoveredTrackedStacks:
    """Return every tracked review stack reachable from the live DAG."""

    tracked_change_ids = tuple(
        change_id
        for change_id, cached_change in state.changes.items()
        if cached_change.has_review_identity or cached_change.is_unlinked
    )
    revisions_by_change_id = jj_client.query_revisions_by_change_ids(tracked_change_ids)
    tracked_revisions = tuple(
        revision
        for change_id in tracked_change_ids
        for revision in revisions_by_change_id.get(change_id, ())
        if revision.is_reviewable(allow_divergent=True, allow_immutable=True)
    )
    if not tracked_revisions:
        return DiscoveredTrackedStacks(current_commit_id=None, stacks=())

    trunk = jj_client.resolve_revision("trunk()")
    descendants = jj_client.query_descendant_revisions(
        tuple(revision.commit_id for revision in tracked_revisions)
    )
    current_commit_id = _current_review_commit_id(descendants)
    all_revisions_by_commit_id = {
        revision.commit_id: revision
        for revision in descendants
        if not revision.current_working_copy
        and revision.is_reviewable(allow_divergent=True, allow_immutable=True)
    }
    for revision in tracked_revisions:
        all_revisions_by_commit_id[revision.commit_id] = revision

    all_revisions = tuple(all_revisions_by_commit_id.values())
    all_commit_ids = {revision.commit_id for revision in all_revisions}
    reviewable_parent_commit_ids = {
        revision.only_parent_commit_id() for revision in all_revisions
    }
    stack_roots = tuple(
        revision
        for revision in all_revisions
        if revision.only_parent_commit_id() not in all_commit_ids
    )
    if not stack_roots:
        return DiscoveredTrackedStacks(current_commit_id=current_commit_id, stacks=())

    reviewable_children = _children_by_parent(tuple(all_revisions_by_commit_id.values()))
    base_parent_commit_ids = tuple(
        dict.fromkeys(
            root.only_parent_commit_id()
            for root in stack_roots
            if root.only_parent_commit_id() not in all_revisions_by_commit_id
        )
    )
    base_parents = {
        revision.commit_id: revision
        for revision in jj_client.query_revisions_by_commit_ids(base_parent_commit_ids)
    }
    trunk_ancestor_base_parent_commit_ids = jj_client.query_trunk_ancestor_commit_ids(
        base_parent_commit_ids
    )

    discovered: list[LocalStack] = []
    seen_keys: set[tuple[str, ...]] = set()
    for root in stack_roots:
        for head in _walk_heads(root, children_by_parent=reviewable_children):
            if head.commit_id in reviewable_parent_commit_ids:
                continue
            stack_revisions = _stack_revisions_from_root_to_head(
                root=root,
                head=head,
                revisions_by_commit_id=all_revisions_by_commit_id,
            )
            key = tuple(revision.change_id for revision in stack_revisions)
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            base_parent = base_parents.get(root.only_parent_commit_id(), root)
            discovered.append(
                LocalStack(
                    base_parent=base_parent,
                    base_parent_is_trunk_ancestor=(
                        base_parent.commit_id in trunk_ancestor_base_parent_commit_ids
                    ),
                    head=head,
                    revisions=stack_revisions,
                    selected_revset=head.commit_id,
                    trunk=trunk,
                )
            )
    return DiscoveredTrackedStacks(
        current_commit_id=current_commit_id,
        stacks=tuple(discovered),
    )


def _current_review_commit_id(revisions: tuple[LocalRevision, ...]) -> str | None:
    for revision in revisions:
        if not revision.current_working_copy:
            continue
        return revision.parents[0] if revision.parents else None
    return None


def _children_by_parent(
    revisions: tuple[LocalRevision, ...],
) -> dict[str, tuple[LocalRevision, ...]]:
    grouped: dict[str, list[LocalRevision]] = {}
    for revision in revisions:
        for parent_commit_id in revision.parents:
            grouped.setdefault(parent_commit_id, []).append(revision)
    return {parent_commit_id: tuple(children) for parent_commit_id, children in grouped.items()}


def _walk_heads(
    revision: LocalRevision,
    *,
    children_by_parent: dict[str, tuple[LocalRevision, ...]],
) -> tuple[LocalRevision, ...]:
    heads: list[LocalRevision] = []
    pending: list[LocalRevision] = [revision]
    while pending:
        current = pending.pop()
        children = children_by_parent.get(current.commit_id, ())
        if not children:
            heads.append(current)
            continue
        pending.extend(reversed(children))
    return tuple(heads)


def _stack_revisions_from_root_to_head(
    *,
    root: LocalRevision,
    head: LocalRevision,
    revisions_by_commit_id: dict[str, LocalRevision],
) -> tuple[LocalRevision, ...]:
    revisions_head_first: list[LocalRevision] = []
    current = head
    while True:
        revisions_head_first.append(current)
        if current.commit_id == root.commit_id:
            break
        parent_commit_id = current.only_parent_commit_id()
        parent = revisions_by_commit_id.get(parent_commit_id)
        if parent is None:
            raise CliError(
                t"Could not safely inspect review stacks: missing ancestor "
                t"{ui.commit_id(parent_commit_id)} for {ui.change_id(head.change_id)}."
            )
        current = parent
    return tuple(reversed(revisions_head_first))
