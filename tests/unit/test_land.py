from __future__ import annotations

from collections.abc import Sequence
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from jj_review import console
from jj_review.commands.land import (
    DivergenceKind,
    _build_land_plan,
    _ensure_trunk_branch_matches_selected_trunk,
    _find_resume_land_intent,
    _land_boundary_message,
    _plan_review_bookmark_cleanup,
    _remote_trunk_matches_commit,
    _report_stale_land_intents,
    _restore_local_trunk_bookmark,
    _resume_land_plan,
    _stack_not_on_trunk_message,
    _updated_landed_change,
)
from jj_review.errors import CliError
from jj_review.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_review.models.github import GithubBranchRef, GithubPullRequest
from jj_review.models.intent import LandIntent, LoadedIntent
from jj_review.models.review_state import CachedChange
from jj_review.review.status import PreparedStatus, ReviewStatusRevision, StatusResult
from jj_review.ui import plain_text


def _assume_diff_equivalent(local_commit_id: str, remote_target: str | None) -> DivergenceKind:
    if remote_target is None or remote_target == local_commit_id:
        return "in_sync"
    return "diff_equivalent"


def _assume_content_divergent(local_commit_id: str, remote_target: str | None) -> DivergenceKind:
    if remote_target is None or remote_target == local_commit_id:
        return "in_sync"
    return "content_divergent"


