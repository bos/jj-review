from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import cleanup as cleanup_module
from jj_review.errors import CliError

from .entrypoint_test_helpers import patch_bootstrap


def test_cleanup_mutates_by_default_and_passes_apply_to_prepare_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
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
        config_path=None,
        current=False,
        debug=False,
        dry_run=False,
        repository=tmp_path,
        restack=False,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert apply_calls == [True]
    assert "No cleanup actions needed." in captured.out


def test_cleanup_restack_mutates_by_default_and_passes_apply_and_revset_to_prepare_restack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
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
        config_path=None,
        current=False,
        debug=False,
        dry_run=False,
        repository=tmp_path,
        restack=True,
        revset="@-",
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert prepare_calls == [(True, "@-")]
    assert "Selected revset: @-" in captured.out
    assert "No merged changes on the selected stack need restacking." in captured.out


def test_cleanup_restack_requires_explicit_revision_selection_when_mutating(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, cleanup_module, tmp_path)
    prepare_called = False

    def fake_prepare_restack(**kwargs):
        nonlocal prepare_called
        prepare_called = True
        raise AssertionError("restack apply should not prepare without a selector")

    monkeypatch.setattr(cleanup_module, "prepare_restack", fake_prepare_restack)

    with pytest.raises(CliError, match="requires an explicit revision selection"):
        cleanup_module.cleanup(
            config_path=None,
            current=False,
            debug=False,
            dry_run=False,
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
        current=False,
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
        current=False,
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


def test_cleanup_prints_remote_and_github_before_stream_completes(
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
        config_path=None,
        current=False,
        debug=False,
        dry_run=True,
        repository=tmp_path,
        restack=False,
        revset=None,
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" not in checkpoints[0]
    assert "GitHub: octo-org/stacked-review" not in checkpoints[0]
    assert checkpoints[0] == ""
    assert "Planned cleanup actions:" in checkpoints[1]
    assert "cleanup --apply" not in captured.out
