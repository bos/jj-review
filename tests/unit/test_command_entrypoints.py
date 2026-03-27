from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import (
    cleanup as cleanup_module,
)
from jj_review.commands import (
    close as close_module,
)
from jj_review.commands import (
    import_ as import_module,
)
from jj_review.commands import (
    land as land_module,
)
from jj_review.commands import (
    relink as relink_module,
)
from jj_review.commands import (
    review_state as review_state_module,
)
from jj_review.commands import (
    submit as submit_module,
)
from jj_review.commands import (
    unlink as unlink_module,
)
from jj_review.errors import CliError
from jj_review.jj import UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState


def _app_context(
    tmp_path: Path,
    *,
    repo_config: object | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        repo_root=tmp_path,
        config=SimpleNamespace(
            change={},
            logging=SimpleNamespace(level="WARNING"),
            repo=repo_config if repo_config is not None else SimpleNamespace(trunk_branch=None),
        ),
    )


def _patch_bootstrap(
    monkeypatch: pytest.MonkeyPatch,
    module,
    tmp_path: Path,
    *,
    repo_config: object | None = None,
) -> None:
    monkeypatch.setattr(
        module,
        "bootstrap_context",
        lambda **kwargs: _app_context(tmp_path, repo_config=repo_config),
    )


def _fake_submit_state_store(tmp_path: Path) -> SimpleNamespace:
    state_dir = tmp_path / "jj-review-state"
    return SimpleNamespace(
        state_dir=state_dir,
        require_writable=lambda: state_dir,
    )


def test_status_passes_fetch_to_prepare_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, review_state_module, tmp_path)
    prepare_calls: list[bool] = []

    def fake_prepare_status(**kwargs):
        prepare_calls.append(bool(kwargs["fetch_remote_state"]))
        return SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=None,
                remote_error=None,
                stack=SimpleNamespace(
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(),
            ),
            selected_revset="@",
            trunk_subject="base",
            outstanding_intents=(),
        )

    monkeypatch.setattr(review_state_module, "prepare_status", fake_prepare_status)
    monkeypatch.setattr(
        review_state_module,
        "stream_status",
        lambda **kwargs: SimpleNamespace(incomplete=False),
    )

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=True,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No reviewable commits" in captured.out
    assert prepare_calls == [True]


def test_status_reports_targeted_divergent_stack_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bootstrap(monkeypatch, review_state_module, tmp_path)

    def raise_unsupported_stack(**kwargs):
        raise UnsupportedStackError(
            "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq: "
            "divergent changes are not supported.",
            change_id="nznokxmvrnysowwwkktpmroswxqsozqq",
            reason="divergent_change",
        )

    monkeypatch.setattr(review_state_module, "prepare_status", raise_unsupported_stack)

    with pytest.raises(CliError, match="Could not inspect review status"):
        review_state_module.status(
            config_path=None,
            debug=False,
            fetch=False,
            repository=tmp_path,
            revset=None,
        )


def test_describe_status_preparation_error_falls_back_without_structured_context() -> None:
    error = UnsupportedStackError(
        "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq: "
        "divergent changes are not supported."
    )

    assert "jj log -r" not in review_state_module.describe_status_preparation_error(error)


def test_status_reports_uninspected_github_target_for_empty_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, review_state_module, tmp_path)
    monkeypatch.setattr(
        review_state_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                stack=SimpleNamespace(
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(),
            ),
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            github_repository_error=None,
            selected_revset="main",
            trunk_subject="base",
            outstanding_intents=(),
        ),
    )

    def fake_stream_status(**kwargs):
        kwargs["on_github_status"](
            "octo-org/stacked-review",
            "not inspected; no reviewable commits",
        )
        return SimpleNamespace(incomplete=False)

    monkeypatch.setattr(review_state_module, "stream_status", fake_stream_status)

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=False,
        repository=tmp_path,
        revset="main",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (
        "GitHub target: octo-org/stacked-review "
        "(not inspected; no reviewable commits)"
    ) in captured.out
    assert "No reviewable commits" in captured.out


