"""One classifier for a change's relationship to its pull request, PR branch, and trunk.

Every lifecycle command observes the same facts about a tracked change: the saved tracking
pair, the live pull request, the PR branch on the remote, the visible local copies, and whether
the submitted work is proven on fetched trunk. `classify` turns one `ChangeObservation` into one
`ChangeState`. A `Stop` state carries the one explanation and repair every command shares, so a
command decides only which states it acts on.

`LocalCommit` keeps describing the change itself: conflicts, emptiness, divergence, and working
copies. This module classifies the change's relationship to GitHub, not its local shape.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypedDict

import jj_stack.ui as ui
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import short_change_id
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, TrackedPR, TrackingState
from jj_stack.ui import Message

if TYPE_CHECKING:
    from jj_stack.stack.trunk_evidence import TrunkEvidenceKind


@dataclass(frozen=True, slots=True)
class Unobserved:
    """A fact the command did not look up, as opposed to one it observed to be absent."""


UNOBSERVED = Unobserved()


@dataclass(frozen=True, kw_only=True)
class ChangeObservation:
    """Everything one command observed about one change; unobserved facts stay marked."""

    change_id: str
    tracked: TrackedPR | None
    # The PR branch the change uses, or would use once submitted.
    branch: str | None
    remote_name: str | None = None
    # Every visible copy of the change; empty when none remains.
    local: tuple[LocalCommit, ...] | Unobserved = UNOBSERVED
    # The copy in the selected stack, when the command selected one.
    selected: LocalCommit | None = None
    # The saved pull request looked up by number; None when GitHub reports none.
    pr: GithubPR | None | Unobserved = UNOBSERVED
    open_prs_on_branch: tuple[GithubPR, ...] | Unobserved = UNOBSERVED
    # The commit at branch@remote; None when the branch is absent.
    remote_target: str | None | Unobserved = UNOBSERVED
    # Whether the submitted work is proven on fetched trunk, from `trunk_evidence`.
    trunk_evidence: TrunkEvidenceKind | None | Unobserved = UNOBSERVED
    trunk_evidence_reason: Message | None = None
    lookup_error: Message | None = None


@dataclass(frozen=True, kw_only=True)
class _State:
    change_id: str
    tracked: TrackedPR | None
    branch: str | None
    remote_name: str | None
    local: tuple[LocalCommit, ...] | Unobserved
    selected: LocalCommit | None

    @property
    def divergent(self) -> bool:
        """Whether more than one visible commit carries this change."""

        if self.selected is not None and self.selected.divergent:
            return True
        return not isinstance(self.local, Unobserved) and any(
            commit.divergent for commit in self.local
        )

    @property
    def has_local_edits(self) -> bool:
        """Whether the selected local commit differs from the submitted baseline."""

        return (
            self.tracked is not None
            and self.selected is not None
            and self.selected.commit_id != self.tracked.submitted_baseline.commit_id
        )

    def _branch_label(self) -> Message:
        branch = self.branch or "?"
        return ui.bookmark(f"{branch}@{self.remote_name}" if self.remote_name else branch)

    def _saved_label(self) -> Message:
        if self.tracked is None:
            raise AssertionError("A saved pull request label requires tracking.")
        return format_pr_label(self.tracked.pr_identity.pr_number)


@dataclass(frozen=True, kw_only=True)
class WithPR(_State):
    """A state GitHub reported a pull request for."""

    pr: GithubPR


class Stop:
    """A state every mutating command stops on; it carries the shared explanation."""

    @property
    def reason(self) -> Message:
        raise NotImplementedError

    @property
    def repair(self) -> Message:
        raise NotImplementedError


# ---- the healthy lifecycle -----------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class Unpublished(_State):
    """No tracking; the change has never been submitted from a tracked repo."""

    # A branch already at the local commit is an interrupted first push, which submit finishes.
    remote_target: str | None | Unobserved


@dataclass(frozen=True, kw_only=True)
class NotInspected(_State):
    """Tracking exists, but this command did not consult GitHub."""


@dataclass(frozen=True, kw_only=True)
class Published(WithPR):
    """The open pull request is at the submitted baseline, and so is the local change."""

    remote_target: str | None | Unobserved


@dataclass(frozen=True, kw_only=True)
class Edited(WithPR):
    """The open pull request is at the submitted baseline; the local change moved on."""

    remote_target: str | None | Unobserved


@dataclass(frozen=True, kw_only=True)
class PushedUnrecorded(WithPR):
    """The open pull request already follows the local commit, but no baseline records it."""

    remote_target: str | None | Unobserved


@dataclass(frozen=True, kw_only=True)
class Queued(WithPR):
    """The open pull request is in the trunk merge queue; every command waits."""

    remote_target: str | None | Unobserved


@dataclass(frozen=True, kw_only=True)
class Landed(WithPR):
    """The submitted work is proven on fetched trunk, whatever the pull request says."""

    evidence: TrunkEvidenceKind


@dataclass(frozen=True, kw_only=True)
class Merged(WithPR):
    """GitHub reports the pull request merged, but no observation proved it on fetched trunk.

    `unproven` explains why when the command looked; it is None when the command did not.
    """

    unproven: Message | None


@dataclass(frozen=True, kw_only=True)
class Closed(WithPR):
    """GitHub reports the pull request closed without merging."""


# ---- stops --------------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class LookupFailed(Stop, _State):
    error: Message

    @property
    def reason(self) -> Message:
        return self.error

    @property
    def repair(self) -> Message:
        return t"run {ui.cmd('jj-stack doctor')} to check GitHub access"


@dataclass(frozen=True, kw_only=True)
class PRMissing(Stop, _State):
    open_prs_on_branch: tuple[GithubPR, ...]

    @property
    def reason(self) -> Message:
        reason: Message = t"GitHub no longer reports {self._saved_label()}"
        if self.open_prs_on_branch:
            others = ui.join(_pr_label, self.open_prs_on_branch)
            reason = t"{reason}; open {others} uses its PR branch {self._branch_label()}"
        return reason

    @property
    def repair(self) -> Message:
        return _RELINK_OR_FORGET


@dataclass(frozen=True, kw_only=True)
class PRIdentityMismatch(Stop, WithPR):
    @property
    def reason(self) -> Message:
        return (
            t"{_pr_label(self.pr)} now uses head branch {ui.bookmark(self.pr.head.ref)}, not "
            t"the saved PR branch {self._branch_label()}"
        )

    @property
    def repair(self) -> Message:
        return _RELINK


@dataclass(frozen=True, kw_only=True)
class PRAmbiguous(Stop, _State):
    open_prs_on_branch: tuple[GithubPR, ...]

    @property
    def reason(self) -> Message:
        numbers = ui.join(_pr_label, self.open_prs_on_branch)
        return (
            t"GitHub reports several open pull requests for PR branch "
            t"{self._branch_label()}: {numbers}"
        )

    @property
    def repair(self) -> Message:
        return _RELINK


@dataclass(frozen=True, kw_only=True)
class CompetingOpenPR(Stop, WithPR):
    competitors: tuple[GithubPR, ...]

    @property
    def ambiguous(self) -> bool:
        """Whether GitHub reports more than one open pull request for the PR branch."""

        return len(self.competitors) + (1 if self.pr.state == "open" else 0) > 1

    @property
    def reason(self) -> Message:
        others = ui.join(_pr_label, self.competitors)
        return (
            t"open {others} also uses PR branch {self._branch_label()}; this change's pull "
            t"request is {_pr_label(self.pr)}"
        )

    @property
    def repair(self) -> Message:
        others = ui.join(_pr_label, self.competitors)
        return t"close or retarget {others}, or {_RELINK}"


@dataclass(frozen=True, kw_only=True)
class UntrackedPRExists(Stop, _State):
    open_prs_on_branch: tuple[GithubPR, ...]

    @property
    def reason(self) -> Message:
        prs = ui.join(_pr_label, self.open_prs_on_branch)
        return t"GitHub already reports {prs} for untracked PR branch {self._branch_label()}"

    @property
    def repair(self) -> Message:
        return t"adopt the intended pull request with {ui.cmd('jj-stack relink')}"


@dataclass(frozen=True, kw_only=True)
class BranchClaimed(Stop, _State):
    remote_target: str

    @property
    def reason(self) -> Message:
        return (
            t"PR branch {self._branch_label()} already exists at "
            t"{ui.commit_id(self.remote_target)} and does not belong to this change"
        )

    @property
    def repair(self) -> Message:
        return "move or delete that branch"


@dataclass(frozen=True, kw_only=True)
class PRHeadMoved(Stop, WithPR):
    remote_target: str | None | Unobserved

    @property
    def reason(self) -> Message:
        head = self.pr.head.sha or "?"
        return (
            t"{_pr_label(self.pr)} is at {ui.commit_id(head)}, not at this change or its last "
            t"submitted commit; the PR branch was updated outside this repo"
        )

    @property
    def repair(self) -> Message:
        number = self.pr.number
        short = short_change_id(self.change_id)
        return (
            t"keep that work with {ui.cmd(f'jj-stack checkout --pull-request {number}')}, or run "
            t"{ui.cmd(f'jj-stack relink --replace-remote {number} {short}')} so the next submit "
            t"replaces it with this change"
        )


@dataclass(frozen=True, kw_only=True)
class BranchMissing(Stop, WithPR):
    @property
    def reason(self) -> Message:
        return t"PR branch {self._branch_label()} for {_pr_label(self.pr)} no longer exists"

    @property
    def repair(self) -> Message:
        return (
            t"restore the branch, or close {_pr_label(self.pr)} on GitHub and run "
            t"{ui.cmd('jj-stack cleanup')}"
        )


@dataclass(frozen=True, kw_only=True)
class BranchDisagrees(Stop, WithPR):
    remote_target: str

    @property
    def reason(self) -> Message:
        head = self.pr.head.sha or "?"
        return (
            t"{_pr_label(self.pr)} is at {ui.commit_id(head)} but PR branch "
            t"{self._branch_label()} is at {ui.commit_id(self.remote_target)}"
        )

    @property
    def repair(self) -> Message:
        return (
            t"inspect them with {ui.cmd('jj-stack view')}; GitHub may still be catching up on a "
            t"recent push"
        )


type ChangeState = (
    Unpublished
    | NotInspected
    | Published
    | Edited
    | PushedUnrecorded
    | Queued
    | Landed
    | Merged
    | Closed
    | LookupFailed
    | PRMissing
    | PRIdentityMismatch
    | PRAmbiguous
    | CompetingOpenPR
    | UntrackedPRExists
    | BranchClaimed
    | PRHeadMoved
    | BranchMissing
    | BranchDisagrees
)

_RELINK: Message = t"reattach the intended pull request with {ui.cmd('jj-stack relink')}"
_RELINK_OR_FORGET: Message = (
    t"{_RELINK}, or forget the link with {ui.cmd('jj-stack unstack --local')}"
)


def _pr_label(pr: GithubPR) -> Message:
    return format_pr_label(pr.number, url=pr.html_url)


# ---- classification ----------------------------------------------------------------------


class _Common(TypedDict):
    change_id: str
    tracked: TrackedPR | None
    branch: str | None
    remote_name: str | None
    local: tuple[LocalCommit, ...] | Unobserved
    selected: LocalCommit | None


def classify(observation: ChangeObservation) -> ChangeState:
    """Derive one state from one observation; unobserved facts never produce a stop."""

    o = observation
    common = _Common(
        change_id=o.change_id,
        tracked=o.tracked,
        branch=o.branch,
        remote_name=o.remote_name,
        local=o.local,
        selected=o.selected,
    )
    open_prs = () if isinstance(o.open_prs_on_branch, Unobserved) else o.open_prs_on_branch
    if o.tracked is None:
        return _classify_untracked(o, common, open_prs)
    if o.lookup_error is not None:
        return LookupFailed(**common, error=o.lookup_error)
    if isinstance(o.pr, Unobserved):
        return NotInspected(**common)
    if o.pr is None:
        if len(open_prs) > 1:
            return PRAmbiguous(**common, open_prs_on_branch=open_prs)
        return PRMissing(**common, open_prs_on_branch=open_prs)
    pr = o.pr.normalize_state()
    if pr.head.ref != o.tracked.pr_identity.head_ref:
        return PRIdentityMismatch(**common, pr=pr)
    competitors = tuple(candidate for candidate in open_prs if candidate.number != pr.number)
    if competitors:
        return CompetingOpenPR(**common, pr=pr, competitors=competitors)
    if pr.state == "closed":
        return Closed(**common, pr=pr)
    evidence = o.trunk_evidence
    if pr.state == "merged":
        if isinstance(evidence, Unobserved):
            return Merged(**common, pr=pr, unproven=None)
        if evidence is None:
            return Merged(**common, pr=pr, unproven=o.trunk_evidence_reason)
        return Landed(**common, pr=pr, evidence=evidence)
    if evidence == "exact":
        return Landed(**common, pr=pr, evidence=evidence)
    if pr.is_queued:
        return Queued(**common, pr=pr, remote_target=o.remote_target)
    return _classify_open(o, common, pr)


def _classify_untracked(
    o: ChangeObservation,
    common: _Common,
    open_prs: tuple[GithubPR, ...],
) -> ChangeState:
    if open_prs:
        return UntrackedPRExists(**common, open_prs_on_branch=open_prs)
    remote = o.remote_target
    if isinstance(remote, str) and remote not in _local_commit_ids(o):
        return BranchClaimed(**common, remote_target=remote)
    return Unpublished(**common, remote_target=remote)


def _classify_open(
    o: ChangeObservation,
    common: _Common,
    pr: GithubPR,
) -> ChangeState:
    if o.tracked is None:
        raise AssertionError("Open pull request classification requires tracking.")
    baseline = o.tracked.submitted_baseline.commit_id
    head = pr.head.sha
    remote = o.remote_target
    if head is not None and head != baseline and head not in _local_commit_ids(o):
        return PRHeadMoved(**common, pr=pr, remote_target=remote)
    if not isinstance(remote, Unobserved):
        if remote is None:
            return BranchMissing(**common, pr=pr)
        if head is not None and remote != head:
            return BranchDisagrees(**common, pr=pr, remote_target=remote)
    if head is not None and head != baseline:
        return PushedUnrecorded(**common, pr=pr, remote_target=remote)
    local_commit = _selected_commit_id(o)
    if local_commit is not None and local_commit != baseline:
        return Edited(**common, pr=pr, remote_target=remote)
    return Published(**common, pr=pr, remote_target=remote)


def _selected_commit_id(o: ChangeObservation) -> str | None:
    if o.selected is not None:
        return o.selected.commit_id
    if not isinstance(o.local, Unobserved) and len(o.local) == 1:
        return o.local[0].commit_id
    return None


def _local_commit_ids(o: ChangeObservation) -> frozenset[str]:
    ids = set() if isinstance(o.local, Unobserved) else {commit.commit_id for commit in o.local}
    if o.selected is not None:
        ids.add(o.selected.commit_id)
    return frozenset(ids)


# ---- shared derived rules ---------------------------------------------------------------


def report_incomplete(state: ChangeState) -> bool:
    """Whether this change stops `view` and `list` from reporting the stack completely.

    Both report commands share this rule so the same repo cannot yield a complete report from
    one and an incomplete report from the other. A saved pull request whose GitHub state went
    unobserved counts the same way a failed lookup does. Divergence of merged work is history
    exposed by a fetch, not an incomplete report.
    """

    if state.divergent and not isinstance(state, (Landed, Merged)):
        return True
    if isinstance(state, CompetingOpenPR):
        return state.ambiguous
    return isinstance(state, (LookupFailed, NotInspected, PRAmbiguous, PRMissing))


@dataclass(frozen=True, slots=True)
class OrphanedRecord:
    """A saved tracking record whose change has left every live stack."""

    change_id: str
    pr_identity: PRIdentity


def enumerate_orphaned_records(
    state: TrackingState,
    local_stacks: Sequence[LocalStack],
) -> tuple[OrphanedRecord, ...]:
    """Return saved PR records whose change is no longer in any live stack."""

    live_change_ids = {change.change_id for stack in local_stacks for change in stack.changes}
    return tuple(
        OrphanedRecord(change_id=change_id, pr_identity=pr_identity)
        for change_id, pr_identity in state.pr_identities.items()
        if change_id not in live_change_ids
    )
