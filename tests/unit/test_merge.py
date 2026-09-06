from __future__ import annotations

import pytest

from jj_stack.cli import build_parser, main
from jj_stack.commands.merge.command import _resolve_merge_method
from jj_stack.commands.merge.models import MergeChange
from jj_stack.commands.merge.preconditions import merge_precondition_error
from jj_stack.errors import EXIT_USAGE, CliError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubBranchRef, GithubPR, GithubRepo
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR
from jj_stack.stack.pr_facts import PRFacts, RepoFacts
from jj_stack.ui import plain_text
from tests.support.change_helpers import make_change


def _repo(
    *,
    allow_merge_commit: bool | None,
    allow_rebase_merge: bool | None,
    allow_squash_merge: bool | None,
) -> GithubRepo:
    return GithubRepo(
        allow_merge_commit=allow_merge_commit,
        allow_rebase_merge=allow_rebase_merge,
        allow_squash_merge=allow_squash_merge,
        default_branch="main",
        full_name="acme/widgets",
    )


def test_command_surface_has_merge_without_land_or_transport_flags(capsys) -> None:
    parser = build_parser()

    args = parser.parse_args(["merge", "--dry-run", "--method", "squash"])
    assert args.command == "merge"
    assert args.dry_run is True
    assert args.merge_method == "squash"

    assert main(["land"]) == EXIT_USAGE
    assert "Unknown command land" in capsys.readouterr().err
    with pytest.raises(CliError):
        parser.parse_args(["merge", "--via", "push"])
    with pytest.raises(CliError):
        parser.parse_args(["merge", "--bypass-readiness"])
    with pytest.raises(CliError):
        parser.parse_args(["merge", "--skip-cleanup"])


@pytest.mark.merge_recovery
def test_resolve_merge_method_uses_the_only_allowed_method() -> None:
    repo = _repo(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=True,
    )

    assert _resolve_merge_method(configured=None, merge_method=None, repo_state=repo) == "squash"


@pytest.mark.merge_recovery
@pytest.mark.parametrize(
    ("repo", "message"),
    (
        (
            _repo(
                allow_merge_commit=True,
                allow_rebase_merge=True,
                allow_squash_merge=False,
            ),
            "more than one merge method",
        ),
        (
            _repo(
                allow_merge_commit=None,
                allow_rebase_merge=None,
                allow_squash_merge=None,
            ),
            "did not report which merge methods",
        ),
        (
            _repo(
                allow_merge_commit=False,
                allow_rebase_merge=False,
                allow_squash_merge=False,
            ),
            "does not allow any pull request merge method",
        ),
    ),
)
def test_resolve_merge_method_rejects_ambiguous_or_absent_settings(
    repo: GithubRepo,
    message: str,
) -> None:
    with pytest.raises(CliError, match=message):
        _resolve_merge_method(configured=None, merge_method=None, repo_state=repo)


@pytest.mark.merge_recovery
def test_resolve_merge_method_prefers_the_flag_over_configuration() -> None:
    """A repo allowing several methods is the normal case, so config has to settle it.

    GitHub reports which methods it allows but never which to prefer, so without a configured
    default every merge in such a repo needs the flag typed out.
    """

    repo = _repo(
        allow_merge_commit=True,
        allow_rebase_merge=True,
        allow_squash_merge=True,
    )

    assert (
        _resolve_merge_method(configured="squash", merge_method=None, repo_state=repo) == "squash"
    )
    assert (
        _resolve_merge_method(configured="squash", merge_method="merge", repo_state=repo)
        == "merge"
    )
    # A repo whose allowed methods GitHub does not report is still configured.
    unreported = _repo(
        allow_merge_commit=None,
        allow_rebase_merge=None,
        allow_squash_merge=None,
    )
    assert (
        _resolve_merge_method(configured="squash", merge_method=None, repo_state=unreported)
        == "squash"
    )


@pytest.mark.merge_recovery
def test_resolve_merge_method_rejects_a_method_the_repo_disallows() -> None:
    repo = _repo(
        allow_merge_commit=False,
        allow_rebase_merge=False,
        allow_squash_merge=True,
    )

    with pytest.raises(CliError, match="does not allow"):
        _resolve_merge_method(configured="rebase", merge_method=None, repo_state=repo)


@pytest.mark.merge_recovery
def test_merge_preconditions_reject_repo_drift() -> None:
    expected_repo = GithubRepoAddress(
        owner="acme",
        repo="widgets",
    )
    observation = RepoFacts(
        configured_repo=GithubRepoAddress(
            owner="other",
            repo="widgets",
        ),
        github_repo=_repo(
            allow_merge_commit=False,
            allow_rebase_merge=False,
            allow_squash_merge=True,
        ),
        prs_by_base=None,
        remote=GitRemote(
            name="origin",
            fetch_url="https://github.test/acme/widgets.git",
            push_url="https://github.test/acme/widgets.git",
        ),
        repo=expected_repo,
        prs={},
    )

    error = merge_precondition_error(
        expected_repo=expected_repo,
        expected_trunk_branch="main",
        observation=observation,
        remote_name="origin",
        change=MergeChange(
            base_ref="main",
            change_id="a" * 32,
            commit_id="c" * 40,
            identity=PRIdentity(pr_number=1, head_ref="jj-stack/feature-aaaaaaaa"),
        ),
    )

    assert error is not None
    assert error.reason == "the configured Git remote no longer names the planned GitHub repo"
    assert error.recovery == "inspect"


@pytest.mark.merge_recovery
def test_merge_preconditions_name_a_closed_pull_request() -> None:
    """A closed pull request is reported as closed, not as unspecified drift."""

    repo = GithubRepoAddress(owner="acme", repo="widgets")
    remote = GitRemote(
        name="origin",
        fetch_url="https://github.test/acme/widgets.git",
        push_url="https://github.test/acme/widgets.git",
    )
    identity = PRIdentity(pr_number=1, head_ref="jj-stack/feature-abcdefgh")
    change = make_change(change_id="abcdefghijkl", commit_id="submitted", description="feature\n")
    closed_pr = GithubPR(
        base=GithubBranchRef(ref="main"),
        head=GithubBranchRef(ref=identity.head_ref, sha=change.commit_id),
        html_url="https://github.test/acme/widgets/pull/1",
        number=1,
        state="closed",
        title="feature",
    )
    observation = RepoFacts(
        configured_repo=repo,
        github_repo=_repo(
            allow_merge_commit=False, allow_rebase_merge=False, allow_squash_merge=True
        ),
        prs_by_base=None,
        remote=remote,
        repo=repo,
        prs={
            change.change_id: PRFacts(
                open_head_prs=(),
                local_commits=(change,),
                pr=closed_pr,
                remote_pr_branch_target=change.commit_id,
                tracked=TrackedPR(
                    pr_identity=identity,
                    submitted_baseline=SubmittedBaseline(commit_id=change.commit_id),
                ),
            )
        },
        observed_open_head_prs=True,
        observed_remote_targets=True,
    )

    error = merge_precondition_error(
        expected_repo=repo,
        expected_trunk_branch="main",
        observation=observation,
        remote_name="origin",
        change=MergeChange(
            base_ref="main",
            change_id=change.change_id,
            commit_id=change.commit_id,
            identity=identity,
        ),
    )

    assert error is not None
    assert "#1 is closed" in plain_text(error.reason)
    assert error.recovery == "inspect"