def test_status_prints_local_header_before_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(
        monkeypatch,
        review_state_module,
        tmp_path,
        repo_config=SimpleNamespace(trunk_branch=None),
    )
    monkeypatch.setattr(
        review_state_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(
                    list_bookmark_states=lambda: {
                        "main": BookmarkState(
                            name="main",
                            local_targets=("trunk-commit",),
                        )
                    }
                ),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                stack=SimpleNamespace(
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(object(),),
            ),
            outstanding_intents=(),
            selected_revset="@",
            stale_intents=(),
            trunk_subject="base",
        ),
    )

    def fake_stream_status(**kwargs):
        streamed = capsys.readouterr()
        assert "Selected revset: @" in streamed.out
        assert "Selected remote: origin" in streamed.out
        assert "◆ base [trunkcha" not in streamed.out
        kwargs["on_github_status"]("octo-org/stacked-review", None)
        kwargs["on_revision"](
            SimpleNamespace(
                cached_change=None,
                change_id="abcdefghijkl",
                pull_request_lookup=SimpleNamespace(
                    pull_request=SimpleNamespace(number=1),
                    state="open",
                ),
                stack_comment_lookup=None,
                subject="feature 1",
            ),
            True,
        )
        return SimpleNamespace(incomplete=False)

    monkeypatch.setattr(review_state_module, "stream_status", fake_stream_status)

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=False,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Stack:" in captured.out
    assert "- feature 1 [abcdefgh]: PR #1" in captured.out
    assert "◆ base [trunkcha]: main" in captured.out
    assert captured.out.index("- feature 1 [abcdefgh]: PR #1") < captured.out.index(
        "◆ base [trunkcha]: main"
    )


def test_status_prints_cleanup_advisories_for_merged_review_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, review_state_module, tmp_path)
    monkeypatch.setattr(
        review_state_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                stack=SimpleNamespace(
                    trunk=SimpleNamespace(
                        change_id="trunkchangeid",
                        commit_id="trunk-commit",
                        subject="base",
                    )
                ),
                status_revisions=(object(),),
            ),
            outstanding_intents=(),
            selected_revset="@",
            stale_intents=(),
            trunk_subject="base",
        ),
    )

    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="abcdefghijkl",
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
        subject="feature 1",
    )

    def fake_stream_status(**kwargs):
        kwargs["on_github_status"]("octo-org/stacked-review", None)
        kwargs["on_revision"](merged_revision, True)
        return SimpleNamespace(
            incomplete=False,
            revisions=(merged_revision,),
            selected_revset="@",
        )

    monkeypatch.setattr(review_state_module, "stream_status", fake_stream_status)

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=False,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "- feature 1 [abcdefgh]: PR #5 merged, cleanup needed" in captured.out
    assert "◆ base [trunkcha]: trunk()\n\nAdvisories:" in captured.out
    assert "jj-review cleanup --restack @" in captured.out


