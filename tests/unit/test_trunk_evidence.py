from __future__ import annotations

import pytest

from jj_stack.models.github import GithubBranchRef, GithubPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR
from jj_stack.stack.trunk_evidence import classify_exact_snapshot, classify_rewritten_result
from tests.support.change_helpers import make_change


def _candidate() -> TrackedPR:
    return TrackedPR(
        pr_identity=PRIdentity(pr_number=1, head_ref="jj-stack/change-1"),
        submitted_baseline=SubmittedBaseline(commit_id="submitted-1"),
    )


def _pr(**updates: object) -> GithubPR:
    pr = GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(
            label="octo-org:jj-stack/change-1",
            ref="jj-stack/change-1",
            sha="submitted-1",
        ),
        html_url="https://github.test/octo-org/stacked-prs/pull/1",
        merged_at=None,
        number=1,
        state="open",
        title="change 1",
    )
    return pr.model_copy(update=updates)


@pytest.mark.merge_recovery
def test_exact_snapshot_evidence_is_identity_and_ancestry_bound() -> None:
    rows = (
        ("on_trunk", _pr(), True, False),
        ("not_on_trunk", _pr(), False, False),
        ("unresolved", _pr(), False, False),
        (
            "on_trunk",
            _pr(head=GithubBranchRef(ref="other", sha="submitted-1")),
            False,
            True,
        ),
        (
            "on_trunk",
            _pr(
                head=GithubBranchRef(
                    label="octo-org:jj-stack/change-1",
                    ref="jj-stack/change-1",
                    sha="other",
                )
            ),
            False,
            True,
        ),
    )

    for ancestry, pr, on_trunk, pr_mismatch in rows:
        result = classify_exact_snapshot(
            ancestry=ancestry,
            candidate=_candidate(),
            change_id="abcdefghijkl",
            pr=pr,
        )

        assert result.on_trunk is on_trunk
        assert result.pr_mismatch is pr_mismatch
        # An unproven verdict always explains itself, so no caller has to invent a message.
        assert on_trunk or result.reason is not None


@pytest.mark.merge_recovery
def test_rewritten_result_requires_a_reachable_concrete_merge_result() -> None:
    rows = (
        (
            _pr(head=GithubBranchRef(ref="other", sha="submitted-1")),
            None,
            False,
        ),
        (
            _pr(
                head=GithubBranchRef(
                    label="octo-org:jj-stack/change-1",
                    ref="jj-stack/change-1",
                    sha="other",
                )
            ),
            None,
            False,
        ),
        (_pr(), None, False),
        (
            _pr(state="closed", merged_at="2026-07-21T12:00:00Z"),
            None,
            False,
        ),
        (
            _pr(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "unresolved",
            False,
        ),
        (
            _pr(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "not_on_trunk",
            False,
        ),
        (
            _pr(
                state="closed",
                merged_at="2026-07-21T12:00:00Z",
                merge_commit_sha="merge-1",
            ),
            "on_trunk",
            True,
        ),
    )

    for pr, ancestry, on_trunk in rows:
        result = classify_rewritten_result(
            candidate=_candidate(),
            change_id="abcdefghijkl",
            merge_result_ancestry=ancestry,
            pr=pr,
        )

        assert result.on_trunk is on_trunk
        assert on_trunk or result.reason is not None


def _change(*, commit_id: str, empty: bool = False, immutable: bool = False) -> LocalCommit:
    return make_change(
        change_id="change-1",
        commit_id=commit_id,
        description="feature",
        empty=empty,
        immutable=immutable,
    )


def test_unpublished_edit_check_covers_every_shape_its_callers_pass() -> None:
    """One wrong answer here destroys local work, so pin every shape callers pass."""

    submitted = "submitted-1"

    assert not _change(commit_id="submitted-1").holds_unpublished_edit(submitted)
    assert _change(commit_id="edited-locally").holds_unpublished_edit(submitted)
    # An immutable change cannot hold a local edit, whatever its commit.
    assert not _change(commit_id="edited-locally", immutable=True).holds_unpublished_edit(
        submitted
    )
    # An empty change modifies no files relative to its parent, so a rewrite that emptied it is
    # safe to remove.
    assert not _change(commit_id="rebased-onto-trunk", empty=True).holds_unpublished_edit(
        submitted
    )
