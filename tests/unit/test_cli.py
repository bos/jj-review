import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.cli import main
from jj_review.config import CONFIG_DIRNAME, CONFIG_FILENAME
from jj_review.models.bookmarks import BookmarkState


def test_main_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "submit" in captured.out
    assert "cleanup" in captured.out


def test_main_time_output_prefixes_help_lines(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["--time-output"])
    captured = capsys.readouterr()

    assert exit_code == 0
    lines = [line for line in captured.out.splitlines() if line]
    assert lines
    assert all(line.startswith("[") for line in lines)
    assert any("submit" in line for line in lines)


def test_main_reports_invalid_config_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "bad.toml"
    config_path.write_text("[repo]\nremote = [\n", encoding="utf-8")

    exit_code = main(["--config", str(config_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Invalid jj-review config" in captured.err
    assert "Traceback" not in captured.err


def test_main_reports_missing_repository_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repository = tmp_path / "missing-repo"

    exit_code = main(["--repository", str(repository), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert str(repository) in captured.err
    assert "does not exist" in captured.err
    assert "Traceback" not in captured.err


def test_main_reports_invalid_logging_level_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "config-home" / CONFIG_DIRNAME / CONFIG_FILENAME
    config_path.parent.mkdir(parents=True)
    config_path.write_text('[logging]\nlevel = "DEBIG"\n', encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config-home"))
    # Bypass jj workspace resolution so the test focuses on config validation.
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    exit_code = main(["--repository", str(tmp_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Invalid logging level" in captured.err
    assert "DEBIG" in captured.err
    assert "Traceback" not in captured.err


def test_main_accepts_global_options_after_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=None,
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
            selected_revset="@",
            trunk_subject="base",
        ),
    )
    monkeypatch.setattr(
        "jj_review.cli.stream_status",
        lambda **kwargs: SimpleNamespace(incomplete=False),
    )

    exit_code = main(["status", "--debug", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected revset: @" in captured.out
    assert "◆ base [trunkcha]: trunk()" in captured.out
    assert "No reviewable commits" in captured.out
    assert "Traceback" not in captured.err


def test_main_status_passes_fetch_to_prepare_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    prepare_calls: list[bool] = []

    def fake_prepare_status(**kwargs):
        prepare_calls.append(bool(kwargs["fetch_remote_state"]))
        return SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=None,
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
            selected_revset="@",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.prepare_status", fake_prepare_status)
    monkeypatch.setattr(
        "jj_review.cli.stream_status",
        lambda **kwargs: SimpleNamespace(incomplete=False),
    )

    exit_code = main(["status", "--fetch", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No reviewable commits" in captured.out
    assert prepare_calls == [True]


def test_main_status_short_fetch_alias_passes_fetch_to_prepare_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    prepare_calls: list[bool] = []

    def fake_prepare_status(**kwargs):
        prepare_calls.append(bool(kwargs["fetch_remote_state"]))
        return SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=None,
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
            selected_revset="@",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.prepare_status", fake_prepare_status)
    monkeypatch.setattr(
        "jj_review.cli.stream_status",
        lambda **kwargs: SimpleNamespace(incomplete=False),
    )

    exit_code = main(["status", "-f", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No reviewable commits" in captured.out
    assert prepare_calls == [True]


def test_main_cleanup_passes_apply_to_prepare_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
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

    def fake_stream_cleanup(**kwargs):
        return SimpleNamespace(
            actions=(),
            applied=True,
            github_error=None,
            github_repository=None,
            remote=None,
            remote_error=None,
        )

    monkeypatch.setattr("jj_review.cli.prepare_cleanup", fake_prepare_cleanup)
    monkeypatch.setattr("jj_review.cli.stream_cleanup", fake_stream_cleanup)

    exit_code = main(["cleanup", "--apply", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert apply_calls == [True]
    assert "No cleanup actions planned." in captured.out


def test_main_cleanup_renders_planned_and_blocked_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_cleanup",
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
                kind="cache",
                message="remove cached review state for abcdef12",
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
                    kind="cache",
                    message="remove cached review state for abcdef12",
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

    monkeypatch.setattr("jj_review.cli.stream_cleanup", fake_stream_cleanup)

    exit_code = main(["cleanup", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Planned cleanup actions:" in captured.out
    assert "[planned] cache: remove cached review state for abcdef12" in captured.out
    assert "[blocked] remote branch: cannot delete review/feature-abcdef12@origin" in (
        captured.out
    )
    assert "cleanup --apply" in captured.out


def test_main_cleanup_prints_remote_and_github_before_stream_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_cleanup",
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
                kind="cache",
                message="remove cached review state for abcdef12",
                status="planned",
            )
        )
        checkpoints.append(capsys.readouterr().out)
        return SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="cache",
                    message="remove cached review state for abcdef12",
                    status="planned",
                ),
            ),
            applied=False,
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
        )

    monkeypatch.setattr("jj_review.cli.stream_cleanup", fake_stream_cleanup)

    exit_code = main(["cleanup", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" in checkpoints[0]
    assert "GitHub: octo-org/stacked-review" in checkpoints[0]
    assert "Planned cleanup actions:" in checkpoints[1]
    assert "[planned] cache: remove cached review state for abcdef12" in checkpoints[1]
    assert "cleanup --apply" in captured.out


def test_main_status_reports_uninspected_github_target_for_empty_stack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
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
            selected_revset="main",
            trunk_subject="base",
        ),
    )

    def fake_stream_status(**kwargs):
        kwargs["on_github_status"](
            "octo-org/stacked-review",
            "not inspected; no reviewable commits",
        )
        return SimpleNamespace(incomplete=False)

    monkeypatch.setattr("jj_review.cli.stream_status", fake_stream_status)

    exit_code = main(["status", "--repository", str(tmp_path), "main"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert (
        "GitHub target: octo-org/stacked-review "
        "(not inspected; no reviewable commits)"
    ) in captured.out
    assert "No reviewable commits" in captured.out


def test_main_time_output_prefixes_status_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=None,
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
            selected_revset="@",
            trunk_subject="base",
        ),
    )
    monkeypatch.setattr(
        "jj_review.cli.stream_status",
        lambda **kwargs: SimpleNamespace(incomplete=False),
    )

    exit_code = main(["status", "--time-output", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    lines = [line for line in captured.out.splitlines() if line]
    assert lines
    assert all(line.startswith("[") for line in lines)
    assert any("Selected revset: @" in line for line in lines)
    assert any("◆ base [trunkcha]: trunk()" in line for line in lines)


def test_main_reports_keyboard_interrupt_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_status",
        lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    exit_code = main(["status", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert captured.err.strip() == "Interrupted."
    assert "Traceback" not in captured.err


def test_main_status_prints_local_header_before_streaming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(
                    list_bookmark_states=lambda: {
                        "main": BookmarkState(
                            name="main",
                            local_targets=("trunk-commit",),
                        )
                    }
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
                status_revisions=(object(),),
            ),
            selected_revset="@",
            trunk_subject="base",
        ),
    )

    def fake_stream_status(**kwargs):
        streamed = capsys.readouterr()
        assert "Selected revset: @" in streamed.out
        assert "Selected remote: origin" in streamed.out
        assert "◆ base [trunkcha" not in streamed.out
        kwargs["on_github_status"]("octo-org/stacked-review", None)
        kwargs["on_revision"](
            SimpleNamespace(
                cached_change=None,
                change_id="abcdefghijkl",
                pull_request_lookup=SimpleNamespace(
                    pull_request=SimpleNamespace(number=1),
                    state="open",
                ),
                stack_comment_lookup=None,
                subject="feature 1",
            ),
            True,
        )
        return SimpleNamespace(incomplete=False)

    monkeypatch.setattr(
        "jj_review.cli.stream_status",
        fake_stream_status,
    )

    exit_code = main(["status", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Stack:" in captured.out
    assert "- feature 1 [abcdefgh]: PR #1" in captured.out
    assert "◆ base [trunkcha]: main" in captured.out
    assert captured.out.index("- feature 1 [abcdefgh]: PR #1") < captured.out.index(
        "◆ base [trunkcha]: main"
    )


def test_main_reports_keyboard_interrupt_during_status_stream_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda: {}),
                remote=None,
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
            selected_revset="@",
            trunk_subject="base",
        ),
    )
    monkeypatch.setattr(
        "jj_review.cli.stream_status",
        lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    exit_code = main(["status", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert "Selected revset: @" in captured.out
    assert "Selected remote: unavailable" in captured.out
    assert "◆ base [trunkcha]" not in captured.out
    assert captured.err.strip() == "Interrupted."
    assert "Traceback" not in captured.err


def test_main_time_output_prefixes_interrupt_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_status",
        lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    exit_code = main(["status", "--time-output", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert captured.err.startswith("[")
    assert "Interrupted." in captured.err


def test_main_reports_non_jj_directory_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A plain directory with no jj workspace should fail fast with a clear
    # BootstrapError, not silently proceed and fail later.
    plain_dir = tmp_path / "not-a-jj-repo"
    plain_dir.mkdir()

    exit_code = main(["--repository", str(plain_dir), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Not inside a jj workspace" in captured.err
    assert "Traceback" not in captured.err


def test_python_m_jj_review_prints_help() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "jj_review", "--help"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert "JJ-native stacked GitHub review tooling" in completed.stdout


def test_importing_package_main_module_does_not_exit() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", "import jj_review.__main__; print('ok')"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 0
    assert completed.stdout.strip() == "ok"
    assert completed.stderr == ""