def test_submit_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: _fake_submit_state_store(tmp_path),
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
    _patch_bootstrap(monkeypatch, submit_module, tmp_path)

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
    _patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: _fake_submit_state_store(tmp_path),
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
    _patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: _fake_submit_state_store(tmp_path),
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
    _patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: _fake_submit_state_store(tmp_path),
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
    _patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: _fake_submit_state_store(tmp_path),
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
    _patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: _fake_submit_state_store(tmp_path),
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
    _patch_bootstrap(monkeypatch, submit_module, tmp_path)
    monkeypatch.setattr(
        submit_module.ReviewStateStore,
        "for_repo",
        lambda _: _fake_submit_state_store(tmp_path),
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


def test_land_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bootstrap(monkeypatch, land_module, tmp_path)
    run_called = False

    def fake_prepare_land(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("land should not run without an explicit selector")

    monkeypatch.setattr(land_module, "prepare_land", fake_prepare_land)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        land_module.land(
            apply=False,
            bypass_readiness=False,
            config_path=None,
            current=False,
            debug=False,
            expect_pr=None,
            repository=tmp_path,
            revset=None,
        )

    assert not run_called


def test_land_renders_planned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, land_module, tmp_path)

    def fake_prepare_land(**kwargs):
        assert kwargs["bypass_readiness"] is False
        assert kwargs["expect_pr_reference"] == "7"
        assert kwargs["revset"] == "@-"
        return SimpleNamespace()

    def fake_stream_land(**kwargs):
        return SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="trunk",
                    message="push main to feature 1 [aaaaaaaa]",
                    status="planned",
                ),
                SimpleNamespace(
                    kind="pull request",
                    message="finalize PR #7 for feature 1 [aaaaaaaa]",
                    status="planned",
                ),
            ),
            applied=False,
            bypass_readiness=False,
            blocked=False,
            expect_pr_number=7,
            follow_up=None,
            github_repository="octo-org/stacked-review",
            remote_name="origin",
            selected_revset="@-",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr(land_module, "prepare_land", fake_prepare_land)
    monkeypatch.setattr(land_module, "stream_land", fake_stream_land)

    exit_code = land_module.land(
        apply=False,
        bypass_readiness=False,
        config_path=None,
        current=False,
        debug=False,
        expect_pr="7",
        repository=tmp_path,
        revset="@-",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected revset: @-" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Planned land actions:" in captured.out
    assert "- [planned] trunk: push main to feature 1 [aaaaaaaa]" in captured.out
    assert "Re-run with `land --apply --expect-pr 7 @-`" in captured.out


def test_land_renders_blocked_output_without_apply_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, land_module, tmp_path)
    monkeypatch.setattr(land_module, "prepare_land", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        land_module,
        "stream_land",
        lambda **kwargs: SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="guardrail",
                    message="`--expect-pr 7` did not match the changes that can be landed now.",
                    status="blocked",
                ),
            ),
            applied=False,
            bypass_readiness=False,
            blocked=True,
            expect_pr_number=7,
            follow_up=None,
            github_repository="octo-org/stacked-review",
            remote_name="origin",
            selected_revset="@-",
            trunk_branch="main",
            trunk_subject="base",
        ),
    )

    exit_code = land_module.land(
        apply=False,
        bypass_readiness=False,
        config_path=None,
        current=False,
        debug=False,
        expect_pr="7",
        repository=tmp_path,
        revset="@-",
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "- [blocked] guardrail:" in captured.out
    assert "Re-run with `land --apply" not in captured.out


def test_land_passes_bypass_readiness_and_renders_apply_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, land_module, tmp_path)

    def fake_prepare_land(**kwargs):
        assert kwargs["bypass_readiness"] is True
        return SimpleNamespace()

    monkeypatch.setattr(land_module, "prepare_land", fake_prepare_land)
    monkeypatch.setattr(
        land_module,
        "stream_land",
        lambda **kwargs: SimpleNamespace(
            actions=(),
            applied=False,
            bypass_readiness=True,
            blocked=False,
            expect_pr_number=7,
            follow_up=None,
            github_repository="octo-org/stacked-review",
            remote_name="origin",
            selected_revset="@-",
            trunk_branch="main",
            trunk_subject="base",
        ),
    )

    exit_code = land_module.land(
        apply=False,
        bypass_readiness=True,
        config_path=None,
        current=False,
        debug=False,
        expect_pr="7",
        repository=tmp_path,
        revset="@-",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Re-run with `land --apply --bypass-readiness --expect-pr 7 @-`" in captured.out


def test_close_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bootstrap(monkeypatch, close_module, tmp_path)
    run_called = False

    def fake_prepare_close(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("close should not run without an explicit selector")

    monkeypatch.setattr(close_module, "prepare_close", fake_prepare_close)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        close_module.close(
            apply=False,
            cleanup=False,
            config_path=None,
            current=False,
            debug=False,
            repository=tmp_path,
            revset=None,
        )

    assert not run_called


def test_close_rejects_revset_and_current_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bootstrap(monkeypatch, close_module, tmp_path)

    with pytest.raises(CliError, match="accepts either `<revset>` or `--current`, not both"):
        close_module.close(
            apply=False,
            cleanup=False,
            config_path=None,
            current=True,
            debug=False,
            repository=tmp_path,
            revset="@",
        )


def test_close_renders_planned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, close_module, tmp_path)

    def fake_prepare_close(**kwargs):
        assert kwargs["apply"] is False
        assert kwargs["cleanup"] is True
        assert kwargs["revset"] == "@"
        return SimpleNamespace()

    monkeypatch.setattr(close_module, "prepare_close", fake_prepare_close)
    monkeypatch.setattr(
        close_module,
        "stream_close",
        lambda **kwargs: SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="pull request",
                    message="close PR #7 for feature 1 [aaaaaaaa]",
                    status="planned",
                ),
                SimpleNamespace(
                    kind="tracking",
                    message="stop saved jj-review tracking for feature 1 [aaaaaaaa]",
                    status="planned",
                ),
            ),
            applied=False,
            blocked=False,
            cleanup=True,
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
            selected_revset="@",
        ),
    )

    exit_code = close_module.close(
        apply=False,
        cleanup=True,
        config_path=None,
        current=False,
        debug=False,
        repository=tmp_path,
        revset="@",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected revset: @" in captured.out
    assert "Selected remote: origin" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Planned close actions:" in captured.out
    assert "- [planned] pull request: close PR #7 for feature 1 [aaaaaaaa]" in captured.out
    assert "Re-run with `close --apply --cleanup @`" in captured.out


def test_close_renders_apply_noop_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, close_module, tmp_path)
    monkeypatch.setattr(close_module, "prepare_close", lambda **kwargs: SimpleNamespace())
    monkeypatch.setattr(
        close_module,
        "stream_close",
        lambda **kwargs: SimpleNamespace(
            actions=(),
            applied=True,
            blocked=False,
            cleanup=False,
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
            selected_revset="@",
        ),
    )

    exit_code = close_module.close(
        apply=True,
        cleanup=False,
        config_path=None,
        current=False,
        debug=False,
        repository=tmp_path,
        revset="@",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No close actions were needed for the selected stack." in captured.out
    assert "No managed open pull requests" not in captured.out


def test_import_renders_up_to_date_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, import_module, tmp_path)

    async def fake_run_import_async(**kwargs):
        assert kwargs["current"] is False
        assert kwargs["fetch"] is False
        return SimpleNamespace(
            actions=(),
            fetched_tip_commit=None,
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
            reviewable_revision_count=2,
            selected_revset="commit-2",
            selector="--head review/feature-aaaaaaaa",
        )

    monkeypatch.setattr(import_module, "_run_import_async", fake_run_import_async)

    exit_code = import_module.import_(
        config_path=None,
        current=False,
        debug=False,
        fetch=False,
        head="review/feature-aaaaaaaa",
        pull_request=None,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected selector: --head review/feature-aaaaaaaa" in captured.out
    assert "Local jj-review tracking is already up to date for the selected stack." in (
        captured.out
    )


def test_import_renders_unavailable_github_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, import_module, tmp_path)
    
    async def fake_run_import_async(**kwargs):
        return SimpleNamespace(
            actions=(),
            fetched_tip_commit=None,
            github_error=None,
            github_repository=None,
            remote=None,
            remote_error=None,
            reviewable_revision_count=0,
            selected_revset="@",
            selector="--current",
        )

    monkeypatch.setattr(import_module, "_run_import_async", fake_run_import_async)

    exit_code = import_module.import_(
        config_path=None,
        current=True,
        debug=False,
        fetch=False,
        head=None,
        pull_request=None,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: unavailable" in captured.out
    assert "GitHub: unavailable" in captured.out
    assert "GitHub target:" not in captured.out


def test_import_fetch_renders_fetched_tip_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, import_module, tmp_path)

    async def fake_run_import_async(**kwargs):
        return SimpleNamespace(
            actions=(),
            fetched_tip_commit="commit-2",
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
            reviewable_revision_count=2,
            selected_revset="commit-2",
            selector="--pull-request 2",
        )

    monkeypatch.setattr(import_module, "_run_import_async", fake_run_import_async)

    exit_code = import_module.import_(
        config_path=None,
        current=False,
        debug=False,
        fetch=True,
        head=None,
        pull_request="2",
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Fetched tip commit: commit-2" in captured.out


def test_cleanup_passes_apply_to_prepare_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
    apply_calls: list[bool] = []

    def fake_prepare_cleanup(**kwargs):
        apply_calls.append(bool(kwargs["apply"]))
        return SimpleNamespace(
            apply=True,
            github_repository=None,
            github_repository_error=None,
            remote=None,
            remote_error=None,
        )

    monkeypatch.setattr(cleanup_module, "prepare_cleanup", fake_prepare_cleanup)
    monkeypatch.setattr(
        cleanup_module,
        "stream_cleanup",
        lambda **kwargs: SimpleNamespace(
            actions=(),
            applied=True,
            github_error=None,
            github_repository=None,
            remote=None,
            remote_error=None,
        ),
    )

    exit_code = cleanup_module.cleanup(
        apply=True,
        config_path=None,
        current=False,
        debug=False,
        repository=tmp_path,
        restack=False,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert apply_calls == [True]
    assert "No cleanup actions planned." in captured.out


def test_cleanup_restack_passes_apply_and_revset_to_prepare_restack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
    prepare_calls: list[tuple[bool, str | None]] = []

    def fake_prepare_restack(**kwargs):
        prepare_calls.append((bool(kwargs["apply"]), kwargs["revset"]))
        return SimpleNamespace(
            apply=True,
            prepared_status=SimpleNamespace(
                github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
                github_repository_error=None,
                prepared=SimpleNamespace(
                    remote=SimpleNamespace(name="origin"),
                    remote_error=None,
                ),
                selected_revset="@-",
            ),
        )

    monkeypatch.setattr(cleanup_module, "prepare_restack", fake_prepare_restack)
    monkeypatch.setattr(
        cleanup_module,
        "stream_restack",
        lambda **kwargs: SimpleNamespace(
            actions=(),
            applied=True,
            blocked=False,
            selected_revset="@-",
        ),
    )

    exit_code = cleanup_module.cleanup(
        apply=True,
        config_path=None,
        current=False,
        debug=False,
        repository=tmp_path,
        restack=True,
        revset="@-",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert prepare_calls == [(True, "@-")]
    assert "Selected revset: @-" in captured.out
    assert "No merged changes on the selected stack need restacking." in captured.out


def test_cleanup_restack_apply_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
    prepare_called = False

    def fake_prepare_restack(**kwargs):
        nonlocal prepare_called
        prepare_called = True
        raise AssertionError("restack apply should not prepare without a selector")

    monkeypatch.setattr(cleanup_module, "prepare_restack", fake_prepare_restack)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        cleanup_module.cleanup(
            apply=True,
            config_path=None,
            current=False,
            debug=False,
            repository=tmp_path,
            restack=True,
            revset=None,
        )

    assert not prepare_called


def test_cleanup_renders_planned_and_blocked_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
    monkeypatch.setattr(
        cleanup_module,
        "prepare_cleanup",
        lambda **kwargs: SimpleNamespace(
            apply=False,
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            github_repository_error=None,
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
        ),
    )

    def fake_stream_cleanup(**kwargs):
        kwargs["on_action"](
            SimpleNamespace(
                kind="tracking",
                message="remove saved jj-review data for abcdef12",
                status="planned",
            )
        )
        kwargs["on_action"](
            SimpleNamespace(
                kind="remote branch",
                message="cannot delete review/feature-abcdef12@origin",
                status="blocked",
            )
        )
        return SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="tracking",
                    message="remove saved jj-review data for abcdef12",
                    status="planned",
                ),
                SimpleNamespace(
                    kind="remote branch",
                    message="cannot delete review/feature-abcdef12@origin",
                    status="blocked",
                ),
            ),
            applied=False,
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
        )

    monkeypatch.setattr(cleanup_module, "stream_cleanup", fake_stream_cleanup)

    exit_code = cleanup_module.cleanup(
        apply=False,
        config_path=None,
        current=False,
        debug=False,
        repository=tmp_path,
        restack=False,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Planned cleanup actions:" in captured.out
    assert "[planned] tracking: remove saved jj-review data for abcdef12" in captured.out
    assert "cleanup --apply" in captured.out


def test_cleanup_restack_renders_next_step_and_policy_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
    monkeypatch.setattr(
        cleanup_module,
        "prepare_restack",
        lambda **kwargs: SimpleNamespace(
            apply=False,
            prepared_status=SimpleNamespace(
                github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
                github_repository_error=None,
                prepared=SimpleNamespace(
                    remote=SimpleNamespace(name="origin"),
                    remote_error=None,
                ),
                selected_revset="@",
            ),
        ),
    )

    def fake_stream_restack(**kwargs):
        kwargs["on_action"](
            SimpleNamespace(
                kind="restack",
                message="rebase abcdef12 onto trunk()",
                status="planned",
            )
        )
        kwargs["on_action"](
            SimpleNamespace(
                kind="policy",
                message="PR #5 merged into review/base-branch",
                status="planned",
            )
        )
        return SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="restack",
                    message="rebase abcdef12 onto trunk()",
                    status="planned",
                ),
                SimpleNamespace(
                    kind="policy",
                    message="PR #5 merged into review/base-branch",
                    status="planned",
                ),
            ),
            applied=False,
            blocked=False,
            selected_revset="@",
        )

    monkeypatch.setattr(cleanup_module, "stream_restack", fake_stream_restack)

    exit_code = cleanup_module.cleanup(
        apply=False,
        config_path=None,
        current=False,
        debug=False,
        repository=tmp_path,
        restack=True,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected revset: @" in captured.out
    assert "Planned restack actions:" in captured.out
    assert "cleanup --restack --apply @" in captured.out


def test_cleanup_prints_remote_and_github_before_stream_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
    monkeypatch.setattr(
        cleanup_module,
        "prepare_cleanup",
        lambda **kwargs: SimpleNamespace(
            apply=False,
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            github_repository_error=None,
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
        ),
    )
    checkpoints: list[str] = []

    def fake_stream_cleanup(**kwargs):
        checkpoints.append(capsys.readouterr().out)
        kwargs["on_action"](
            SimpleNamespace(
                kind="tracking",
                message="remove saved jj-review data for abcdef12",
                status="planned",
            )
        )
        checkpoints.append(capsys.readouterr().out)
        return SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="tracking",
                    message="remove saved jj-review data for abcdef12",
                    status="planned",
                ),
            ),
            applied=False,
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
        )

    monkeypatch.setattr(cleanup_module, "stream_cleanup", fake_stream_cleanup)

    exit_code = cleanup_module.cleanup(
        apply=False,
        config_path=None,
        current=False,
        debug=False,
        repository=tmp_path,
        restack=False,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" in checkpoints[0]
    assert "GitHub: octo-org/stacked-review" in checkpoints[0]
    assert "Planned cleanup actions:" in checkpoints[1]
    assert "cleanup --apply" in captured.out


def test_relink_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bootstrap(monkeypatch, relink_module, tmp_path)
    run_called = False

    async def fake_run_relink_async(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("relink should not run without an explicit selector")

    monkeypatch.setattr(relink_module, "_run_relink_async", fake_run_relink_async)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        relink_module.relink(
            config_path=None,
            current=False,
            debug=False,
            pull_request="123",
            repository=tmp_path,
            revset=None,
        )

    assert not run_called


def test_relink_current_passes_current_path_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, relink_module, tmp_path)
    relink_calls: list[str | None] = []

    async def fake_run_relink_async(**kwargs):
        relink_calls.append(kwargs["revset"])
        return SimpleNamespace(
            bookmark="review/feature-abcdefgh",
            change_id="abcdefghijkl",
            github_repository="octo-org/stacked-review",
            pull_request_number=7,
            remote_name="origin",
            selected_revset="@",
            subject="feature 1",
        )

    monkeypatch.setattr(relink_module, "_run_relink_async", fake_run_relink_async)

    exit_code = relink_module.relink(
        config_path=None,
        current=True,
        debug=False,
        pull_request="7",
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert relink_calls == [None]
    assert "Relinked PR #7 for feature 1 [abcdefgh] -> review/feature-abcdefgh" in (
        captured.out
    )


def test_unlink_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_bootstrap(monkeypatch, unlink_module, tmp_path)
    run_called = False

    async def fake_run_unlink_async(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("unlink should not run without an explicit selector")

    monkeypatch.setattr(unlink_module, "_run_unlink_async", fake_run_unlink_async)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        unlink_module.unlink(
            config_path=None,
            current=False,
            debug=False,
            repository=tmp_path,
            revset=None,
        )

    assert not run_called


def test_unlink_current_passes_current_path_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_bootstrap(monkeypatch, unlink_module, tmp_path)
    calls: list[str | None] = []

    async def fake_run_unlink_async(**kwargs):
        calls.append(kwargs["revset"])
        return SimpleNamespace(
            already_unlinked=False,
            bookmark="review/feature-abcdefgh",
            change_id="abcdefghijkl",
            selected_revset="@",
            subject="feature 1",
        )

    monkeypatch.setattr(unlink_module, "_run_unlink_async", fake_run_unlink_async)

    exit_code = unlink_module.unlink(
        config_path=None,
        current=True,
        debug=False,
        repository=tmp_path,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == [None]
    assert "Stopped review tracking for feature 1 [abcdefgh]" in captured.out