def test_build_land_plan_uses_maximal_ready_prefix() -> None:
    prepared_status = _prepared_status(("change-1", "change-2", "change-3"))
    status_result = cast(
        StatusResult,
        SimpleNamespace(
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
                    review_decision="approved",
                    subject="feature 2",
                ),
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=1),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 1",
                ),
            )
        ),
    )

    plan = _build_land_plan(
        bypass_readiness=False,
        classify_divergence=_assume_diff_equivalent,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is False
    assert [revision.pull_request_number for revision in plan.landed_revisions] == [1, 2]
    assert plan.boundary_action is not None
    assert plan.boundary_action.status == "planned"
    assert "before feature 3" in plan.boundary_action.message


@pytest.mark.parametrize(
    ("bypass_readiness", "revision_factory", "expected_message"),
    [
        pytest.param(
            False,
            lambda: _status_revision(
                change_id="change-1",
                commit_id="commit-1",
                link_state="unlinked",
                pull_request=_pull_request(number=7),
                pull_request_state="open",
                subject="feature 1",
            ),
            "unlinked from review tracking",
            id="unlinked",
        ),
        pytest.param(
            False,
            lambda: _status_revision(
                change_id="change-1",
                commit_id="commit-1",
                pull_request=_pull_request(number=7),
                pull_request_state="open",
                subject="feature 1",
            ),
            "PR #7 is not approved",
            id="unapproved-open",
        ),
        pytest.param(
            False,
            lambda: _status_revision(
                change_id="change-1",
                commit_id="commit-1",
                pull_request=_pull_request(number=7, draft=True),
                pull_request_state="open",
                review_decision="approved",
                subject="feature 1",
            ),
            "PR #7 is still a draft",
            id="draft",
        ),
        pytest.param(
            False,
            lambda: _status_revision(
                change_id="change-1",
                commit_id="commit-1",
                pull_request=_pull_request(number=7),
                pull_request_state="open",
                review_decision="changes_requested",
                subject="feature 1",
            ),
            "PR #7 has changes requested",
            id="changes-requested",
        ),
        pytest.param(
            True,
            lambda: _status_revision(
                change_id="change-1",
                commit_id="commit-1",
                pull_request=_pull_request(number=7),
                pull_request_state="open",
                review_decision_error="review decision lookup failed",
                subject="feature 1",
            ),
            "review decision lookup failed",
            id="review-decision-error",
        ),
    ],
)
def test_build_land_plan_blocks_unready_boundary_changes(
    bypass_readiness: bool,
    revision_factory,
    expected_message: str,
) -> None:
    prepared_status = _prepared_status(("change-1",))
    status_result = cast(
        StatusResult,
        SimpleNamespace(revisions=(revision_factory(),)),
    )

    plan = _build_land_plan(
        bypass_readiness=bypass_readiness,
        classify_divergence=_assume_diff_equivalent,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is True
    assert plan.boundary_action is not None
    assert expected_message in plan.boundary_action.message


def test_build_land_plan_allows_rebased_prefix_when_pr_link_still_matches() -> None:
    prepared_status = _prepared_status(("change-1",))
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    remote_target="old-commit-1",
                    pull_request=_pull_request(number=1),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 1",
                ),
            )
        ),
    )

    plan = _build_land_plan(
        bypass_readiness=False,
        classify_divergence=_assume_diff_equivalent,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is False
    assert [revision.pull_request_number for revision in plan.landed_revisions] == [1]
    assert [revision.needs_resubmit for revision in plan.landed_revisions] == [True]
    assert plan.resubmit_revisions == plan.landed_revisions
    assert plan.boundary_action is None


def test_build_land_plan_blocks_conflicted_local_revision() -> None:
    prepared_status = _prepared_status(("change-1",), conflicted_change_ids=("change-1",))
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=1),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 1",
                ),
            )
        ),
    )

    plan = _build_land_plan(
        bypass_readiness=False,
        classify_divergence=_assume_diff_equivalent,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is True
    assert plan.boundary_action is not None
    assert "still has unresolved conflicts" in plan.boundary_action.message


def test_build_land_plan_blocks_content_divergent_rebased_revision() -> None:
    prepared_status = _prepared_status(("change-1",))
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    remote_target="old-commit-1",
                    pull_request=_pull_request(number=1),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 1",
                ),
            )
        ),
    )

    plan = _build_land_plan(
        bypass_readiness=False,
        classify_divergence=_assume_content_divergent,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is True
    assert plan.boundary_action is not None
    assert "differs from what reviewers approved" in plan.boundary_action.message
    assert plan.landed_revisions == ()
    assert plan.resubmit_revisions == ()


def test_land_boundary_message_allows_rebased_revision_when_pr_link_is_ready() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        remote_target="other-tip",
        pull_request=_pull_request(number=1),
        pull_request_state="open",
        review_decision="approved",
        subject="feature 1",
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        classify_divergence=_assume_diff_equivalent,
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is None


def test_land_boundary_message_allows_ready_revision_without_remote_state() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        pull_request=_pull_request(number=1),
        pull_request_state="open",
        review_decision="approved",
        subject="feature 1",
        with_remote_state=False,
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        classify_divergence=_assume_diff_equivalent,
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is None


def test_land_boundary_message_blocks_content_divergent_revision() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        remote_target="old-commit-1",
        pull_request=_pull_request(number=1),
        pull_request_state="open",
        review_decision="approved",
        subject="feature 1",
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        classify_divergence=_assume_content_divergent,
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is not None
    assert "differs from what reviewers approved" in plain_text(message)


def test_land_boundary_message_prefers_unlinked_state_over_content_divergence() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        link_state="unlinked",
        remote_target="old-commit-1",
        pull_request=_pull_request(number=1),
        pull_request_state="open",
        review_decision="approved",
        subject="feature 1",
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        classify_divergence=_assume_content_divergent,
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is not None
    rendered = plain_text(message)
    assert "unlinked from review tracking" in rendered
    assert "differs from what reviewers approved" not in rendered


def test_land_boundary_message_prefers_missing_pr_over_content_divergence() -> None:
    prepared_revision = _prepared_status(("change-1",)).prepared.status_revisions[0]
    revision = _status_revision(
        change_id="change-1",
        commit_id="commit-1",
        remote_target="old-commit-1",
        pull_request=_pull_request(number=1),
        pull_request_state="missing",
        subject="feature 1",
    )

    message = _land_boundary_message(
        bypass_readiness=False,
        classify_divergence=_assume_content_divergent,
        prepared_revision=prepared_revision,
        revision=revision,
    )

    assert message is not None
    rendered = plain_text(message)
    assert "GitHub no longer reports a pull request" in rendered
    assert "differs from what reviewers approved" not in rendered


def test_build_land_plan_bypass_readiness_uses_maximal_open_prefix() -> None:
    prepared_status = _prepared_status(("change-1", "change-2", "change-3"))
    status_result = cast(
        StatusResult,
        SimpleNamespace(
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
                    pull_request=_pull_request(number=2, draft=True),
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
        ),
    )

    plan = _build_land_plan(
        bypass_readiness=True,
        classify_divergence=_assume_diff_equivalent,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is False
    assert [revision.pull_request_number for revision in plan.landed_revisions] == [1, 2]
    assert plan.boundary_action is not None
    assert "before feature 3" in plan.boundary_action.message


def test_stack_not_on_trunk_message_recommends_rebase_when_no_changes_are_merged() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"), selected_revset="@-")
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-2",
                    commit_id="commit-2",
                    pull_request=_pull_request(number=2),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 2",
                ),
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=1),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 1",
                ),
            ),
            selected_revset="@-",
        ),
    )

    message = _stack_not_on_trunk_message(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    rendered = plain_text(message)
    assert "jj rebase -s change-1 -d 'trunk()'" in rendered
    assert "cleanup --restack" not in rendered


def test_stack_not_on_trunk_message_recommends_cleanup_when_stack_has_merged_change() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"), selected_revset="@-")
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-2",
                    commit_id="commit-2",
                    pull_request=_pull_request(number=2),
                    pull_request_state="open",
                    review_decision="approved",
                    subject="feature 2",
                ),
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=1).model_copy(
                        update={"state": "merged", "merged_at": "2026-03-22T12:00:00Z"}
                    ),
                    pull_request_state="closed",
                    subject="feature 1",
                ),
            ),
            selected_revset="@-",
        ),
    )

    message = _stack_not_on_trunk_message(
        prepared_status=prepared_status,
        status_result=status_result,
    )

    rendered = plain_text(message)
    assert "cleanup --restack @-" in rendered
    assert "jj rebase -s" not in rendered


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


