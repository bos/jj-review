from __future__ import annotations

from typing import Any

import pytest

import jj_stack.ui as ui
from jj_stack.models.github import GithubBranchRef, GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR, TrackingState
from jj_stack.stack.change_state import (
    UNOBSERVED,
    BranchClaimed,
    BranchDisagrees,
    BranchMissing,
    ChangeObservation,
    ChangeState,
    Closed,
    CompetingOpenPR,
    Edited,
    Landed,
    LookupFailed,
    Merged,
    NotInspected,
    PRAmbiguous,
    PRHeadMoved,
    PRIdentityMismatch,
    PRMissing,
    Published,
    PushedUnrecorded,
    Queued,
    Stop,
    Unpublished,
    UntrackedPRExists,
    classify,
    enumerate_orphaned_records,
    report_incomplete,
)
from tests.support.change_helpers import make_change
from tests.support.tracking import make_pr_identity

_BRANCH = "jj-stack/feature-abcdefgh"
_TRACKED = TrackedPR(
    change_id="abcdefghijkl",
    pr_identity=make_pr_identity(head_ref=_BRANCH, pr_number=7),
    submitted_baseline=SubmittedBaseline(commit_id="baseline"),
)


def _pr(
    *,
    number: int = 7,
    state: str = "open",
    head_ref: str = _BRANCH,
    head_sha: str | None = "baseline",
    queued: bool = False,
) -> GithubPR:
    return GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(ref=head_ref, sha=head_sha),
        html_url=f"https://github.test/octo/repo/pull/{number}",
        is_queued=queued,
        merged_at="2026-05-09T12:00:00Z" if state == "merged" else None,
        number=number,
        state="closed" if state == "merged" else state,
        title="feature",
    )


def _local(commit_id: str = "baseline", *, divergent: bool = False) -> LocalCommit:
    return make_change(
        change_id=_TRACKED.change_id, commit_id=commit_id, description="feature\n"
    ).model_copy(update={"divergent": divergent})


def _observe(**overrides: Any) -> ChangeObservation:
    local = _local()
    fields: dict[str, Any] = {
        "change_id": _TRACKED.change_id,
        "tracked": _TRACKED,
        "branch": _BRANCH,
        "remote_name": "origin",
        "local": (local,),
        "selected": local,
        "pr": _pr(),
        "open_prs_on_branch": (_pr(),),
    }
    fields.update(overrides)
    return ChangeObservation(**fields)


_REPRESENTATIVES: tuple[tuple[str, dict[str, object], type], ...] = (
    ("untracked, nothing on GitHub", {"tracked": None, "open_prs_on_branch": ()}, Unpublished),
    (
        "untracked, branch already at the local commit",
        {"tracked": None, "open_prs_on_branch": (), "remote_target": "baseline"},
        Unpublished,
    ),
    (
        "untracked, branch at a foreign commit",
        {"tracked": None, "open_prs_on_branch": (), "remote_target": "other"},
        BranchClaimed,
    ),
    ("untracked, open PR on the branch", {"tracked": None}, UntrackedPRExists),
    ("tracked, GitHub not consulted", {"pr": UNOBSERVED}, NotInspected),
    ("lookup failed", {"lookup_error": "GitHub returned 502"}, LookupFailed),
    ("saved PR gone", {"pr": None, "open_prs_on_branch": ()}, PRMissing),
    (
        "saved PR gone, two open PRs on the branch",
        {"pr": None, "open_prs_on_branch": (_pr(number=8), _pr(number=9))},
        PRAmbiguous,
    ),
    ("saved PR moved to another branch", {"pr": _pr(head_ref="other")}, PRIdentityMismatch),
    (
        "another open PR shares the branch",
        {"open_prs_on_branch": (_pr(), _pr(number=8))},
        CompetingOpenPR,
    ),
    ("closed without merging", {"pr": _pr(state="closed")}, Closed),
    ("merged, trunk not inspected", {"pr": _pr(state="merged")}, Merged),
    (
        "merged, not proven on fetched trunk",
        {"pr": _pr(state="merged"), "trunk_evidence": None, "trunk_evidence_reason": "why"},
        Merged,
    ),
    ("merged and proven", {"pr": _pr(state="merged"), "trunk_evidence": "rewritten"}, Landed),
    ("open, exact commit already on trunk", {"trunk_evidence": "exact"}, Landed),
    ("queued", {"pr": _pr(queued=True)}, Queued),
    ("queued but head moved", {"pr": _pr(queued=True, head_sha="elsewhere")}, PRHeadMoved),
    ("head moved off the change", {"pr": _pr(head_sha="elsewhere")}, PRHeadMoved),
    (
        "branch deletion closed the pull request",
        {"remote_target": None, "pr": _pr(state="closed")},
        BranchMissing,
    ),
    (
        "branch deleted while the head moved",
        {"remote_target": None, "pr": _pr(head_sha="elsewhere")},
        BranchMissing,
    ),
    ("branch and PR head disagree", {"remote_target": "other"}, BranchDisagrees),
    ("in sync", {}, Published),
    ("local edited since submit", {"selected": _local("rewrite")}, Edited),
    (
        "pushed but baseline not recorded",
        {"selected": _local("rewrite"), "pr": _pr(head_sha="rewrite")},
        PushedUnrecorded,
    ),
)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [(fields, expected) for _label, fields, expected in _REPRESENTATIVES],
    ids=[label for label, _fields, _expected in _REPRESENTATIVES],
)
def test_classifier_reaches_each_state_from_a_representative_observation(
    overrides: dict[str, object], expected: type
) -> None:
    state = classify(_observe(**overrides))

    assert type(state) is expected
    if isinstance(state, Stop):
        assert ui.plain_text(state.reason).strip()
        assert ui.plain_text(state.repair).strip()


