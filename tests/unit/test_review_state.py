from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import review_state as review_state_module


def test_display_change_id_truncates_to_eight_characters() -> None:
    assert review_state_module.display_change_id("abcdefghijkl") == "abcdefgh"


def test_format_pull_request_label_marks_drafts() -> None:
    assert review_state_module.format_pull_request_label(7, is_draft=True) == "draft PR #7"


def test_render_status_selection_lines_reports_selected_remote_error() -> None:
    prepared_status = SimpleNamespace(
        selected_revset="@",
        prepared=SimpleNamespace(
            remote=None,
            remote_error="no git remote configured",
        ),
    )

    assert review_state_module.render_status_selection_lines(
        prepared_status=prepared_status
    ) == (
        "Selected revset: @",
        "Selected remote: unavailable (no git remote configured)",
    )


def test_render_status_github_lines_includes_stack_header_when_revisions_exist() -> None:
    assert review_state_module.render_status_github_lines(
        github_error=None,
        github_repository="octo-org/stacked-review",
        has_revisions=True,
    ) == (
        "GitHub: octo-org/stacked-review",
        "Stack:",
    )


def test_render_trunk_status_row_prefers_unique_local_bookmark() -> None:
    prepared = SimpleNamespace(
        client=SimpleNamespace(
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
        review_state_module.render_trunk_status_row(
            prepared,
            configured_trunk_branch=None,
        )
        == "◆ base [trunkcha]: main"
    )


def test_render_status_advisory_lines_reports_cleanup_and_policy_warning() -> None:
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