def test_find_resume_land_intent_matches_exact_path() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"))
    loaded_intent = _loaded_land_intent(
        ordered_change_ids=("change-1", "change-2"),
        ordered_commit_ids=("commit-1", "commit-2"),
        landed_change_ids=("change-1",),
    )

    result = _find_resume_land_intent(
        bypass_readiness=False,
        cleanup_bookmarks=True,
        current_landed_change_ids=("change-1",),
        prepared_status=prepared_status,
        selected_pr_number=None,
        stale_intents=(loaded_intent,),
        trunk_branch="main",
    )

    assert result is not None
    assert result.mode == "exact-path"


def test_find_resume_land_intent_matches_tail_after_landed_prefix() -> None:
    prepared_status = _prepared_status(
        ("change-2", "change-3"),
        commit_ids=("commit-2", "commit-3"),
    )
    loaded_intent = _loaded_land_intent(
        ordered_change_ids=("change-1", "change-2", "change-3"),
        ordered_commit_ids=("commit-1", "commit-2", "commit-3"),
        landed_change_ids=("change-1",),
    )

    result = _find_resume_land_intent(
        bypass_readiness=False,
        cleanup_bookmarks=True,
        current_landed_change_ids=("change-2", "change-3"),
        prepared_status=prepared_status,
        selected_pr_number=None,
        stale_intents=(loaded_intent,),
        trunk_branch="main",
    )

    assert result is not None
    assert result.mode == "tail-after-landed-prefix"


def test_find_resume_land_intent_returns_none_for_mismatch() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"))
    loaded_intent = _loaded_land_intent(
        ordered_change_ids=("change-1", "change-2"),
        ordered_commit_ids=("commit-1", "commit-2"),
        landed_change_ids=("change-1",),
        selected_pr_number=7,
        trunk_branch="main",
    )

    result = _find_resume_land_intent(
        bypass_readiness=False,
        cleanup_bookmarks=True,
        current_landed_change_ids=("change-1",),
        prepared_status=prepared_status,
        selected_pr_number=9,
        stale_intents=(loaded_intent,),
        trunk_branch="main",
    )

    assert result is None


