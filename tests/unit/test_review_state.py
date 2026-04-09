from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import review_state as review_state_module


def test_render_status_selection_lines_reports_selected_remote_error() -> None:
    prepared_status = SimpleNamespace(
        selected_revset="@",
        prepared=SimpleNamespace(
            remote=None,
            remote_error="no git remote configured",
        ),
    )

    assert review_state_module.render_status_selection_lines(
        prepared_status=prepared_status,
    ) == ("Selected remote: unavailable (no git remote configured)",)


def test_status_reports_github_target_when_empty_stack_was_not_inspected() -> None:
    assert review_state_module.render_status_github_lines(
        github_error=None,
        github_repository="octo-org/stacked-review",
        has_revisions=False,
    ) == ("GitHub target: octo-org/stacked-review (not inspected; no reviewable commits)",)


def test_render_trunk_status_lines_prefers_unique_local_bookmark() -> None:
    prepared = SimpleNamespace(
        client=SimpleNamespace(
            render_revision_log_lines=lambda revision, *, color_when: (
                f"◆ {revision.subject} [{revision.change_id[:8]}]",
            ),
            list_bookmark_states=lambda: {
                "main": SimpleNamespace(local_target="trunk-commit"),
            }
        ),
        remote=SimpleNamespace(name="origin"),
        stack=SimpleNamespace(
            trunk=SimpleNamespace(
                change_id="trunkchangeid",
                commit_id="trunk-commit",
                subject="base",
            )
        ),
    )

    assert (
        review_state_module.render_trunk_status_lines(
            color_when="never",
            prepared=prepared,
            configured_trunk_branch=None,
        )
        == ("◆ base [trunkcha]",)
    )


def test_status_advises_cleanup_and_restack_when_merged_pr_remains_in_stack() -> None:
    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="abcdefghijkl",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="review/feature-base"),
                number=5,
                state="merged",
            ),
            state="closed",
        ),
        stack_comment_lookup=None,
    )

    lines = review_state_module.render_status_advisory_lines(
        result=SimpleNamespace(
            revisions=(merged_revision,),
            selected_revset="@",
        )
    )

    assert "Advisories:" in lines
    assert any("jj-review cleanup --restack @" in line for line in lines)
    assert any("PR #5 is merged" in line for line in lines)
    assert any("merged into review/feature-base" in line for line in lines)


def test_render_status_intent_lines_reports_stale_and_interrupted_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_pid_is_alive(pid: int) -> bool:
        return pid == 101

    monkeypatch.setattr(review_state_module, "pid_is_alive", fake_pid_is_alive)
    prepared_status = SimpleNamespace(
        stale_intents=(
            SimpleNamespace(
                intent=SimpleNamespace(label="submit on @", pid=101),
                path=Path("/tmp/stale-submit.json"),
            ),
        ),
        outstanding_intents=(
            SimpleNamespace(
                intent=SimpleNamespace(label="land on @", pid=202),
                path=Path("/tmp/outstanding-land.json"),
            ),
        ),
    )

    lines = review_state_module.render_status_intent_lines(prepared_status=prepared_status)

    assert lines == (
        "",
        "Stale incomplete operations (change IDs no longer in repo):",
        "  submit on @  [process alive, stale-submit.json]",
        "",
        "Incomplete operations detected:",
        "  land on @  [interrupted, re-run to complete]",
    )


