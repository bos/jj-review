from __future__ import annotations

import asyncio
from typing import cast

import jj_stack.ui as ui
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import SubmittedBaseline, TrackingState
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
    client = _PrepareStatusClient(_stack_for_status(change))
    prepared = prepare_stack_for_status(
        context=fake_command_context(jj_client=cast(JjClient, client)),
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
            on_change=None,
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
        pr_identities={
            change.change_id: make_pr_identity(
                head_ref="jj-stack/feature-1-aaaaaaaa",
                pr_number=1,
            )
        },
        submitted_baselines={change.change_id: SubmittedBaseline(commit_id=change.commit_id)},
    )
    client = _PrepareStatusClient(_stack_for_status(change))
    prepared = prepare_stack_for_status(
        context=fake_command_context(jj_client=cast(JjClient, client)),
        remote=_STATUS_REMOTE,
        remote_error=None,
        stack=_stack_for_status(change),
        state=state,
    )
    prepared_status = PreparedStatus(
        github_target=_github_target(),
        prepared=prepared,
    )
    streamed: list[tuple[str, bool]] = []

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
            on_change=lambda item, github_available: streamed.append(
                (item.change_id, github_available)
            ),
            prepared_status=prepared_status,
        )
    )

    assert streamed == [(change.change_id, False)]
    assert result.github_error == "GitHub lookup failed"
    assert result.incomplete is True
    assert result.changes[0].branch == "jj-stack/feature-1-aaaaaaaa"


def test_pr_lookup_falls_back_to_exact_remembered_pr_number() -> None:
    class FakeGithubClient:
        repo = GithubRepoAddress(
            owner="octo-org",
            repo="stacked-prs",
        )

        async def get_open_prs_by_head_refs(self, *, head_refs):
            assert head_refs == ("jj-stack/old-branch",)
            return {"jj-stack/old-branch": ()}

        async def get_prs_by_numbers(self, *, pr_numbers):
            assert pr_numbers == (7,)
            return {
                7: GithubPR.model_validate(
                    {
                        "base": {"ref": "jj-stack/base"},
                        "head": {
                            "label": "octo-org:jj-stack/old-branch",
                            "ref": "jj-stack/old-branch",
                        },
                        "html_url": "https://github.test/octo-org/stacked-prs/pull/7",
                        "merged_at": "2026-03-16T12:00:00Z",
                        "number": 7,
                        "state": "closed",
                        "title": "feature 7",
                    }
                )
            }

    prepared_change = PreparedChange(
        branch="jj-stack/old-branch",
        change=make_change(
            change_id="feature7change",
            commit_id="old-commit",
            description="feature 7\n",
        ),
        pr_identity=make_pr_identity(
            head_ref="jj-stack/old-branch",
            pr_number=7,
        ),
        submitted_baseline=None,
    )

    lookups = asyncio.run(
        status_module._discover_pr_lookups(
            github_client=cast(GithubClient, FakeGithubClient()),
            prepared_changes=(prepared_change,),
        )
    )

    lookup = lookups["jj-stack/old-branch"]
    assert lookup.source == "remembered"
    assert lookup.state == "closed"
    assert lookup.pr is not None
    assert lookup.pr.number == 7
    assert lookup.pr.state == "merged"


def test_pr_lookup_reports_the_saved_pr_when_another_open_pr_uses_its_branch() -> None:
    def pr_payload(number: int, state: str) -> GithubPR:
        return GithubPR.model_validate(
            {
                "base": {"ref": "main"},
                "head": {"label": "octo-org:jj-stack/branch", "ref": "jj-stack/branch"},
                "html_url": f"https://github.test/octo-org/stacked-prs/pull/{number}",
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
        branch="jj-stack/branch",
        change=make_change(change_id="change", commit_id="commit", description="feature\n"),
        pr_identity=make_pr_identity(head_ref="jj-stack/branch", pr_number=155),
        submitted_baseline=None,
    )

    lookup = asyncio.run(
        status_module._discover_pr_lookups(
            github_client=cast(GithubClient, FakeGithubClient()),
            prepared_changes=(prepared_change,),
        )
    )["jj-stack/branch"]

    assert lookup.pr is not None and lookup.pr.number == 155
    assert (lookup.source, lookup.state) == ("remembered", "closed")
    assert "#180" in ui.plain_text(lookup.message or "")


def test_pr_lookup_ignores_draft_review_decision() -> None:
    lookup = status_module._pr_lookup_from_discovered(
        head_label="octo-org:jj-stack/draft",
        prs=(
            GithubPR(
                base={"ref": "main"},
                draft=True,
                head={"ref": "jj-stack/draft"},
                html_url="https://github.test/octo-org/stacked-prs/pull/3",
                number=3,
                review_decision="approved",
                state="open",
                title="draft",
            ),
        ),
    )

    assert lookup.review_decision is None


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


class _PrepareStatusClient:
    def __init__(self, stack: LocalStack) -> None:
        self.stack = stack

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        return (_STATUS_REMOTE,)
