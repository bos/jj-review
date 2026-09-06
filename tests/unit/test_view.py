from __future__ import annotations

from io import StringIO

import jj_stack.commands.view as view_module
import jj_stack.console as console_module
import jj_stack.ui as ui_module
from jj_stack.models.github import GithubBranchRef, GithubPR
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR
from jj_stack.stack.change_state import UNOBSERVED, ChangeObservation, classify
from jj_stack.stack.status import (
    PRLookup,
    StackStatusChange,
    StatusResult,
)
from tests.support.change_helpers import make_change
from tests.support.tracking import make_pr_identity


def _pr(*, base_ref: str = "main", number: int, state: str) -> GithubPR:
    return GithubPR(
        base=GithubBranchRef(ref=base_ref),
        head=GithubBranchRef(ref="jj-stack/feature"),
        html_url=f"https://github.test/octo-org/repo/pull/{number}",
        number=number,
        state=state,
        title="feature",
    )


def _lookup(*, pr: GithubPR | None = None, error: str | None = None) -> PRLookup:
    return PRLookup(
        pr=pr,
        open_prs_on_branch=(pr,) if pr is not None and pr.state == "open" else (),
        error=error,
    )


def _status_result(
    *,
    changes: tuple[StackStatusChange, ...],
    selected_revset: str = "@",
) -> StatusResult:
    return StatusResult(
        changes=changes,
        github_error=None,
        github_repo=None,
        incomplete=False,
        remote=None,
        remote_error=None,
        selected_revset=selected_revset,
    )


def _status_change(
    *,
    change_id: str,
    commit_id: str = "commit-1",
    pr_lookup: PRLookup | None = None,
    pr_identity: PRIdentity | None = None,
    submitted_baseline: SubmittedBaseline | None = None,
    subject: str = "feature",
) -> StackStatusChange:
    change = make_change(change_id=change_id, commit_id=commit_id, description=f"{subject}\n")
    tracked = (
        TrackedPR(
            pr_identity=pr_identity,
            submitted_baseline=submitted_baseline or SubmittedBaseline(commit_id=commit_id),
        )
        if pr_identity is not None
        else None
    )
    observation = ChangeObservation(
        change_id=change_id,
        tracked=tracked,
        branch=pr_identity.head_ref if pr_identity is not None else None,
        local=(change,),
        selected=change,
        pr=UNOBSERVED if pr_lookup is None else pr_lookup.pr,
        open_prs_on_branch=UNOBSERVED if pr_lookup is None else pr_lookup.open_prs_on_branch,
        lookup_error=None if pr_lookup is None else pr_lookup.error,
    )
    return StackStatusChange(change=change, tracked=tracked, state=classify(observation))


def _render_lines(*lines: ui_module.Renderable) -> tuple[str, ...]:
    stdout = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=StringIO(), color_mode="never"):
        for line in lines:
            console_module.output(line)
    return tuple(stdout.getvalue().splitlines())


def test_view_advises_cleanup_and_rebase_when_merged_pr_remains_in_stack() -> None:
    merged_change = _status_change(
        change_id="abcdefghijkl",
        pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=5),
        pr_lookup=_lookup(pr=_pr(base_ref="team/feature-base", number=5, state="merged")),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=(merged_change,)),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Advisories:" in lines
    assert "jj-stack sync @" in normalized_lines
    assert "jj-stack sync --dry-run @" in normalized_lines
    assert normalized_lines.index("jj-stack sync --dry-run @") < normalized_lines.index(
        "jj-stack sync @"
    )
    assert "PR #5 is merged" in normalized_lines
    assert "later local changes are still based on it" in normalized_lines


def test_view_advises_submit_when_selected_stack_changed_since_submit() -> None:
    edited = tuple(
        _status_change(
            change_id=change_id,
            commit_id=f"rewritten-{change_id}",
            pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=number),
            submitted_baseline=SubmittedBaseline(commit_id=f"submitted-{change_id}"),
            pr_lookup=_lookup(
                pr=_pr(number=number, state="open").model_copy(
                    update={
                        "head": GithubBranchRef(
                            ref="jj-stack/feature", sha=f"submitted-{change_id}"
                        )
                    }
                )
            ),
        )
        for change_id, number in (("abcdefghijkl", 1), ("bcdefghijklm", 2))
    )
    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=edited, selected_revset="ulxwxsqw"),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Advisories:" in lines
    assert "New commit IDs" in normalized_lines
    assert "abcdefgh" in normalized_lines
    assert "bcdefghi" in normalized_lines


def test_view_advises_checkout_or_replace_when_a_pr_branch_moved() -> None:
    pr = _pr(number=7, state="open").model_copy(
        update={"head": GithubBranchRef(ref="jj-stack/feature", sha="f" * 40)}
    )
    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(
                changes=(
                    _status_change(
                        change_id="abcdefghijkl",
                        commit_id="local-commit",
                        pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=7),
                        pr_lookup=_lookup(pr=pr),
                        submitted_baseline=SubmittedBaseline(commit_id="submitted-commit"),
                    ),
                ),
            ),
        )
    )
    normalized = " ".join(" ".join(line.split()) for line in lines)

    assert "PR branch moved" in normalized
    assert "jj-stack checkout --pull-request 7" in normalized
    assert "jj-stack relink --replace-remote 7 abcdefgh" in normalized
    assert "Submit needed" not in normalized


