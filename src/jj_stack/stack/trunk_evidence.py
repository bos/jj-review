"""Pure classification of whether a tracked pull request's work is on trunk.

Check whether the submitted commit or GitHub's rewritten merge result is an ancestor of trunk.
Both checks also compare the PR with its saved record. A PR's merged state alone does not show
that its work reached this repo's trunk.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

import jj_stack.ui as ui
from jj_stack.formatting import format_pr_label
from jj_stack.models.github import GithubPR
from jj_stack.models.tracking import TrackedPR
from jj_stack.ui import Message

CommitAncestry = Literal["not_on_trunk", "on_trunk", "unresolved"]
TrunkEvidenceKind = Literal["exact", "rewritten"]


@dataclass(frozen=True, slots=True)
class TrunkEvidence:
    """Whether the PR and commit ancestry checks confirm that submitted work reached trunk.

    An unsuccessful check includes a reason to report to the user.
    """

    on_trunk: bool
    reason: Message | None = None

    @classmethod
    def confirmed(cls) -> TrunkEvidence:
        return cls(on_trunk=True)

    @classmethod
    def not_confirmed(
        cls,
        reason: Message,
    ) -> TrunkEvidence:
        return cls(
            on_trunk=False,
            reason=reason,
        )


def classify_exact_snapshot(
    *,
    ancestry: CommitAncestry,
    candidate: TrackedPR,
    change_id: str,
    pr: GithubPR,
) -> TrunkEvidence:
    """Check that the submitted commit is on trunk and the PR still matches its saved record."""

    if ancestry != "on_trunk":
        return TrunkEvidence.not_confirmed(
            _ancestry_reason(ancestry, candidate.submitted_baseline.commit_id)
        )
    mismatch = _snapshot_mismatch(candidate, change_id, pr)
    if mismatch is not None:
        return TrunkEvidence.not_confirmed(mismatch)
    return TrunkEvidence.confirmed()


def classify_rewritten_result(
    *,
    candidate: TrackedPR,
    change_id: str,
    merge_result_ancestry: CommitAncestry | None,
    pr: GithubPR,
) -> TrunkEvidence:
    """Classify merge-result evidence for one currently selected pull request."""

    mismatch = _snapshot_mismatch(candidate, change_id, pr)
    if mismatch is not None:
        return TrunkEvidence.not_confirmed(mismatch)
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    if pr.state != "merged":
        return TrunkEvidence.not_confirmed(t"{pr_label} is {pr.state} without a result on trunk")
    merge_commit_id = pr.merge_commit_sha
    if merge_commit_id is None:
        return TrunkEvidence.not_confirmed(
            t"GitHub did not report the commit produced by merging {pr_label}"
        )
    if merge_result_ancestry == "unresolved":
        return TrunkEvidence.not_confirmed(
            t"commit {ui.commit_id(merge_commit_id)} from GitHub's merge is unavailable locally",
        )
    if merge_result_ancestry != "on_trunk":
        return TrunkEvidence.not_confirmed(
            t"commit {ui.commit_id(merge_commit_id)} from GitHub's merge is not on trunk",
        )
    return TrunkEvidence.confirmed()


def classify_trunk_evidence(
    *,
    ancestries: Mapping[str, CommitAncestry],
    candidate: TrackedPR,
    change_id: str,
    pr: GithubPR,
) -> tuple[TrunkEvidenceKind | None, Message]:
    """Check submitted-commit and merge-result ancestry using the supplied observations."""

    exact = classify_exact_snapshot(
        ancestry=ancestries[candidate.submitted_baseline.commit_id],
        candidate=candidate,
        change_id=change_id,
        pr=pr,
    )
    rewritten = classify_rewritten_result(
        candidate=candidate,
        change_id=change_id,
        merge_result_ancestry=ancestries.get(pr.merge_commit_sha or ""),
        pr=pr,
    )
    if exact.on_trunk:
        return "exact", ""
    if rewritten.on_trunk:
        return "rewritten", ""
    return None, rewritten.reason or exact.reason or "no merge result is on trunk"


def _ancestry_reason(ancestry: CommitAncestry, commit_id: str) -> Message:
    if ancestry == "unresolved":
        return t"the submitted commit {ui.commit_id(commit_id)} is unavailable locally"
    return t"the submitted commit {ui.commit_id(commit_id)} is not on trunk"


def _snapshot_mismatch(
    candidate: TrackedPR,
    change_id: str,
    pr: GithubPR,
) -> Message | None:
    if candidate.matches_snapshot(pr):
        return None
    identity = candidate.pr_identity
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    if not identity.matches_pr(pr):
        return (
            t"{pr_label} no longer matches the saved pull request link for "
            t"{ui.change_id(change_id)}"
        )
    return t"{pr_label} no longer points to the last submitted commit"
