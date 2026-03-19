import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jj_review.cli import main
from jj_review.config import CONFIG_DIRNAME, CONFIG_FILENAME
from jj_review.jj import UnsupportedStackError
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


def test_main_status_reports_targeted_divergent_stack_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def raise_unsupported_stack(**kwargs):
        raise UnsupportedStackError(
            "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq: "
            "divergent changes are not supported."
        )

    monkeypatch.setattr("jj_review.cli.prepare_status", raise_unsupported_stack)

    exit_code = main(["status", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not inspect review status" in captured.err
    assert "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq" in captured.err
    assert "jj log -r 'change_id(nznokxmvrnysowwwkktpmroswxqsozqq)'" in captured.err
    assert "`status --fetch` or another fetch imports remote bookmark updates" in captured.err


def test_main_submit_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.bootstrap.load_config",
        lambda **kwargs: SimpleNamespace(
            change={},
            logging=SimpleNamespace(level="WARNING"),
            repo=SimpleNamespace(),
        ),
    )
    run_called = False

    def fake_run_submit(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("submit should not run without an explicit selector")

    monkeypatch.setattr("jj_review.cli.run_submit", fake_run_submit)

    exit_code = main(["submit", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert not run_called
    assert "requires an explicit revision selection" in captured.err


def test_main_submit_rejects_revset_and_current_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.bootstrap.load_config",
        lambda **kwargs: SimpleNamespace(
            change={},
            logging=SimpleNamespace(level="WARNING"),
            repo=SimpleNamespace(),
        ),
    )

    exit_code = main(["submit", "--current", "--repository", str(tmp_path), "@-"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "accepts either `<revset>` or `--current`, not both" in captured.err


def test_main_submit_passes_dry_run_and_renders_planned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    dry_run_calls: list[bool] = []
    selected_revsets: list[str | None] = []

    def fake_run_submit(**kwargs):
        dry_run_calls.append(bool(kwargs["dry_run"]))
        selected_revsets.append(kwargs["revset"])
        return SimpleNamespace(
            dry_run=True,
            remote=SimpleNamespace(name="origin"),
            revisions=(
                SimpleNamespace(
                    bookmark="review/feature-abcdefgh",
                    bookmark_source="generated",
                    change_id="abcdefghijkl",
                    local_action="created",
                    pull_request_action="created",
                    pull_request_number=None,
                    remote_action="pushed",
                    subject="feature 1",
                ),
            ),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.run_submit", fake_run_submit)

    exit_code = main(["submit", "--dry-run", "--current", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert dry_run_calls == [True]
    assert selected_revsets == [None]
    assert "Dry run: no local, remote, or GitHub changes applied." in captured.out
    assert "Planned review bookmarks:" in captured.out
    assert "- feature 1 [abcdefgh]" in captured.out
    assert "  -> review/feature-abcdefgh [pushed] [PR #n created]" in captured.out


def test_main_submit_prints_final_output_without_duplicate_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    revision = SimpleNamespace(
        bookmark="review/feature-abcdefgh",
        bookmark_source="generated",
        change_id="abcdefghijkl",
        local_action="created",
        pull_request_action="created",
        pull_request_number=None,
        remote_action="pushed",
        subject="feature 1",
    )

    def fake_run_submit(**kwargs):
        kwargs["on_prepared"]("@", SimpleNamespace(name="origin"), True)
        kwargs["on_trunk_resolved"]("base", "main", True)
        return SimpleNamespace(
            dry_run=True,
            remote=SimpleNamespace(name="origin"),
            revisions=(revision,),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.run_submit", fake_run_submit)

    exit_code = main(["submit", "--dry-run", "--current", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.count("Selected revset: @") == 1
    assert captured.out.count("Selected remote: origin") == 1
    assert captured.out.count("Trunk: base -> main") == 1
    assert captured.out.count("Dry run: no local, remote, or GitHub changes applied.") == 1
    assert captured.out.count("Planned review bookmarks:") == 1
    assert captured.out.count("- feature 1 [abcdefgh]") == 1
    assert captured.out.count("  -> review/feature-abcdefgh [pushed] [PR #n created]") == 1


def test_main_time_output_prefixes_submit_summary_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    revision = SimpleNamespace(
        bookmark="review/feature-abcdefgh",
        bookmark_source="generated",
        change_id="abcdefghijkl",
        local_action="created",
        pull_request_action="created",
        pull_request_number=7,
        remote_action="pushed",
        subject="feature 1",
    )

    def fake_run_submit(**kwargs):
        kwargs["on_prepared"]("@", SimpleNamespace(name="origin"), True)
        kwargs["on_trunk_resolved"]("base", "main", True)
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(revision,),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.run_submit", fake_run_submit)

    exit_code = main(["submit", "--current", "--time-output", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    summary_line = next(
        line
        for line in captured.out.splitlines()
        if "review/feature-abcdefgh [pushed] [PR #7 created]" in line
    )
    assert summary_line.count("[") == 3


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


def test_main_cleanup_restack_passes_apply_and_revset_to_prepare_restack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
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

    monkeypatch.setattr("jj_review.cli.prepare_restack", fake_prepare_restack)
    monkeypatch.setattr(
        "jj_review.cli.stream_restack",
        lambda **kwargs: SimpleNamespace(
            actions=(),
            applied=True,
            blocked=False,
            selected_revset="@-",
        ),
    )

    exit_code = main(
        ["cleanup", "--restack", "--apply", "--repository", str(tmp_path), "@-"]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert prepare_calls == [(True, "@-")]
    assert "Selected revset: @-" in captured.out
    assert "No merged review units on the selected path need restacking." in captured.out


def test_main_cleanup_restack_apply_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.bootstrap.load_config",
        lambda **kwargs: SimpleNamespace(
            change={},
            logging=SimpleNamespace(level="WARNING"),
            repo=SimpleNamespace(),
        ),
    )
    prepare_called = False

    def fake_prepare_restack(**kwargs):
        nonlocal prepare_called
        prepare_called = True
        raise AssertionError("restack apply should not prepare without a selector")

    monkeypatch.setattr("jj_review.cli.prepare_restack", fake_prepare_restack)

    exit_code = main(["cleanup", "--restack", "--apply", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert not prepare_called
    assert "requires an explicit revision selection" in captured.err


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


def test_main_adopt_requires_explicit_revision_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.bootstrap.load_config",
        lambda **kwargs: SimpleNamespace(
            change={},
            logging=SimpleNamespace(level="WARNING"),
            repo=SimpleNamespace(),
        ),
    )
    run_called = False

    def fake_run_adopt(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("adopt should not run without an explicit selector")

    monkeypatch.setattr("jj_review.cli.run_adopt", fake_run_adopt)

    exit_code = main(["adopt", "--repository", str(tmp_path), "123"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert not run_called
    assert "requires an explicit revision selection" in captured.err


def test_main_adopt_current_passes_current_path_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    adopt_calls: list[str | None] = []

    def fake_run_adopt(**kwargs):
        adopt_calls.append(kwargs["revset"])
        return SimpleNamespace(
            bookmark="review/feature-abcdefgh",
            change_id="abcdefghijkl",
            github_repository="octo-org/stacked-review",
            pull_request_number=7,
            remote_name="origin",
            selected_revset="@",
            subject="feature 1",
        )

    monkeypatch.setattr("jj_review.cli.run_adopt", fake_run_adopt)

    exit_code = main(["adopt", "--current", "--repository", str(tmp_path), "7"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert adopt_calls == [None]
    assert "Adopted PR #7 for feature 1 [abcdefgh] -> review/feature-abcdefgh" in (
        captured.out
    )


def test_main_cleanup_restack_renders_next_step_and_policy_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    monkeypatch.setattr(
        "jj_review.cli.prepare_restack",
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

    monkeypatch.setattr("jj_review.cli.stream_restack", fake_stream_restack)

    exit_code = main(["cleanup", "--restack", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected revset: @" in captured.out
    assert "Planned restack actions:" in captured.out
    assert "[planned] restack: rebase abcdef12 onto trunk()" in captured.out
    assert "[planned] policy: PR #5 merged into review/base-branch" in captured.out
    assert "cleanup --restack --apply @" in captured.out


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
            outstanding_intents=(),
            selected_revset="@",
            stale_intents=(),
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


def test_main_status_prints_cleanup_advisories_for_merged_review_units(
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
                status_revisions=(object(),),
            ),
            outstanding_intents=(),
            selected_revset="@",
            stale_intents=(),
            trunk_subject="base",
        ),
    )

    merged_revision = SimpleNamespace(
        cached_change=None,
        change_id="abcdefghijkl",
        local_divergent=False,
        pull_request_lookup=SimpleNamespace(
            pull_request=SimpleNamespace(
                base=SimpleNamespace(ref="review/feature-base"),
                number=5,
                state="merged",
            ),
            state="closed",
        ),
        stack_comment_lookup=None,
        subject="feature 1",
    )

    def fake_stream_status(**kwargs):
        kwargs["on_github_status"]("octo-org/stacked-review", None)
        kwargs["on_revision"](merged_revision, True)
        return SimpleNamespace(
            incomplete=False,
            revisions=(merged_revision,),
            selected_revset="@",
        )

    monkeypatch.setattr("jj_review.cli.stream_status", fake_stream_status)

    exit_code = main(["status", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "- feature 1 [abcdefgh]: PR #5 merged, cleanup needed" in captured.out
    assert "◆ base [trunkcha]: trunk()\n\nAdvisories:" in captured.out
    assert "Advisories:" in captured.out
    assert "Submit note: descendant PR bases still follow the old local ancestry" in (
        captured.out
    )
    assert "jj-review cleanup --restack @" in captured.out
    assert "[abcdefgh]: PR #5 is merged, and later local changes are still based on it" in (
        captured.out
    )
    assert "Repository policy warning: PR #5 merged into review/feature-base;" in (
        captured.out
    )
    advisory_lines = captured.out.split("Advisories:\n", maxsplit=1)[1].splitlines()
    assert all(len(line) <= 80 for line in advisory_lines if line)


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