def test_find_resume_land_intent_returns_none_for_cleanup_mode_mismatch() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"))
    loaded_intent = _loaded_land_intent(
        cleanup_bookmarks=False,
        ordered_change_ids=("change-1", "change-2"),
        ordered_commit_ids=("commit-1", "commit-2"),
        landed_change_ids=("change-1",),
    )

    result = _find_resume_land_intent(
        bypass_readiness=False,
        cleanup_bookmarks=True,
        current_landed_change_ids=("change-1",),
        prepared_status=prepared_status,
        selected_pr_number=None,
        stale_intents=(loaded_intent,),
        trunk_branch="main",
    )

    assert result is None


def test_report_stale_land_intents_does_not_claim_resume_without_resume_match() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"))
    loaded_intent = _loaded_land_intent(
        cleanup_bookmarks=False,
        ordered_change_ids=("change-1", "change-2"),
        ordered_commit_ids=("commit-1", "commit-2"),
        landed_change_ids=("change-1",),
    )

    stdout = StringIO()
    stderr = StringIO()
    with console.configured_console(stdout=stdout, stderr=stderr, color_mode="never"):
        _report_stale_land_intents(
            current_landed_change_ids=("change-1",),
            prepared_status=prepared_status,
            resume_intent=None,
            stale_intents=[loaded_intent],
        )

    rendered = stdout.getvalue()
    assert "Resuming interrupted" not in rendered
    assert "incomplete operation outstanding: land for change-2" in rendered


def test_remote_trunk_matches_commit_requires_matching_remote_and_local_state() -> None:
    client = _BookmarkClientStub(
        BookmarkState(
            name="main",
            local_targets=("commit-2",),
            remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-2",)),),
        )
    )

    assert (
        _remote_trunk_matches_commit(
            client=client,
            remote_name="origin",
            trunk_branch="main",
            commit_id="commit-2",
        )
        is True
    )
    assert (
        _remote_trunk_matches_commit(
            client=client,
            remote_name="origin",
            trunk_branch="main",
            commit_id="commit-1",
        )
        is False
    )


def test_resume_land_plan_skips_completed_change_ids() -> None:
    intent = cast(
        LandIntent,
        _loaded_land_intent(
            ordered_change_ids=("change-1", "change-2"),
            ordered_commit_ids=("commit-1", "commit-2"),
            landed_change_ids=("change-1", "change-2"),
            completed_change_ids=("change-1",),
        ).intent,
    )
    plan = _resume_land_plan(
        intent=intent,
        trunk_branch="main",
    )

    assert plan.blocked is False
    assert plan.push_trunk is False
    assert [revision.change_id for revision in plan.landed_revisions] == ["change-2"]
    assert [revision.pull_request_number for revision in plan.landed_revisions] == [2]


def test_resume_land_plan_rejects_incomplete_intent_data() -> None:
    intent = cast(
        LandIntent,
        _loaded_land_intent(
            ordered_change_ids=("change-1", "change-2"),
            ordered_commit_ids=("commit-1", "commit-2"),
            landed_change_ids=("change-1", "change-2"),
        ).intent,
    )
    broken_intent = intent.model_copy(update={"landed_subjects": {"change-1": "feature 1"}})

    with pytest.raises(CliError, match="Interrupted land intent"):
        _resume_land_plan(intent=broken_intent, trunk_branch="main")


def test_restore_local_trunk_bookmark_resets_existing_target() -> None:
    client = _BookmarkRestorerStub()

    _restore_local_trunk_bookmark(
        client=client,
        original_target="trunk-commit",
        trunk_branch="main",
    )

    assert client.set_calls == [("main", "trunk-commit", True)]
    assert client.forget_calls == []


def test_restore_local_trunk_bookmark_forgets_bookmark_when_original_target_missing() -> None:
    client = _BookmarkRestorerStub()

    _restore_local_trunk_bookmark(
        client=client,
        original_target=None,
        trunk_branch="main",
    )

    assert client.forget_calls == ["main"]
    assert client.set_calls == []


