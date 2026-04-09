from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import submit as submit_module

from .entrypoint_test_helpers import fake_submit_state_store, patch_bootstrap


def test_submit_prints_final_output_without_duplicate_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: fake_submit_state_store(tmp_path),
    )
    revision = SimpleNamespace(
        bookmark="review/feature-abcdefgh",
        bookmark_source="generated",
        change_id="abcdefghijkl",
        local_action="created",
        pull_request_action="created",
        pull_request_number=None,
        pull_request_url=None,
        remote_action="pushed",
        subject="feature 1",
    )

    async def fake_run_submit(**kwargs):
        kwargs["on_prepared"]("@", "abcdefghijkl", "feature 1", True)
        kwargs["on_trunk_resolved"]("base", "basebasebase", "main", True)
        return SimpleNamespace(
            dry_run=True,
            remote=SimpleNamespace(name="origin"),
            revisions=(revision,),
            selected_change_id="abcdefghijkl",
            selected_revset="@",
            selected_subject="feature 1",
            trunk_change_id="basebasebase",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    exit_code = submit_module.submit(
        config_path=None,
        debug=False,
        describe_with=None,
        draft=False,
        draft_all=False,
        dry_run=True,
        publish=False,
        repository=tmp_path,
        reviewers=None,
        revset=None,
        team_reviewers=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.count("Selected: feature 1 [abcdefgh]") == 1
    assert "Selected remote:" not in captured.out
    assert captured.out.count("Trunk: base [basebase] -> main") == 1
    assert "\n\nTrunk: base [basebase] -> main" not in captured.out
    assert captured.out.count("Dry run: no local, remote, or GitHub changes applied.") == 1
    assert captured.out.count("Planned changes:") == 1
    assert captured.out.count("- feature 1 [abcdefgh]: new PR") == 1
    assert "Top of stack:" not in captured.out


def test_submit_prints_top_pull_request_url_at_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: fake_submit_state_store(tmp_path),
    )
    revision = SimpleNamespace(
        bookmark="review/feature-abcdefgh",
        bookmark_source="generated",
        change_id="abcdefghijkl",
        local_action="created",
        pull_request_action="created",
        pull_request_number=7,
        pull_request_url="https://github.test/example/repo/pull/7",
        remote_action="pushed",
        subject="feature 1",
    )

    async def fake_run_submit(**kwargs):
        kwargs["on_prepared"]("@", "abcdefghijkl", "feature 1", True)
        kwargs["on_trunk_resolved"]("base", "basebasebase", "main", True)
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(revision,),
            selected_change_id="abcdefghijkl",
            selected_revset="@",
            selected_subject="feature 1",
            trunk_change_id="basebasebase",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    exit_code = submit_module.submit(
        config_path=None,
        debug=False,
        describe_with=None,
        draft=False,
        draft_all=False,
        dry_run=False,
        publish=False,
        repository=tmp_path,
        reviewers=None,
        revset=None,
        team_reviewers=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.rstrip().endswith(
        "Top of stack: https://github.test/example/repo/pull/7"
    )
