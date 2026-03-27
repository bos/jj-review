from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import relink as relink_module
from jj_review.errors import CliError

from .entrypoint_test_helpers import patch_bootstrap


def test_relink_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, relink_module, tmp_path)
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
    patch_bootstrap(monkeypatch, relink_module, tmp_path)
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