def test_plan_review_bookmark_cleanup_forgets_owned_landed_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1",),
        ),
        change_id="change-1",
        commit_id="commit-1",
    )

    assert plan is not None
    assert plan.can_forget is True
    assert plan.action.message == "forget review/feature-aaaaaaaa"
    assert plan.action.status == "planned"


def test_plan_review_bookmark_cleanup_blocks_conflicted_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-1", "commit-2"),
        ),
        change_id="change-1",
        commit_id="commit-1",
    )

    assert plan is not None
    assert plan.can_forget is False
    assert "is conflicted" in plan.action.message
    assert plan.action.status == "blocked"


def test_plan_review_bookmark_cleanup_blocks_moved_bookmark() -> None:
    plan = _plan_review_bookmark_cleanup(
        bookmark="review/feature-aaaaaaaa",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            local_targets=("commit-2",),
        ),
        change_id="change-1",
        commit_id="commit-1",
    )

    assert plan is not None
    assert plan.can_forget is False
    assert "points to a different revision" in plan.action.message
    assert plan.action.status == "blocked"


def test_ensure_trunk_branch_matches_selected_trunk_rejects_missing_remote_bookmark() -> None:
    client = _BookmarkClientStub(BookmarkState(name="main", local_targets=("commit-1",)))

    with pytest.raises(CliError, match="Remote trunk bookmark main@origin is not available"):
        _ensure_trunk_branch_matches_selected_trunk(
            client=client,
            remote_name="origin",
            trunk_branch="main",
            trunk_commit_id="commit-1",
        )


@pytest.mark.parametrize(
    ("bookmark_state", "message"),
    [
        pytest.param(
            BookmarkState(
                name="main",
                local_targets=("commit-1", "commit-2"),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            "Local trunk bookmark main is conflicted",
            id="conflicted-local",
        ),
        pytest.param(
            BookmarkState(
                name="main",
                local_targets=("commit-2",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-1",)),),
            ),
            "Local bookmark main points to a different revision",
            id="moved-local",
        ),
        pytest.param(
            BookmarkState(
                name="main",
                local_targets=("commit-1",),
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("commit-1", "commit-2")),
                ),
            ),
            "Remote trunk bookmark main@origin is conflicted",
            id="conflicted-remote",
        ),
        pytest.param(
            BookmarkState(
                name="main",
                local_targets=("commit-1",),
                remote_targets=(RemoteBookmarkState(remote="origin", targets=("commit-2",)),),
            ),
            "Remote trunk bookmark main@origin moved",
            id="moved-remote",
        ),
    ],
)
def test_ensure_trunk_branch_matches_selected_trunk_rejects_unsafe_bookmarks(
    bookmark_state: BookmarkState,
    message: str,
) -> None:
    client = _BookmarkClientStub(bookmark_state)

    with pytest.raises(CliError, match=message):
        _ensure_trunk_branch_matches_selected_trunk(
            client=client,
            remote_name="origin",
            trunk_branch="main",
            trunk_commit_id="commit-1",
        )


def _status_revision(
    *,
    change_id: str,
    commit_id: str,
    remote_target: str | None = None,
    with_remote_state: bool = True,
    pull_request: GithubPullRequest,
    pull_request_state: str,
    review_decision: str | None = None,
    review_decision_error: str | None = None,
    subject: str,
    link_state: str = "active",
) -> ReviewStatusRevision:
    return cast(
        ReviewStatusRevision,
        SimpleNamespace(
            bookmark=f"review/{change_id}",
            change_id=change_id,
            link_state=link_state,
            local_divergent=False,
            pull_request_lookup=SimpleNamespace(
                message=None,
                pull_request=pull_request,
                review_decision=review_decision,
                review_decision_error=review_decision_error,
                state=pull_request_state,
            ),
            remote_state=(
                RemoteBookmarkState(
                    remote="origin",
                    targets=((remote_target,) if remote_target is not None else (commit_id,)),
                )
                if with_remote_state
                else None
            ),
            subject=subject,
        ),
    )


