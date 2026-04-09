from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import unlink as unlink_module
from jj_review.errors import CliError

from .entrypoint_test_helpers import patch_bootstrap


def test_unlink_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, unlink_module, tmp_path)
    run_called = False

    async def fake_run_unlink_async(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("unlink should not run without an explicit selector")

    monkeypatch.setattr(unlink_module, "_run_unlink_async", fake_run_unlink_async)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        unlink_module.unlink(
            config_path=None,
            debug=False,
            repository=tmp_path,
            revset=None,
        )

    assert not run_called


def test_unlink_passes_explicit_revset_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, unlink_module, tmp_path)
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
        debug=False,
        repository=tmp_path,
        revset="@-",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == ["@-"]
    assert "Stopped review tracking for feature 1 [abcdefgh]" in captured.out
