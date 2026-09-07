from __future__ import annotations

import asyncio
from typing import cast

from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import SubmittedBaseline, TrackedPR, TrackingState
from jj_stack.stack import status as status_module
from jj_stack.stack.status import (
    PreparedChange,
    PreparedStatus,
    prepare_stack_for_status,
    stream_status_async,
)
from tests.support.change_helpers import make_change
from tests.support.contexts import fake_command_context
from tests.support.tracking import make_pr_identity


def test_untracked_status_omits_branch_and_skips_github_discovery(
    monkeypatch,
) -> None:
    change = make_change(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    prepared = prepare_stack_for_status(
        context=fake_command_context(),
        remote=_STATUS_REMOTE,
        remote_error=None,
        stack=_stack_for_status(change),
        state=TrackingState(),
    )
    prepared_status = PreparedStatus(
        github_target=_github_target(),
        prepared=prepared,
    )

    async def fail_github_inspection(**_kwargs):
        if False:
            yield None
        raise AssertionError("untracked changes must not trigger GitHub inspection")

    monkeypatch.setattr(
        "jj_stack.stack.status._iter_status_changes_with_github",
        fail_github_inspection,
    )

    result = asyncio.run(
        stream_status_async(
            on_progress=lambda: None,
            prepared_status=prepared_status,
        )
    )

    assert result.changes[0].branch is None


def test_stream_status_falls_back_to_local_data_after_github_abort(monkeypatch) -> None:
    change = make_change(
        commit_id="commit-1",
        description="feature 1",
        change_id="aaaaaaaa1234",
    )
    state = TrackingState(
        prs={
            change.change_id: TrackedPR(
                pr_identity=make_pr_identity(head_ref="jj-stack/feature-1-aaaaaaaa", pr_number=1),
                submitted_baseline=SubmittedBaseline(commit_id=change.commit_id),
            )
        }
    )
    prepared = prepare_stack_for_status(
        context=fake_command_context(),
        remote=_STATUS_REMOTE,
        remote_error=None,
        stack=_stack_for_status(change),
        state=state,
    )
    prepared_status = PreparedStatus(
        github_target=_github_target(),
        prepared=prepared,
    )
    progress_updates = 0

    def on_progress() -> None:
        nonlocal progress_updates
        progress_updates += 1

    async def abort_github_inspection(**_kwargs):
        if False:
            yield None
        raise CliError("GitHub lookup failed")

    monkeypatch.setattr(
        "jj_stack.stack.status._iter_status_changes_with_github",
        abort_github_inspection,
    )

    result = asyncio.run(
        stream_status_async(
            on_progress=on_progress,
            prepared_status=prepared_status,
        )
    )

    assert progress_updates == 1
    assert result.github_error == "GitHub lookup failed"
    assert result.incomplete is True
    assert result.changes[0].branch == "jj-stack/feature-1-aaaaaaaa"


def test_pr_lookup_reports_the_saved_pr_when_another_open_pr_uses_its_branch() -> None:
    def pr_payload(number: int, state: str) -> GithubPR:
        return GithubPR.model_validate(
            {
                "base": {"ref": "main"},
                "head": {
                    "label": "octo-org:jj-stack/branch",
                    "ref": "jj-stack/branch",
                    "sha": "head-commit",
                },
                "html_url": f"https://github.test/octo-org/stacked-prs/pull/{number}",
                "node_id": f"PR_{number}",
                "number": number,
                "state": state,
                "title": f"feature {number}",
            }
        )

    class FakeGithubClient:
        repo = GithubRepoAddress(owner="octo-org", repo="stacked-prs")

        async def get_open_prs_by_head_refs(self, *, head_refs):
            return {"jj-stack/branch": (pr_payload(180, "open"),)}

        async def get_prs_by_numbers(self, *, pr_numbers):
            assert pr_numbers == (155,)
            return {155: pr_payload(155, "closed")}

    prepared_change = PreparedChange(
        change=make_change(change_id="change", commit_id="commit", description="feature\n"),
        tracked=TrackedPR(
            pr_identity=make_pr_identity(head_ref="jj-stack/branch", pr_number=155),
            submitted_baseline=SubmittedBaseline(commit_id="commit"),
        ),
    )

    lookup = asyncio.run(
        status_module.discover_pr_lookups(
            github_client=cast(GithubClient, FakeGithubClient()),
            tracked_by_branch={prepared_change.branch or "": prepared_change.tracked},
        )
    )["jj-stack/branch"]

    assert lookup.pr is not None and lookup.pr.number == 155
    assert tuple(pr.number for pr in lookup.open_prs_on_branch) == (180,)


_STATUS_REMOTE = GitRemote(
    name="origin",
    fetch_url="git@github.com:octo-org/stacked-prs.git",
    push_url="git@github.com:octo-org/stacked-prs.git",
)


def _github_target() -> GithubTarget:
    return GithubTarget(
        remote=_STATUS_REMOTE,
        repo=GithubRepoAddress(
            owner="octo-org",
            repo="stacked-prs",
        ),
    )


def _stack_for_status(*changes: LocalCommit) -> LocalStack:
    trunk = make_change(
        commit_id="trunk",
        description="base",
        change_id="trunkchangeid",
    )
    return LocalStack(
        base_parent=trunk,
        head=changes[-1],
        changes=tuple(changes),
        selected_revset="@",
        trunk=trunk,
    )