def _pull_request(
    *,
    number: int,
    state: str = "open",
    draft: bool = False,
) -> GithubPullRequest:
    merged_at = "2026-03-22T12:00:00Z" if state == "merged" else None
    pr_state = "closed" if state == "merged" else state
    return GithubPullRequest(
        base=GithubBranchRef(ref="main"),
        draft=draft,
        head=GithubBranchRef(ref=f"review/{number}"),
        html_url=f"https://github.test/octo-org/stacked-review/pull/{number}",
        merged_at=merged_at,
        number=number,
        state=pr_state,
        title=f"feature {number}",
    )


def _prepared_status(
    change_ids: tuple[str, ...],
    *,
    commit_ids: tuple[str, ...] | None = None,
    conflicted_change_ids: tuple[str, ...] = (),
    selected_revset: str = "@-",
) -> PreparedStatus:
    resolved_commit_ids = commit_ids or tuple(
        f"commit-{index + 1}" for index, _change_id in enumerate(change_ids)
    )
    status_revisions = tuple(
        SimpleNamespace(
            revision=SimpleNamespace(
                change_id=change_id,
                commit_id=commit_id,
                conflict=change_id in conflicted_change_ids,
            )
        )
        for change_id, commit_id in zip(change_ids, resolved_commit_ids, strict=True)
    )
    return cast(
        PreparedStatus,
        SimpleNamespace(
            prepared=SimpleNamespace(
                stack=SimpleNamespace(trunk=SimpleNamespace(commit_id="trunk-commit")),
                status_revisions=status_revisions,
            ),
            selected_revset=selected_revset,
        ),
    )


def _loaded_land_intent(
    *,
    bypass_readiness: bool = False,
    cleanup_bookmarks: bool = True,
    ordered_change_ids: tuple[str, ...],
    ordered_commit_ids: tuple[str, ...],
    landed_change_ids: tuple[str, ...],
    completed_change_ids: tuple[str, ...] = (),
    selected_pr_number: int | None = None,
    trunk_branch: str = "main",
) -> LoadedIntent:
    return LoadedIntent(
        path=Path("/tmp/incomplete-land.json"),
        intent=LandIntent(
            kind="land",
            pid=123,
            label="land on @-",
            bypass_readiness=bypass_readiness,
            cleanup_bookmarks=cleanup_bookmarks,
            display_revset="@-",
            ordered_change_ids=ordered_change_ids,
            ordered_commit_ids=ordered_commit_ids,
            landed_change_ids=landed_change_ids,
            landed_bookmarks={
                change_id: f"review/{change_id}" for change_id in ordered_change_ids
            },
            landed_commit_ids={
                change_id: commit_id
                for change_id, commit_id in zip(
                    ordered_change_ids,
                    ordered_commit_ids,
                    strict=True,
                )
            },
            landed_pull_request_numbers={
                change_id: index + 1 for index, change_id in enumerate(ordered_change_ids)
            },
            landed_subjects={
                change_id: f"feature {index + 1}"
                for index, change_id in enumerate(ordered_change_ids)
            },
            completed_change_ids=completed_change_ids,
            trunk_branch=trunk_branch,
            trunk_commit_id="trunk-commit",
            landed_commit_id=ordered_commit_ids[len(landed_change_ids) - 1]
            if landed_change_ids
            else "trunk-commit",
            selected_pr_number=selected_pr_number,
            started_at="2026-03-22T12:00:00Z",
        ),
    )


class _BookmarkClientStub:
    def __init__(self, bookmark_state: BookmarkState) -> None:
        self._bookmark_state = bookmark_state

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        assert bookmark == self._bookmark_state.name
        return self._bookmark_state


class _BookmarkRestorerStub:
    def __init__(self) -> None:
        self.forget_calls: list[str] = []
        self.set_calls: list[tuple[str, str, bool]] = []

    def forget_bookmarks(self, bookmarks: Sequence[str]) -> None:
        self.forget_calls.extend(bookmarks)

    def set_bookmark(
        self,
        bookmark: str,
        revision: str,
        *,
        allow_backwards: bool = False,
    ) -> None:
        self.set_calls.append((bookmark, revision, allow_backwards))
