"""Pure classification of whether a tracked pull request's work is on trunk.

Two routes prove it: the exact submitted commit is an ancestor of fetched trunk, or GitHub
rewrote it and the merge-result commit is. GitHub reporting a pull request as merged is not one of
them, since that says nothing about the trunk this repo fetched.
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
    """Whether one pull request's work is proven to be on trunk, and why not when it is not.

    Callers only ever ask whether the work is proven and, failing that, what to tell the user, so
    an unproven verdict always carries a reason. `pr_mismatch` marks the one distinction a
    caller draws beyond that: the saved pull request identity no longer describes the live pull
    request, which is a tracking problem rather than a question about trunk.
    """

    on_trunk: bool
    reason: Message | None = None
    pr_mismatch: bool = False

    @classmethod
    def proven(cls) -> TrunkEvidence:
        return cls(on_trunk=True)

    @classmethod
    def unproven(
        cls,
        reason: Message,
        *,
        pr_mismatch: bool = False,
    ) -> TrunkEvidence:
        return cls(
            on_trunk=False,
            reason=reason,
            pr_mismatch=pr_mismatch,
        )


def classify_exact_snapshot(
    *,
    ancestry: CommitAncestry,
    candidate: TrackedPR,
    change_id: str,
    pr: GithubPR,
) -> TrunkEvidence:
    """Classify the repo-wide exact-snapshot gate without lifecycle policy."""

    if ancestry != "on_trunk":
        return TrunkEvidence.unproven(
            _ancestry_reason(ancestry, candidate.submitted_baseline.commit_id)
        )
    mismatch = _snapshot_mismatch(candidate, change_id, pr)
    if mismatch is not None:
        return TrunkEvidence.unproven(mismatch, pr_mismatch=True)
    return TrunkEvidence.proven()


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
        return TrunkEvidence.unproven(mismatch, pr_mismatch=True)
    lifecycle = pr.normalize_state().state
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    if lifecycle != "merged":
        return TrunkEvidence.unproven(t"{pr_label} is {lifecycle} without a result on trunk")
    merge_commit_id = pr.merge_commit_sha
    if merge_commit_id is None:
        return TrunkEvidence.unproven(
            t"GitHub did not report the merge-result commit for {pr_label}"
        )
    if merge_result_ancestry == "unresolved":
        return TrunkEvidence.unproven(
            t"merge result {ui.commit_id(merge_commit_id)} is unavailable locally",
        )
    if merge_result_ancestry != "on_trunk":
        return TrunkEvidence.unproven(
            t"merge result {ui.commit_id(merge_commit_id)} is not on trunk",
        )
    return TrunkEvidence.proven()


def classify_proven_kind(
    *,
    ancestries: Mapping[str, CommitAncestry],
    candidate: TrackedPR,
    change_id: str,
    pr: GithubPR,
) -> tuple[TrunkEvidenceKind | None, Message]:
    """Classify both proof routes from one previously batched ancestry observation."""

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
            t"{pr_label} no longer matches the pull request recorded for "
            t"{ui.change_id(change_id)}"
        )
    return t"{pr_label} no longer reports the submitted head"
