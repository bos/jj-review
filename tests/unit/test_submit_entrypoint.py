from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import submit as submit_module
from jj_review.errors import CliError

from .entrypoint_test_helpers import fake_submit_state_store, patch_bootstrap


def test_submit_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: fake_submit_state_store(tmp_path),
    )
    run_called = False

    async def fake_run_submit(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("submit should not run without an explicit selector")

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        submit_module.submit(
            config_path=None,
            current=False,
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

    assert not run_called


def test_submit_rejects_revset_and_current_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, submit_module, tmp_path)

    with pytest.raises(CliError, match="accepts either `<revset>` or `--current`, not both"):
        submit_module.submit(
            config_path=None,
            current=True,
            debug=False,
            describe_with=None,
            draft=False,
            draft_all=False,
            dry_run=False,
            publish=False,
            repository=tmp_path,
            reviewers=None,
            revset="@",
            team_reviewers=None,
        )


def test_submit_passes_dry_run_and_renders_planned_output(
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
    dry_run_calls: list[bool] = []
    selected_revsets: list[str | None] = []

    async def fake_run_submit(**kwargs):
        dry_run_calls.append(bool(kwargs["dry_run"]))
        selected_revsets.append(kwargs["revset"])
        return SimpleNamespace(
            dry_run=True,
            remote=SimpleNamespace(name="origin"),
            revisions=(
                SimpleNamespace(
                    bookmark="review/feature-abcdefgh",
                    bookmark_source="generated",
                    change_id="abcdefghijkl",
                    local_action="created",
                    pull_request_action="created",
                    pull_request_number=None,
                    pull_request_url=None,
                    remote_action="pushed",
                    subject="feature 1",
                ),
            ),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    exit_code = submit_module.submit(
        config_path=None,
        current=True,
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
    assert dry_run_calls == [True]
    assert selected_revsets == [None]
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "Planned bookmarks:" in captured.out
    assert "- feature 1 [abcdefgh]" in captured.out
    assert "  -> review/feature-abcdefgh [new PR]" in captured.out
    assert "Top of stack:" not in captured.out


@pytest.mark.parametrize(
    ("draft", "draft_all", "publish", "expected_mode"),
    [
        (True, False, False, "draft"),
        (False, True, False, "draft_all"),
    ],
)
def test_submit_passes_draft_modes_to_submit_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    draft: bool,
    draft_all: bool,
    publish: bool,
    expected_mode: str,
) -> None:
    patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: fake_submit_state_store(tmp_path),
    )
    draft_modes: list[str] = []

    async def fake_run_submit(**kwargs):
        draft_modes.append(kwargs["draft_mode"])
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    exit_code = submit_module.submit(
        config_path=None,
        current=True,
        debug=False,
        describe_with=None,
        draft=draft,
        draft_all=draft_all,
        dry_run=False,
        publish=publish,
        repository=tmp_path,
        reviewers=None,
        revset=None,
        team_reviewers=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert draft_modes == [expected_mode]
    assert "No reviewable commits" in captured.out


def test_submit_passes_reviewer_overrides_to_submit_runner(
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
    reviewer_calls: list[tuple[list[str] | None, list[str] | None]] = []

    async def fake_run_submit(**kwargs):
        reviewer_calls.append((kwargs["reviewers"], kwargs["team_reviewers"]))
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    exit_code = submit_module.submit(
        config_path=None,
        current=True,
        debug=False,
        describe_with=None,
        draft=False,
        draft_all=False,
        dry_run=False,
        publish=False,
        repository=tmp_path,
        reviewers=["alice,bob", "bob,carol"],
        revset=None,
        team_reviewers=["platform", "infra,platform"],
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert reviewer_calls == [(["alice", "bob", "carol"], ["platform", "infra"])]
    assert "No reviewable commits" in captured.out


def test_submit_passes_describe_with_to_submit_runner(
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
    describe_with_calls: list[str | None] = []

    async def fake_run_submit(**kwargs):
        describe_with_calls.append(kwargs["describe_with"])
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    exit_code = submit_module.submit(
        config_path=None,
        current=True,
        debug=False,
        describe_with="scripts/describe_with_codex.py",
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
    assert describe_with_calls == ["scripts/describe_with_codex.py"]
    assert "No reviewable commits" in captured.out


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
        kwargs["on_prepared"]("@", SimpleNamespace(name="origin"), True)
        kwargs["on_trunk_resolved"]("base", "main", True)
        return SimpleNamespace(
            dry_run=True,
            remote=SimpleNamespace(name="origin"),
            revisions=(revision,),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    exit_code = submit_module.submit(
        config_path=None,
        current=True,
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
    assert captured.out.count("Selected revset: @") == 1
    assert captured.out.count("Selected remote: origin") == 1
    assert captured.out.count("Trunk: base -> main") == 1
    assert captured.out.count("Dry run: no local, remote, or GitHub changes applied.") == 1
    assert captured.out.count("Planned bookmarks:") == 1
    assert captured.out.count("- feature 1 [abcdefgh]") == 1
    assert captured.out.count("  -> review/feature-abcdefgh [new PR]") == 1
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
        kwargs["on_prepared"]("@", SimpleNamespace(name="origin"), True)
        kwargs["on_trunk_resolved"]("base", "main", True)
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(revision,),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(submit_module, "_run_submit_async", fake_run_submit)

    exit_code = submit_module.submit(
        config_path=None,
        current=True,
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
