"""Stable naming policy for jj-stack PR branches."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR
from jj_stack.pr_branch_namespace import current_pr_branch_namespace


@dataclass(frozen=True, slots=True)
class ResolvedPRBranch:
    """Stable PR branch selected for one local change."""

    branch: str
    change_id: str
    recovered: bool = False


def resolve_pr_branches(
    *,
    changes: tuple[LocalCommit, ...],
    tracked_prs: Mapping[str, TrackedPR],
) -> tuple[ResolvedPRBranch, ...]:
    """Resolve each branch from its saved identity or initial name."""

    resolutions = tuple(
        ResolvedPRBranch(
            branch=(
                tracked.pr_identity.head_ref
                if (tracked := tracked_prs.get(change.change_id)) is not None
                else current_pr_branch_namespace().generate_branch(change)
            ),
            change_id=change.change_id,
        )
        for change in changes
    )
    ensure_unique_pr_branches(resolutions)
    return resolutions


def ensure_new_pr_branches_unclaimed(
    resolutions: tuple[ResolvedPRBranch, ...],
    tracked_prs: Mapping[str, TrackedPR],
) -> None:
    saved_by_branch = {
        tracked.pr_identity.head_ref: change_id for change_id, tracked in tracked_prs.items()
    }
    collisions = tuple(
        resolution.branch
        for resolution in resolutions
        if resolution.change_id not in tracked_prs
        and resolution.branch in saved_by_branch
        and saved_by_branch[resolution.branch] != resolution.change_id
    )
    if collisions:
        raise CliError(
            t"Cannot create a pull request: these PR branches are already linked to other "
            t"changes: "
            t"{ui.join(ui.bookmark, collisions)}.",
            hint=t"Run {ui.cmd('jj-stack list')} to find those changes. Use "
            t"{ui.cmd('jj-stack cleanup --pull-request PR')} for a closed or merged PR, or "
            t"change the new change's subject with {ui.cmd('jj describe CHANGE')}.",
        )


def ensure_unique_pr_branches(
    resolutions: tuple[ResolvedPRBranch, ...],
) -> None:
    duplicates = duplicate_pr_branch_claims(
        (resolution.branch, resolution.change_id) for resolution in resolutions
    )
    if not duplicates:
        return
    collisions = ui.join(
        lambda item: t"{ui.bookmark(item[0])} for changes {ui.join(ui.change_id, item[1])}",
        sorted(duplicates.items()),
    )
    raise CliError(
        t"Multiple changes in the selected stack would use the same PR branch: {collisions}.",
        hint=t"Use {ui.cmd('jj describe CHANGE')} to change an unsubmitted change's subject, or "
        t"{ui.cmd('jj-stack relink PR CHANGE')} to correct a saved pull request link.",
    )


def duplicate_pr_branch_claims(
    claims: Iterable[tuple[str, str]],
) -> dict[str, tuple[str, ...]]:
    """Return branches claimed by more than one distinct change."""

    change_ids_by_branch: dict[str, set[str]] = {}
    for branch, change_id in claims:
        change_ids_by_branch.setdefault(branch, set()).add(change_id)
    return {
        branch: tuple(sorted(change_ids))
        for branch, change_ids in change_ids_by_branch.items()
        if len(change_ids) > 1
    }