def test_view_closed_pr_advisory_guides_reopen_relink_or_cleanup() -> None:
    change = _status_change(
        change_id="loqvlqrqabcdefghijkl",
        pr_identity=make_pr_identity(head_ref="jj-stack/feature", pr_number=21216),
        pr_lookup=_lookup(pr=_pr(number=21216, state="closed")),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=(change,)),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Closed GitHub PR" in normalized_lines
    assert "GitHub reports a closed PR for the change shown above" in normalized_lines
    assert "Reopen the PR on GitHub to continue using it" in normalized_lines
    assert "relink an open replacement" in normalized_lines
    assert "jj-stack cleanup @" in normalized_lines
    assert "changes below" not in normalized_lines


def test_view_missing_pr_advisory_guides_fetch_relink_or_cleanup() -> None:
    change = _status_change(
        pr_identity=make_pr_identity(
            head_ref="jj-stack/feature-8-abcdefgh",
            pr_number=42,
        ),
        change_id="abcdefgh1234",
        pr_lookup=_lookup(),
    )

    lines = _render_lines(
        *view_module.render_status_advisory_lines(
            result=_status_result(changes=(change,)),
        )
    )
    normalized_lines = " ".join(" ".join(line.split()) for line in lines)

    assert "Missing GitHub PR" in normalized_lines
    assert "GitHub did not report a PR for the saved PR branch" in normalized_lines
    assert "jj git fetch" in normalized_lines
    assert "Relink an open PR if one exists" in normalized_lines
    assert "jj-stack unstack --local @" in normalized_lines
    assert "GitHub did not report saved PR #42 for this branch" in normalized_lines


def test_view_summary_does_not_call_tracked_missing_pr_not_submitted() -> None:
    change = _status_change(
        pr_identity=make_pr_identity(
            head_ref="jj-stack/feature-8-abcdefgh",
            pr_number=8,
        ),
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        pr_lookup=_lookup(),
        subject="feature 8",
    )

    lines = _render_lines(
        *view_module.render_status_summary_lines(
            prerendered_blocks={
                change.commit_id: (
                    f"○  {change.change_id[:8]} {change.commit_id[:8]}",
                    f"│  {change.subject}",
                )
            },
            leading_separator=False,
            result=_status_result(changes=(change,)),
            verbose=False,
        )
    )

    assert lines == (
        "Submitted stack:",
        "○  abcdefgh 12345678: saved PR #8, no PR found for branch",
        "│  feature 8",
    )


def test_view_joins_summary_to_base_without_a_dangling_graph_edge() -> None:
    lines = view_module.render_trunk_status_lines(
        ("◆  base", "\033[38;5;8m│\033[39m"),
    )

    assert lines == ("◆  base",)


def test_view_summary_labels_row_when_pr_lookup_fails() -> None:
    change = _status_change(
        pr_identity=make_pr_identity(
            head_ref="jj-stack/feature-1-abcdefgh",
            pr_number=1,
        ),
        change_id="abcdefgh1234",
        commit_id="1234567890abcdef",
        pr_lookup=_lookup(error="pull request lookup failed"),
        subject="feature 1",
    )

    lines = _render_lines(
        *view_module.render_status_summary_lines(
            prerendered_blocks={
                change.commit_id: (
                    f"○  {change.change_id[:8]} {change.commit_id[:8]}",
                    f"│  {change.subject}",
                )
            },
            leading_separator=False,
            result=_status_result(changes=(change,)),
            verbose=False,
        )
    )

    normalized_lines = " ".join(lines)
    assert "saved PR #1, pull request lookup failed" in normalized_lines


def test_view_summary_truncates_middle_of_long_unsubmitted_sections() -> None:
    changes = tuple(
        _status_change(
            change_id=f"{index}" * 12,
            commit_id=f"commit-{index}",
            subject=f"feature {index}",
        )
        for index in range(8, 0, -1)
    )

    lines = _render_lines(
        *view_module.render_status_summary_lines(
            prerendered_blocks={
                change.commit_id: (
                    f"{change.subject} [{change.change_id[:8]}]",
                    f"body for {change.subject}",
                )
                for change in changes
            },
            leading_separator=False,
            result=_status_result(changes=changes),
            verbose=False,
        )
    )

    assert lines == (
        "Unsubmitted stack:",
        "feature 8 [88888888]",
        "body for feature 8",
        "feature 7 [77777777]",
        "body for feature 7",
        "feature 6 [66666666]",
        "body for feature 6",
        "   ... 2 changes omitted ...",
        "feature 3 [33333333]",
        "body for feature 3",
        "feature 2 [22222222]",
        "body for feature 2",
        "feature 1 [11111111]",
        "body for feature 1",
    )
