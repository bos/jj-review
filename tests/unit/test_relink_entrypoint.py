from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import relink as relink_module

from .entrypoint_test_helpers import patch_bootstrap


def test_relink_passes_explicit_revset_selection(
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
        debug=False,
        pull_request="7",
        repository=tmp_path,
        revset="@-",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert relink_calls == ["@-"]
    assert "Relinked PR #7 for feature 1 [abcdefgh] -> review/feature-abcdefgh" in (
        captured.out
    )
