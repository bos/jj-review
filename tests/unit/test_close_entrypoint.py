from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import close as close_module
from jj_review.errors import CliError

from .entrypoint_test_helpers import patch_bootstrap


def test_close_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, close_module, tmp_path)
    run_called = False

    def fake_prepare_close(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("close should not run without an explicit selector")

    monkeypatch.setattr(close_module, "prepare_close", fake_prepare_close)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        close_module.close(
            cleanup=False,
            config_path=None,
            current=False,
            debug=False,
            dry_run=False,
            repository=tmp_path,
            revset=None,
        )

    assert not run_called


def test_close_rejects_revset_and_current_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, close_module, tmp_path)

    with pytest.raises(CliError, match="accepts either `<revset>` or `--current`, not both"):
        close_module.close(
            cleanup=False,
            config_path=None,
            current=True,
            debug=False,
            dry_run=False,
            repository=tmp_path,
            revset="@",
        )


def test_close_renders_planned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, close_module, tmp_path)

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
        cleanup=True,
        config_path=None,
        current=False,
        debug=False,
        dry_run=True,
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
    assert "Re-run with" not in captured.out


def test_close_renders_apply_noop_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, close_module, tmp_path)
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
        cleanup=False,
        config_path=None,
        current=False,
        debug=False,
        dry_run=False,
        repository=tmp_path,
        revset="@",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No close actions were needed for the selected stack." in captured.out
    assert "No managed open pull requests" not in captured.out
