from __future__ import annotations

from dataclasses import replace as dataclass_replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from jj_review.commands.land import (
    LandError,
    _build_land_plan,
    _find_resume_land_intent,
    _LandPreviewSnapshot,
    _parse_pull_request_reference,
    _planned_land_actions,
    _remote_trunk_matches_commit,
    _require_matching_land_preview,
    _restore_local_trunk_bookmark,
    _resume_land_plan,
    _updated_landed_change,
    _write_land_preview,
)
from jj_review.commands.review_state import PreparedStatus, StatusResult
from jj_review.commands.submit import ResolvedGithubRepository
from jj_review.models.bookmarks import BookmarkState, RemoteBookmarkState
from jj_review.models.cache import CachedChange
from jj_review.models.github import GithubBranchRef, GithubPullRequest
from jj_review.models.intent import LandIntent, LoadedIntent


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
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=7),
                    pull_request_state="open",
                    subject="feature 1",
                ),
            )
        ),
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


def test_build_land_plan_blocks_detached_change() -> None:
    prepared_status = _prepared_status(("change-1",))
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    link_state="detached",
                    pull_request=_pull_request(number=7),
                    pull_request_state="open",
                    subject="feature 1",
                ),
            )
        ),
    )

    plan = _build_land_plan(
        expect_pr_number=None,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert plan.blocked is True
    assert plan.boundary_action is not None
    assert "detached from managed review" in plan.boundary_action.message


def test_build_land_plan_raises_assertion_when_status_revision_is_missing() -> None:
    prepared_status = _prepared_status(("change-1",))
    status_result = cast(StatusResult, SimpleNamespace(revisions=()))

    with pytest.raises(
        AssertionError,
        match="Prepared land revision 'change-1' is missing from the status result.",
    ):
        _build_land_plan(
            expect_pr_number=None,
            prepared_status=prepared_status,
            status_result=status_result,
            trunk_branch="main",
        )


def test_planned_land_actions_omit_mutations_when_plan_is_blocked() -> None:
    prepared_status = _prepared_status(("change-1",))
    status_result = cast(
        StatusResult,
        SimpleNamespace(
            revisions=(
                _status_revision(
                    change_id="change-1",
                    commit_id="commit-1",
                    pull_request=_pull_request(number=7),
                    pull_request_state="open",
                    subject="feature 1",
                ),
            )
        ),
    )

    plan = _build_land_plan(
        expect_pr_number=9,
        prepared_status=prepared_status,
        status_result=status_result,
        trunk_branch="main",
    )

    assert _planned_land_actions(plan=plan) == (plan.boundary_action,)


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


def test_find_resume_land_intent_matches_exact_path() -> None:
    prepared_status = _prepared_status(("change-1", "change-2"))
    loaded_intent = _loaded_land_intent(
        ordered_change_ids=("change-1", "change-2"),
        ordered_commit_ids=("commit-1", "commit-2"),
        landed_change_ids=("change-1",),
    )

    result = _find_resume_land_intent(
        expect_pr_number=None,
        prepared_status=prepared_status,
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
        expect_pr_number=None,
        prepared_status=prepared_status,
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
        expected_pr_number=7,
        trunk_branch="main",
    )

    result = _find_resume_land_intent(
        expect_pr_number=9,
        prepared_status=prepared_status,
        stale_intents=(loaded_intent,),
        trunk_branch="main",
    )

    assert result is None


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


def test_require_matching_land_preview_requires_saved_preview(tmp_path: Path) -> None:
    with pytest.raises(
        LandError,
        match=r"requires a saved preview\. Run `land --expect-pr 5 @-` first\.",
    ):
        _require_matching_land_preview(
            current_snapshot=_preview_snapshot(
                expect_pr_number=5,
                landed_commit_ids=("commit-1",),
            ),
            selected_revset="@-",
            state_dir=tmp_path,
        )


def test_require_matching_land_preview_rejects_changed_preview(tmp_path: Path) -> None:
    _write_land_preview(
        tmp_path,
        _preview_snapshot(
            expect_pr_number=5,
            landed_change_ids=("change-1",),
            landed_commit_ids=("commit-1",),
            landed_pull_request_numbers=(7,),
        ),
    )

    with pytest.raises(
        LandError,
        match=r"changed since the saved preview\. Run `land --expect-pr 5 @-` again",
    ):
        _require_matching_land_preview(
            current_snapshot=_preview_snapshot(
                landed_change_ids=("change-1", "change-2"),
                landed_commit_ids=("commit-1", "commit-2"),
                landed_pull_request_numbers=(7, 8),
            ),
            selected_revset="@-",
            state_dir=tmp_path,
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
    broken_intent = dataclass_replace(
        intent,
        landed_subjects={"change-1": "feature 1"},
    )

    with pytest.raises(LandError, match="Interrupted land intent"):
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


def _status_revision(
    *,
    change_id: str,
    commit_id: str,
    pull_request: GithubPullRequest,
    pull_request_state: str,
    subject: str,
    link_state: str = "active",
):
    return SimpleNamespace(
        bookmark=f"review/{change_id}",
        change_id=change_id,
        link_state=link_state,
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


def _prepared_status(
    change_ids: tuple[str, ...],
    *,
    commit_ids: tuple[str, ...] | None = None,
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
    ordered_change_ids: tuple[str, ...],
    ordered_commit_ids: tuple[str, ...],
    landed_change_ids: tuple[str, ...],
    completed_change_ids: tuple[str, ...] = (),
    expected_pr_number: int | None = None,
    trunk_branch: str = "main",
) -> LoadedIntent:
    return LoadedIntent(
        path=Path("/tmp/incomplete-land.toml"),
        intent=LandIntent(
            kind="land",
            pid=123,
            label="land on @-",
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
            expected_pr_number=expected_pr_number,
            started_at="2026-03-22T12:00:00Z",
        ),
    )


def _preview_snapshot(
    *,
    expect_pr_number: int | None = None,
    landed_change_ids: tuple[str, ...] = (),
    landed_commit_ids: tuple[str, ...] = (),
    landed_pull_request_numbers: tuple[int, ...] = (),
) -> _LandPreviewSnapshot:
    return _LandPreviewSnapshot(
        boundary_message=None,
        expect_pr_number=expect_pr_number,
        github_repository="octo-org/stacked-review",
        landed_change_ids=landed_change_ids,
        landed_commit_ids=landed_commit_ids,
        landed_pull_request_numbers=landed_pull_request_numbers,
        ordered_change_ids=("change-1",),
        ordered_commit_ids=("commit-1",),
        remote_name="origin",
        selected_revset="@-",
        trunk_branch="main",
        trunk_commit_id="trunk-commit",
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

    def forget_bookmark(self, bookmark: str) -> None:
        self.forget_calls.append(bookmark)

    def set_bookmark(
        self,
        bookmark: str,
        revision: str,
        *,
        allow_backwards: bool = False,
    ) -> None:
        self.set_calls.append((bookmark, revision, allow_backwards))
