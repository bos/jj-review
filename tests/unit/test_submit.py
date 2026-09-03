from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import replace

import pytest

from jj_stack.commands.submit.changes import prepare_submit_changes
from jj_stack.commands.submit.command import (
    _pr_sync_plans,
)
from jj_stack.commands.submit.inputs import preflight_private_commits
from jj_stack.commands.submit.models import (
    GeneratedDescription,
    PreparedSubmitChange,
    SubmitOptions,
)
from jj_stack.commands.submit.overview_comments import sync_stack_overview_comments
from jj_stack.commands.submit.revision_comments import _include_submitted_force_push
from jj_stack.config import AppConfig
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.git import GitRemote
from jj_stack.models.github import (
    GithubBranchRef,
    GithubIssueComment,
    GithubPR,
    GithubPRRevision,
)
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import (
    TrackingState,
)
from jj_stack.stack.pr_branches import ResolvedPRBranch
from jj_stack.stack.status import PRLookup
from tests.support.change_helpers import make_change
from tests.support.contexts import fake_command_context

_REMOTE_URL = "https://github.test/octo-org/repo.git"
_REMOTE = GitRemote(name="origin", fetch_url=_REMOTE_URL, push_url=_REMOTE_URL)


def test_overview_comment_move_keeps_source_when_head_creation_fails() -> None:
    source_comment = GithubIssueComment(
        body="<!-- jj-stack-overview -->\nEdited",
        databaseId=7,
    )

    class CommentClientStub(GithubClient):
        def __init__(self) -> None:
            self.deleted_comment_ids: list[int] = []
            self._repo = GithubRepoAddress(owner="octo-org", repo="stacked-prs")

        async def find_issue_comments_by_body_marker(
            self,
            *,
            body_marker: str,
            pr_numbers: Sequence[int],
        ) -> dict[int, GithubIssueComment | None]:
            assert body_marker == "<!-- jj-stack-overview -->"
            assert tuple(pr_numbers) == (1, 2)
            return {1: source_comment, 2: None}

        async def create_issue_comment(
            self,
            *,
            issue_number: int,
            body: str,
        ) -> GithubIssueComment:
            assert issue_number == 2
            assert body == source_comment.body
            raise GithubClientError("create failed")

        async def delete_issue_comment(self, *, comment_id: int) -> None:
            self.deleted_comment_ids.append(comment_id)

    client = CommentClientStub()

    with pytest.raises(CliError, match="Could not create a stack overview comment"):
        asyncio.run(
            sync_stack_overview_comments(
                base_is_another_pr=False,
                comments_by_pr_number={1: source_comment, 2: None},
                concurrency=2,
                generated_stack_description=None,
                github_client=client,
                pr_numbers=(1, 2),
            )
        )

    assert client.deleted_comment_ids == []


def _prepare(
    change: LocalCommit,
    *,
    branch: str,
    lookup: PRLookup,
    remote_target: str | None,
    state: TrackingState,
    recovered_target: str | None = None,
):
    return prepare_submit_changes(
        branch_resolutions=(
            ResolvedPRBranch(
                branch=branch,
                change_id=change.change_id,
                recovered_target=recovered_target,
            ),
        ),
        lookups={branch: lookup},
        remote_targets={} if remote_target is None else {branch: remote_target},
        remote=_REMOTE,
        stack=_local_stack(change),
        state=state,
    )


def test_prepare_submit_changes_rejects_unclaimed_existing_branch() -> None:
    change = make_change(commit_id="current-commit", change_id="abcdefghijk", description="f\n")

    with pytest.raises(CliError, match="already exists"):
        _prepare(
            change,
            branch="jj-stack/feature-abcdefgh",
            lookup=PRLookup(pr=None, open_prs_on_branch=()),
            remote_target="another-commit",
            state=TrackingState(),
        )


def test_prepare_submit_changes_requires_recovered_branch_lease_to_stay_exact() -> None:
    change = make_change(commit_id="current-commit", change_id="abcdefghijk", description="f\n")

    with pytest.raises(CliError, match="changed while submit was running"):
        _prepare(
            change,
            branch="jj-stack/older-title-abcdefgh",
            lookup=PRLookup(pr=None, open_prs_on_branch=()),
            recovered_target="interrupted-commit",
            remote_target="external-commit",
            state=TrackingState(),
        )


