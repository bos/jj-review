"""Observe ordinary repo paths for the pure path projection."""

from __future__ import annotations

from collections.abc import Sequence

from jj_stack.jj.client import JjClient, quote_revset_symbol
from jj_stack.models.tracking import TrackingState
from jj_stack.stack.path import (
    RepoPathObservation,
    RepoStackPaths,
    project_repo_paths,
)
from jj_stack.stack.pr_branches import prepare_visible_pr_snapshots
from jj_stack.stack.trunk import require_usable_trunk


def observe_repo_paths(
    *,
    jj_client: JjClient,
    state: TrackingState,
    descendant_of: Sequence[str] = (),
) -> RepoStackPaths:
    """Batch the visible facts for ordinary maximal paths.

    With no anchors this observes the repo inventory. Exact commit anchors narrow the
    observation to the anchors' descendants; callers then keep the paths that contain their
    anchor, so a wider scope costs only query work.
    """

    trunk_path = "first_ancestors(trunk())"
    visible_scope = "visible()"
    if descendant_of:
        anchors = " | ".join(quote_revset_symbol(commit_id) for commit_id in descendant_of)
        visible_scope = f"(visible() & ({anchors})::)"
    candidates = f"(({visible_scope}) ~ {trunk_path})"
    prepare_visible_pr_snapshots(jj_client=jj_client, state=state)
    rows = jj_client.query_commits_with_membership(
        f"trunk() | ({candidates}) | parents({candidates}) | @",
        membership_revsets=("trunk()", candidates, trunk_path),
    )
    trunks = tuple(commit for commit, flags in rows if flags[0])
    trunk = require_usable_trunk(trunks)
    current_working_copy = next(
        (commit for commit, _flags in rows if commit.current_working_copy),
        None,
    )
    current_tracked_commit_id = (
        (
            current_working_copy.commit_id
            if current_working_copy.change_id in state.prs
            and not current_working_copy.empty
            and bool(current_working_copy.description.strip())
            else current_working_copy.parents[0]
        )
        if current_working_copy is not None and len(current_working_copy.parents) == 1
        else None
    )
    return project_repo_paths(
        RepoPathObservation(
            candidate_commit_ids=frozenset(
                commit.commit_id for commit, flags in rows if flags[1]
            ),
            current_tracked_commit_id=current_tracked_commit_id,
            fetched_trunk_commit_ids=frozenset(
                commit.commit_id for commit, flags in rows if flags[2]
            ),
            commits=tuple(commit for commit, _flags in rows),
            tracked_change_ids=frozenset(state.prs),
            trunk=trunk,
        )
    )
