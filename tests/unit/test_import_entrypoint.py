from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import import_ as import_module

from .entrypoint_test_helpers import patch_bootstrap


def test_import_renders_up_to_date_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, import_module, tmp_path)

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
    patch_bootstrap(monkeypatch, import_module, tmp_path)

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
    patch_bootstrap(monkeypatch, import_module, tmp_path)

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
