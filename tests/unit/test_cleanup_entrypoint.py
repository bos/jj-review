from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import cleanup as cleanup_module

from .entrypoint_test_helpers import patch_bootstrap


def test_cleanup_renders_planned_and_blocked_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
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
        config_path=None,
        debug=False,
        dry_run=True,
        repository=tmp_path,
        restack=False,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" not in captured.out
    assert "GitHub: octo-org/stacked-review" not in captured.out
    assert "Planned cleanup actions:" in captured.out
    assert "[planned] tracking: remove saved jj-review data for abcdef12" in captured.out
    assert "cleanup --apply" not in captured.out


def test_cleanup_restack_renders_next_step_and_policy_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
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
        config_path=None,
        debug=False,
        dry_run=True,
        repository=tmp_path,
        restack=True,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected revset: @" in captured.out
    assert "Planned restack actions:" in captured.out
    assert "cleanup --restack --apply @" not in captured.out
