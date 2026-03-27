from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import land as land_module
from jj_review.errors import CliError

from .entrypoint_test_helpers import patch_bootstrap


def test_land_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, land_module, tmp_path)
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
    patch_bootstrap(monkeypatch, land_module, tmp_path)

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
    patch_bootstrap(monkeypatch, land_module, tmp_path)
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
    patch_bootstrap(monkeypatch, land_module, tmp_path)

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
    assert "Re-run with `land --apply --bypass-readiness --expect-pr 7 @-`" in (
        captured.out
    )
