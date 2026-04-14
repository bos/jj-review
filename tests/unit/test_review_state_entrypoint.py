from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.commands import review_state as review_state_module
from jj_review.errors import CliError
from jj_review.jj import UnsupportedStackError

from .entrypoint_test_helpers import patch_bootstrap


def test_status_reports_targeted_divergent_stack_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, review_state_module, tmp_path)

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
            verbose=False,
        )


def test_describe_status_preparation_error_falls_back_without_structured_context() -> None:
    error = UnsupportedStackError(
        "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq: "
        "divergent changes are not supported."
    )

    assert "jj log -r" not in review_state_module.describe_status_preparation_error(error)


def test_status_updates_tty_progress_bar_while_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, review_state_module, tmp_path)
    progress_updates: list[int] = []
    progress_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        review_state_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(
                    list_bookmark_states=lambda: {},
                    render_revision_log_lines=lambda revision, *, color_when: (
                        f"{revision.subject} [{revision.change_id[:8]}]",
                    ),
                    resolve_color_when=lambda *, cli_color, stdout_is_tty: "never",
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
                status_revisions=(object(), object()),
            ),
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            github_repository_error=None,
            outstanding_intents=(),
            selected_revset="@",
            stale_intents=(),
            trunk_subject="base",
        ),
    )

    @contextmanager
    def fake_progress(*, description: str, total: int):
        progress_calls.append({"description": description, "total": total})

        class Handle:
            def advance(self, amount: int = 1) -> None:
                progress_updates.append(amount)

        yield Handle()

    def fake_stream_status(**kwargs):
        kwargs["on_revision"](object(), True)
        kwargs["on_revision"](object(), True)
        return SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            incomplete=False,
            revisions=(),
        )

    monkeypatch.setattr(review_state_module, "stream_status", fake_stream_status)
    monkeypatch.setattr(review_state_module.ui, "progress", fake_progress)

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=False,
        repository=tmp_path,
        revset=None,
        verbose=False,
    )

    assert exit_code == 0
    assert progress_updates == [1, 1]
    assert progress_calls == [{"description": "Inspecting GitHub", "total": 2}]


def test_status_passes_cli_color_override_to_native_jj_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patch_bootstrap(monkeypatch, review_state_module, tmp_path)
    observed: dict[str, object] = {}
    monkeypatch.setattr("jj_review.formatting.requested_color_mode", lambda: "debug")
    monkeypatch.setattr(
        review_state_module,
        "prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(
                    list_bookmark_states=lambda: {},
                    render_revision_log_lines=lambda revision, *, color_when: (
                        f"{revision.subject} [{revision.change_id[:8]}]",
                    ),
                    resolve_color_when=lambda *, cli_color, stdout_is_tty: observed.update(
                        cli_color=cli_color,
                        stdout_is_tty=stdout_is_tty,
                    )
                    or "never",
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
                status_revisions=(),
            ),
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            github_repository_error=None,
            outstanding_intents=(),
            selected_revset="@",
            stale_intents=(),
            trunk_subject="base",
        ),
    )
    monkeypatch.setattr(
        review_state_module,
        "stream_status",
        lambda **kwargs: SimpleNamespace(
            github_error=None,
            github_repository="octo-org/stacked-review",
            incomplete=False,
            revisions=(),
        ),
    )

    exit_code = review_state_module.status(
        config_path=None,
        debug=False,
        fetch=False,
        repository=tmp_path,
        revset=None,
        verbose=False,
    )

    assert exit_code == 0
    assert observed["cli_color"] == "debug"
