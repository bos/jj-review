from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from jj_review.commands.land import (
    LandError,
    _build_land_plan,
    _parse_pull_request_reference,
    _updated_landed_change,
)
from jj_review.commands.review_state import PreparedStatus
from jj_review.commands.submit import ResolvedGithubRepository
from jj_review.models.bookmarks import RemoteBookmarkState
from jj_review.models.cache import CachedChange
from jj_review.models.github import GithubBranchRef, GithubPullRequest


def test_build_land_plan_uses_maximal_open_prefix() -> None:
    prepared_status = cast(
        PreparedStatus,
        SimpleNamespace(
            prepared=SimpleNamespace(
                status_revisions=(
                    SimpleNamespace(
                        revision=SimpleNamespace(change_id="change-1", commit_id="commit-1")
                    ),
                    SimpleNamespace(
                        revision=SimpleNamespace(change_id="change-2", commit_id="commit-2")
                    ),
                    SimpleNamespace(
                        revision=SimpleNamespace(change_id="change-3", commit_id="commit-3")
                    ),
                )
            )
        ),
    )
    status_result = SimpleNamespace(
        revisions=(
            _status_revision(
                change_id="change-3",
                commit_id="commit-3",
                pull_request=_pull_request(number=3, state="closed"),
                pull_request_state="closed",
                subject="feature 3",
            ),
            _status_revision(
                change_id="change-2",
                commit_id="commit-2",
                pull_request=_pull_request(number=2),
                pull_request_state="open",
                subject="feature 2",
            ),
            _status_revision(
                change_id="change-1",
                commit_id="commit-1",
                pull_request=_pull_request(number=1),
                pull_request_state="open",
                subject="feature 1",
            ),
        )
    )

    plan = _build_land_plan(
        expect_pr_number=None,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is False
    assert [revision.pull_request_number for revision in plan.landed_revisions] == [1, 2]
    assert plan.boundary_action is not None
    assert plan.boundary_action.status == "planned"
    assert "stop before feature 3" in plan.boundary_action.message


def test_build_land_plan_blocks_expect_pr_mismatch() -> None:
    prepared_status = cast(
        PreparedStatus,
        SimpleNamespace(
            prepared=SimpleNamespace(
                status_revisions=(
                    SimpleNamespace(
                        revision=SimpleNamespace(change_id="change-1", commit_id="commit-1")
                    ),
                )
            )
        ),
    )
    status_result = SimpleNamespace(
        revisions=(
            _status_revision(
                change_id="change-1",
                commit_id="commit-1",
                pull_request=_pull_request(number=7),
                pull_request_state="open",
                subject="feature 1",
            ),
        )
    )

    plan = _build_land_plan(
        expect_pr_number=9,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is True
    assert plan.boundary_action is not None
    assert "`--expect-pr 9`" in plan.boundary_action.message


def test_updated_landed_change_marks_pr_merged_and_clears_stack_comment() -> None:
    updated = _updated_landed_change(
        bookmark="review/feature-1-aaaaaaaa",
        cached_change=CachedChange(
            bookmark="review/feature-1-aaaaaaaa",
            last_submitted_commit_id="old-commit",
            pr_number=1,
            pr_review_decision="approved",
            pr_state="open",
            pr_url="https://github.test/octo-org/stacked-review/pull/1",
            stack_comment_id=99,
        ),
        commit_id="new-commit",
        pull_request=GithubPullRequest(
            base=GithubBranchRef(ref="main"),
            head=GithubBranchRef(ref="review/feature-1-aaaaaaaa"),
            html_url="https://github.test/octo-org/stacked-review/pull/1",
            merged_at="2026-03-22T12:00:00Z",
            number=1,
            state="closed",
            title="feature 1",
        ),
    )

    assert updated.last_submitted_commit_id == "new-commit"
    assert updated.pr_review_decision is None
    assert updated.pr_state == "merged"
    assert updated.stack_comment_id is None


def test_parse_pull_request_reference_accepts_matching_url() -> None:
    assert (
        _parse_pull_request_reference(
            reference="https://github.test/octo-org/stacked-review/pull/17",
            github_repository=ResolvedGithubRepository(
                host="github.test",
                owner="octo-org",
                repo="stacked-review",
            ),
        )
        == 17
    )


def test_parse_pull_request_reference_rejects_wrong_repo() -> None:
    with pytest.raises(LandError, match="`--expect-pr`"):
        _parse_pull_request_reference(
            reference="https://github.test/other-org/stacked-review/pull/17",
            github_repository=ResolvedGithubRepository(
                host="github.test",
                owner="octo-org",
                repo="stacked-review",
            ),
        )


def _status_revision(
    *,
    change_id: str,
    commit_id: str,
    pull_request: GithubPullRequest,
    pull_request_state: str,
    subject: str,
):
    return SimpleNamespace(
        bookmark=f"review/{change_id}",
        change_id=change_id,
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            message=None,
            pull_request=pull_request,
            state=pull_request_state,
        ),
        remote_state=RemoteBookmarkState(remote="origin", targets=(commit_id,)),
        subject=subject,
    )


def _pull_request(*, number: int, state: str = "open") -> GithubPullRequest:
    merged_at = "2026-03-22T12:00:00Z" if state == "merged" else None
    pr_state = "closed" if state == "merged" else state
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(ref=f"review/{number}"),
        html_url=f"https://github.test/octo-org/stacked-review/pull/{number}",
        merged_at=merged_at,
        number=number,
        state=pr_state,
        title=f"feature {number}",
    )