def test_every_state_variant_has_a_representative() -> None:
    covered = {expected for _label, _fields, expected in _REPRESENTATIVES}
    declared = set(ChangeState.__value__.__args__)

    assert covered == declared


def test_unobserved_facts_never_produce_a_stop() -> None:
    state = classify(
        _observe(
            local=UNOBSERVED,
            selected=None,
            open_prs_on_branch=UNOBSERVED,
            remote_target=UNOBSERVED,
            trunk_evidence=UNOBSERVED,
        )
    )

    assert isinstance(state, Published)


def test_merged_state_keeps_the_reason_only_when_trunk_was_inspected() -> None:
    uninspected = classify(_observe(pr=_pr(state="merged")))
    unproven = classify(
        _observe(pr=_pr(state="merged"), trunk_evidence=None, trunk_evidence_reason="why")
    )

    assert isinstance(uninspected, Merged) and uninspected.unproven is None
    assert isinstance(unproven, Merged) and unproven.unproven == "why"


def test_open_pr_head_is_compared_against_every_visible_copy() -> None:
    # The published snapshot is still visible beside the local rewrite.
    snapshot, rewrite = _local("baseline"), _local("rewrite")
    state = classify(_observe(local=(snapshot, rewrite), selected=rewrite))

    assert isinstance(state, Edited)
    assert state.has_local_edits is True


def test_report_incompleteness_rule_is_shared_by_view_and_list() -> None:
    assert report_incomplete(classify(_observe())) is False
    assert report_incomplete(classify(_observe(pr=UNOBSERVED))) is True
    assert report_incomplete(classify(_observe(pr=None, open_prs_on_branch=()))) is True
    assert report_incomplete(classify(_observe(pr=_pr(head_sha="elsewhere")))) is False
    # Two open pull requests on one branch are ambiguous; a closed saved PR beside one open
    # competitor is only a warning.
    assert (
        report_incomplete(classify(_observe(open_prs_on_branch=(_pr(), _pr(number=8))))) is True
    )
    assert (
        report_incomplete(
            classify(_observe(pr=_pr(state="closed"), open_prs_on_branch=(_pr(number=8),)))
        )
        is False
    )
    divergent = _local(divergent=True)
    assert report_incomplete(classify(_observe(local=(divergent,), selected=divergent))) is True
    assert (
        report_incomplete(
            classify(_observe(local=(divergent,), selected=divergent, pr=_pr(state="merged")))
        )
        is False
    )


def test_enumerate_orphans_returns_tracked_record_with_no_live_change() -> None:
    live = make_change(change_id="live", commit_id="commit-live", description="live\n")
    trunk = make_change(change_id="trunk", commit_id="trunk", description="trunk\n")
    stack = LocalStack(
        base_parent=trunk, head=live, changes=(live,), selected_revset="@-", trunk=trunk
    )
    state = TrackingState(
        pr_identities={
            "live": make_pr_identity(head_ref="jj-stack/live-live", pr_number=1),
            "orphan": PRIdentity(pr_number=2, head_ref="jj-stack/orphan-orphan"),
        },
        submitted_baselines={
            "live": SubmittedBaseline(commit_id="commit-live"),
            "orphan": SubmittedBaseline(commit_id="commit-orphan"),
        },
    )

    orphans = enumerate_orphaned_records(state, (stack,))

    assert tuple(orphan.change_id for orphan in orphans) == ("orphan",)