def test_status_summary_truncates_middle_of_long_unsubmitted_sections() -> None:
    revisions = tuple(
        SimpleNamespace(
            bookmark=f"review/feature-{index}",
            cached_change=None,
            change_id=f"{index}" * 12,
            link_state="active",
            local_divergent=False,
            pull_request_lookup=None,
            stack_comment_lookup=None,
            subject=f"feature {index}",
        )
        for index in range(8, 0, -1)
    )

    lines = review_state_module.render_status_summary_lines(
        client=SimpleNamespace(
            render_revision_log_lines=lambda revision, *, color_when: (
                f"{revision.subject} [{revision.change_id[:8]}]",
                f"body for {revision.subject}",
            )
        ),
        color_when="never",
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(revisions=revisions),
        verbose=False,
    )

    assert lines == (
        "Unsubmitted stack:",
        "feature 8 [88888888]",
        "body for feature 8",
        "feature 7 [77777777]",
        "body for feature 7",
        "feature 6 [66666666]",
        "body for feature 6",
        "  [...2 changes omitted...]",
        "feature 3 [33333333]",
        "body for feature 3",
        "feature 2 [22222222]",
        "body for feature 2",
        "feature 1 [11111111]",
        "body for feature 1",
        "",
    )


def test_render_status_summary_lines_show_empty_sections_in_verbose_mode() -> None:
    lines = review_state_module.render_status_summary_lines(
        client=SimpleNamespace(render_revision_log_lines=lambda revision, *, color_when: ()),
        color_when="never",
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(revisions=()),
        verbose=True,
    )

    assert lines == (
        "Unsubmitted stack:",
        "  (none)",
        "",
        "Submitted stack:",
        "  (none)",
        "",
    )


def test_render_status_summary_lines_links_submitted_header_to_top_pr() -> None:
    lines = review_state_module.render_status_summary_lines(
        client=SimpleNamespace(
            render_revision_log_lines=lambda revision, *, color_when: (revision.subject,)
        ),
        color_when="never",
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(
            revisions=(
                SimpleNamespace(
                    bookmark="review/feature-8",
                    cached_change=None,
                    change_id="abcdefgh1234",
                    link_state="active",
                    local_divergent=False,
                    pull_request_lookup=SimpleNamespace(
                        pull_request=SimpleNamespace(
                            html_url="https://github.com/bos/jj-review/pull/8",
                            is_draft=False,
                            number=8,
                        ),
                        review_decision=None,
                        review_decision_error=None,
                        state="open",
                    ),
                    stack_comment_lookup=None,
                    subject="feature 8",
                ),
                SimpleNamespace(
                    bookmark="review/feature-7",
                    cached_change=None,
                    change_id="bcdefghi1234",
                    link_state="active",
                    local_divergent=False,
                    pull_request_lookup=SimpleNamespace(
                        pull_request=SimpleNamespace(
                            html_url="https://github.com/bos/jj-review/pull/7",
                            is_draft=False,
                            number=7,
                        ),
                        review_decision=None,
                        review_decision_error=None,
                        state="open",
                    ),
                    stack_comment_lookup=None,
                    subject="feature 7",
                ),
            ),
        ),
        verbose=False,
    )

    assert lines == (
        "Submitted stack (https://github.com/bos/jj-review/pull/8):",
        "feature 8: PR #8",
        "feature 7: PR #7",
        "",
    )


def test_status_summary_hides_managed_review_bookmark_but_keeps_other_bookmarks() -> None:
    revision = SimpleNamespace(
        bookmark="review/feature-8-abcdefgh",
        cached_change=None,
        change_id="abcdefgh1234",
        link_state="active",
        local_divergent=False,
        pull_request_lookup=None,
        stack_comment_lookup=None,
        subject="feature 8",
    )

    lines = review_state_module.render_status_summary_lines(
        client=SimpleNamespace(
            render_revision_log_lines=lambda current_revision, *, color_when: (
                (
                    "○  abcdefgh bos 2026-01-01 keep/one "
                    f"{current_revision.bookmark} keep/two 12345678"
                ),
                f"│  {current_revision.subject}",
            )
        ),
        color_when="never",
        github_available=True,
        leading_separator=False,
        result=SimpleNamespace(revisions=(revision,)),
        verbose=False,
    )

    assert lines == (
        "Unsubmitted stack:",
        "○  abcdefgh bos 2026-01-01 keep/one keep/two 12345678",
        "│  feature 8",
        "",
    )