def test_preflight_private_commits_rejects_blocked_change() -> None:
    private = make_change(
        commit_id="head",
        change_id="head-change",
        description="private thing\n",
    )

    class PrivateCommitClient:
        def find_private_commits(
            self,
            changes: tuple[LocalCommit, ...],
        ) -> tuple[LocalCommit, ...]:
            del changes
            return (private,)

    with pytest.raises(CliError, match="git.private-commits"):
        preflight_private_commits(PrivateCommitClient(), (private,))


def test_pr_plan_prefers_cli_metadata_over_config() -> None:
    context = fake_command_context(
        config=AppConfig(
            labels=["config-label"],
            reviewers=["config-user"],
            team_reviewers=["config-team"],
        ),
    )

    change = make_change(
        commit_id="current-commit",
        change_id="abcdefghijk",
        description="feature\n",
    )
    branch = "jj-stack/feature-abcdefgh"
    plans = _pr_sync_plans(
        bottom_base_branch="main",
        context=context,
        drafts={change.change_id: False},
        generated_descriptions={change.change_id: GeneratedDescription(body="", title="feature")},
        options=replace(
            _submit_options(),
            labels=["cli-label"],
            reviewers=["cli-user"],
        ),
        prepared_changes=(
            PreparedSubmitChange(
                branch=branch,
                expected_remote_target="old-commit",
                remote_action="pushed",
                change=change,
                pr=_github_pr(17, branch=branch),
            ),
        ),
        prior_reviewers={},
    )

    plan = plans[0]
    assert plan.action == "unchanged"
    assert plan.metadata is not None
    assert plan.metadata.labels == ["cli-label"]
    assert plan.metadata.reviewers == ["cli-user"]
    assert plan.metadata.team_reviewers == ["config-team"]


def _submit_options() -> SubmitOptions:
    return SubmitOptions(
        base_revset=None,
        descriptions=(),
        describe_with=None,
        draft_mode="default",
        dry_run=False,
        edit=False,
        existing_only=False,
        labels=None,
        re_request=False,
        reviewers=None,
        revset="@",
        team_reviewers=None,
    )


def _local_stack(*changes: LocalCommit) -> LocalStack:
    trunk = make_change(
        commit_id="trunk",
        change_id="trunk-change",
        description="base\n",
    )
    return LocalStack(
        base_parent=trunk,
        head=changes[-1],
        changes=changes,
        selected_revset=changes[-1].change_id,
        trunk=trunk,
    )


def _github_pr(
    number: int,
    *,
    branch: str = "jj-stack/foo",
    head_sha: str = "head-commit",
    state: str = "open",
) -> GithubPR:
    return GithubPR(
        base=GithubBranchRef(ref="main"),
        body="",
        head=GithubBranchRef(
            label=f"octo-org:{branch}",
            ref=branch,
            sha=head_sha,
        ),
        html_url=f"https://github.test/octo-org/repo/pull/{number}",
        number=number,
        state=state,
        title="feature",
    )


def test_revision_history_fills_only_the_force_push_github_has_not_indexed() -> None:
    def revision(version: int, before: str, after: str, *, current: bool) -> GithubPRRevision:
        return GithubPRRevision(
            before_commit_id=before, commit_id=after, is_current=current, version=version
        )

    indexed = (revision(2, "c1", "c2", current=True),)

    assert _include_submitted_force_push(indexed, None) == indexed
    assert _include_submitted_force_push(indexed, ("c1", "c2")) == indexed
    # A push that does not continue the indexed history is not this PR's next revision.
    assert _include_submitted_force_push(indexed, ("c9", "c3")) == indexed
    assert _include_submitted_force_push(indexed, ("c2", "c3")) == (
        revision(2, "c1", "c2", current=False),
        revision(3, "c2", "c3", current=True),
    )
    assert _include_submitted_force_push((), ("c1", "c2")) == (
        revision(2, "c1", "c2", current=True),
    )
