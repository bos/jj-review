from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import close as close_module

from .entrypoint_test_helpers import patch_bootstrap


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
        debug=False,
        dry_run=False,
        repository=tmp_path,
        revset="@",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No close actions were needed for the selected stack." in captured.out
    assert "No managed open pull requests" not in captured.out
