import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import jj_review.cli as cli_module
from jj_review.cli import _find_subcommand_parser, build_parser, main
from jj_review.config import CONFIG_DIRNAME, CONFIG_FILENAME
from jj_review.jj import UnsupportedStackError
from jj_review.models.bookmarks import BookmarkState


def test_main_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "submit" in captured.out
    assert "land" in captured.out
    assert "close" in captured.out
    assert "import" in captured.out
    assert "cleanup" in captured.out
    assert "help" in captured.out
    assert "unlink" not in captured.out
    assert "relink" not in captured.out
    assert "completion" not in captured.out


def test_main_help_all_prints_hidden_commands(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main(["help", "--all"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Reconnect an existing pull request to a local change" in captured.out
    assert "Stop managing one local change as part of review" in captured.out
    assert "Print shell completion setup for bash, zsh, or fish" in captured.out
    assert "--repository" in captured.out
    assert "--config" in captured.out
    assert "--debug" in captured.out
    assert "--time-output" in captured.out


def test_main_help_command_prints_subcommand_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["help", "submit"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Usage: jj-review submit" in captured.out
    assert "Positional Arguments:" in captured.out
    assert "Options:" in captured.out
    assert "\nusage:" not in captured.out
    assert "\npositional arguments:" not in captured.out
    assert "\noptions:" not in captured.out
    assert "Create or update the GitHub pull requests for the selected stack of changes" in (
        captured.out
    )
    assert "--current" in captured.out


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (
            "submit",
            "Create or update the GitHub pull requests for the selected stack of changes",
        ),
        (
            "status",
            "This reports the pull requests and GitHub branches jj-review is using",
        ),
        (
            "land",
            "Without `--apply`, this command only shows what would be landed",
        ),
        (
            "close",
            "Without `--apply`, this command shows what would be closed",
        ),
        (
            "cleanup",
            "Find stale jj-review remote branches and saved local data left behind by "
            "earlier review work",
        ),
        (
            "import",
            "Without `--fetch`, this uses the selected stack only if its commits "
            "and matching pull request or branch are already available locally",
        ),
        (
            "relink",
            "Use this to repair a missing or wrong local link between a change and its "
            "pull request",
        ),
        (
            "unlink",
            "Later jj-review commands will ignore that change unless you link it again",
        ),
        (
            "completion",
            "This only prints local shell setup text and does not inspect the "
            "repository or GitHub",
        ),
        (
            "help",
            "Use `--all` to also show the advanced repair commands and hidden global "
            "options",
        ),
    ],
)
def test_subcommand_help_includes_a_command_description(
    command: str,
    expected: str,
) -> None:
    parser = _find_subcommand_parser(build_parser(), command)

    assert parser is not None
    normalized_help = " ".join(parser.format_help().split())
    assert expected in normalized_help


def test_top_level_help_uses_updated_command_summaries(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Send a jj stack to GitHub for review" in captured.out
    assert "Check the review status of a jj stack" in captured.out
    assert "Land the ready prefix of a stack" in captured.out
    assert "Stop reviewing a jj stack on GitHub" in captured.out
    assert "Clean up stale jj-review data for a jj stack" in captured.out
    assert "Set up local jj-review tracking for an existing stack" in captured.out
    assert "Show help for this command or another command" in captured.out


def test_top_level_help_describes_what_jj_review_is_for(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    normalized = " ".join(captured.out.split())
    assert "jj-review lets you review a local jj stack on GitHub" in normalized
    assert "submit changes for review" in normalized
    assert "land ready changes" in normalized


def test_default_top_level_help_hides_advanced_global_options(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "--repository" not in captured.out
    assert "--config" not in captured.out
    assert "--debug" not in captured.out
    assert "--time-output" not in captured.out
    assert "[--repository REPOSITORY]" not in captured.out
    assert "[--config CONFIG]" not in captured.out
    assert "[--debug]" not in captured.out
    assert "[--time-output]" not in captured.out


def test_help_output_omits_trailing_periods_in_command_and_option_descriptions() -> None:
    top_level_help = build_parser().format_help()
    submit_parser = _find_subcommand_parser(build_parser(), "submit")
    assert submit_parser is not None
    submit_help = submit_parser.format_help()

    assert "Send a jj stack to GitHub for review." not in top_level_help
    assert "Check the review status of a jj stack." not in top_level_help
    assert "Land the ready prefix of a stack." not in top_level_help
    assert "Stop reviewing a jj stack on GitHub." not in top_level_help
    assert "Clean up stale jj-review data for a jj stack." not in top_level_help
    assert "Workspace path to operate on; defaults to the current directory." not in (
        top_level_help
    )
    assert "Explicit path to a TOML config file." not in top_level_help
    assert "Enable debug logging." not in top_level_help
    assert (
        "Print the submit plan without mutating local, remote, or GitHub state."
        not in submit_help
    )
    assert (
        "Explicitly operate on the current stack instead of passing a revset."
        not in submit_help
    )


def test_help_output_uses_uppercase_help_and_version_descriptions() -> None:
    top_level_help = build_parser().format_help()
    submit_parser = _find_subcommand_parser(build_parser(), "submit")

    assert submit_parser is not None
    submit_help = submit_parser.format_help()

    assert "Show help" in top_level_help
    assert "Show program's version number and exit" in top_level_help
    assert "show help" not in top_level_help
    assert "show program's version number and exit" not in top_level_help
    assert "--repository" not in top_level_help
    assert "--config" not in top_level_help
    assert "--debug" not in top_level_help
    assert "--time-output" not in top_level_help
    assert "Show help" in submit_help
    assert "show help" not in submit_help


def test_top_level_help_uses_title_case_options_heading(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["help", "--all"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "\nOptions:\n" in captured.out
    assert "\noptions:\n" not in captured.out


def test_top_level_help_width_uses_terminal_width_when_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli_module.sys.stderr, "isatty", lambda: False)
    monkeypatch.setattr(
        cli_module.shutil,
        "get_terminal_size",
        lambda fallback: cli_module.shutil.os.terminal_size((100, 24)),
    )

    assert cli_module._top_level_help_width() == 100


def test_top_level_help_width_falls_back_to_80_when_not_tty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_module.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(cli_module.sys.stderr, "isatty", lambda: False)
    monkeypatch.setattr(
        cli_module.shutil,
        "get_terminal_size",
        lambda fallback: cli_module.shutil.os.terminal_size((120, 24)),
    )

    assert cli_module._top_level_help_width() == 80


def test_help_command_with_invalid_option_shows_usage_without_traceback() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "jj_review", "help", "--version"],
        capture_output=True,
        check=False,
        text=True,
    )

    assert completed.returncode == 2
    assert "usage: jj-review" in completed.stderr
    assert "unrecognized arguments: --version" in completed.stderr
    assert "Traceback" not in completed.stderr


def test_build_parser_help_hides_suppressed_alias() -> None:
    rendered = build_parser().format_help()

    assert "==SUPPRESS==" not in rendered


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
            "divergent changes are not supported.",
            change_id="nznokxmvrnysowwwkktpmroswxqsozqq",
            reason="divergent_change",
        )

    monkeypatch.setattr("jj_review.cli.prepare_status", raise_unsupported_stack)

    exit_code = main(["status", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Could not inspect review status" in captured.err
    assert "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq" in captured.err
    assert "jj log -r 'change_id(nznokxmvrnysowwwkktpmroswxqsozqq)'" in captured.err
    assert "`status --fetch` or another fetch imports remote bookmark updates" in captured.err


def test_describe_status_preparation_error_falls_back_without_structured_context() -> None:
    error = UnsupportedStackError(
        "Unsupported stack shape at nznokxmvrnysowwwkktpmroswxqsozqq: "
        "divergent changes are not supported."
    )

    assert "jj log -r" not in cli_module._describe_status_preparation_error(error)


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


def test_main_submit_rejects_draft_and_publish_together() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["submit", "--draft", "--publish", "@"])

    assert exc_info.value.code == 2


def test_main_submit_rejects_invalid_draft_mode(
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

    exit_code = main(["submit", "--draft=oops", "--current", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Invalid value for `--draft`" in captured.err


def test_main_land_requires_explicit_revision_selection(
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

    def fake_run_land(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("land should not run without an explicit selector")

    monkeypatch.setattr("jj_review.commands.land.run_land", fake_run_land)

    exit_code = main(["land", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert not run_called
    assert "requires an explicit revision selection" in captured.err


def test_main_close_requires_explicit_revision_selection(
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

    def fake_run_close(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("close should not run without an explicit selector")

    monkeypatch.setattr("jj_review.commands.close.run_close", fake_run_close)

    exit_code = main(["close", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert not run_called
    assert "requires an explicit revision selection" in captured.err


def test_main_close_rejects_revset_and_current_together(
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

    exit_code = main(["close", "--current", "--repository", str(tmp_path), "@"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "accepts either `<revset>` or `--current`, not both" in captured.err


def test_main_close_renders_planned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def fake_run_close(**kwargs):
        assert kwargs["apply"] is False
        assert kwargs["cleanup"] is True
        assert kwargs["revset"] == "@"
        return SimpleNamespace(
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
        )

    monkeypatch.setattr("jj_review.commands.close.run_close", fake_run_close)

    exit_code = main(["close", "--cleanup", "--repository", str(tmp_path), "@"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected revset: @" in captured.out
    assert "Selected remote: origin" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Planned close actions:" in captured.out
    assert "- [planned] pull request: close PR #7 for feature 1 [aaaaaaaa]" in captured.out
    assert "- [planned] tracking: stop saved jj-review tracking for feature 1 [aaaaaaaa]" in (
        captured.out
    )
    assert "Re-run with `close --apply --cleanup @`" in captured.out


def test_main_close_renders_apply_noop_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def fake_run_close(**kwargs):
        assert kwargs["apply"] is True
        assert kwargs["cleanup"] is False
        assert kwargs["revset"] == "@"
        return SimpleNamespace(
            actions=(),
            applied=True,
            blocked=False,
            cleanup=False,
            github_error=None,
            github_repository="octo-org/stacked-review",
            remote=SimpleNamespace(name="origin"),
            remote_error=None,
            selected_revset="@",
        )

    monkeypatch.setattr("jj_review.commands.close.run_close", fake_run_close)

    exit_code = main(["close", "--apply", "--repository", str(tmp_path), "@"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "No close actions were needed for the selected stack." in captured.out
    assert "No managed open pull requests" not in captured.out


def test_main_import_requires_exactly_one_selector() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["import"])

    assert exc_info.value.code == 2


def test_main_import_rejects_multiple_selectors() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["import", "--current", "--revset", "@"])

    assert exc_info.value.code == 2


def test_main_import_renders_up_to_date_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def fake_run_import(**kwargs):
        assert kwargs["current"] is False
        assert kwargs["fetch"] is False
        assert kwargs["head"] == "review/feature-aaaaaaaa"
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

    monkeypatch.setattr("jj_review.commands.import_.run_import", fake_run_import)

    exit_code = main(
        [
            "import",
            "--repository",
            str(tmp_path),
            "--head",
            "review/feature-aaaaaaaa",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected selector: --head review/feature-aaaaaaaa" in captured.out
    assert "Local jj-review tracking is already up to date for the selected stack." in (
        captured.out
    )
    assert "No reviewable commits" not in captured.out


def test_main_import_renders_unavailable_github_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def fake_run_import(**kwargs):
        assert kwargs["fetch"] is False
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

    monkeypatch.setattr("jj_review.commands.import_.run_import", fake_run_import)

    exit_code = main(["import", "--repository", str(tmp_path), "--current"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: unavailable" in captured.out
    assert "GitHub: unavailable" in captured.out
    assert "GitHub target:" not in captured.out


def test_main_import_fetch_renders_fetched_tip_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def fake_run_import(**kwargs):
        assert kwargs["fetch"] is True
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

    monkeypatch.setattr("jj_review.commands.import_.run_import", fake_run_import)

    exit_code = main(
        [
            "import",
            "--repository",
            str(tmp_path),
            "--fetch",
            "--pull-request",
            "2",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Fetched tip commit: commit-2" in captured.out


def test_main_land_renders_planned_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def fake_run_land(**kwargs):
        assert kwargs["bypass_readiness"] is False
        assert kwargs["expect_pr_reference"] == "7"
        assert kwargs["revset"] == "@-"
        return SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="trunk",
                    message="push main to feature 1 [aaaaaaaa]",
                    status="planned",
                ),
                SimpleNamespace(
                    kind="pull request",
                    message="finalize PR #7 for feature 1 [aaaaaaaa]",
                    status="planned",
                ),
            ),
            applied=False,
            bypass_readiness=False,
            blocked=False,
            expect_pr_number=7,
            follow_up=None,
            github_repository="octo-org/stacked-review",
            remote_name="origin",
            selected_revset="@-",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.commands.land.run_land", fake_run_land)

    exit_code = main(
        [
            "land",
            "--repository",
            str(tmp_path),
            "--expect-pr",
            "7",
            "@-",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected revset: @-" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Planned land actions:" in captured.out
    assert "- [planned] trunk: push main to feature 1 [aaaaaaaa]" in captured.out
    assert "Re-run with `land --apply --expect-pr 7 @-`" in captured.out


def test_main_land_renders_blocked_output_without_apply_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def fake_run_land(**kwargs):
        assert kwargs["bypass_readiness"] is False
        return SimpleNamespace(
            actions=(
                SimpleNamespace(
                    kind="guardrail",
                    message=(
                        "`--expect-pr 7` did not match the changes that can be "
                        "landed now."
                    ),
                    status="blocked",
                ),
            ),
            applied=False,
            bypass_readiness=False,
            blocked=True,
            expect_pr_number=7,
            follow_up=None,
            github_repository="octo-org/stacked-review",
            remote_name="origin",
            selected_revset="@-",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.commands.land.run_land", fake_run_land)

    exit_code = main(
        [
            "land",
            "--repository",
            str(tmp_path),
            "--expect-pr",
            "7",
            "@-",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Land blocked:" in captured.out
    assert "- [blocked] guardrail:" in captured.out
    assert "Re-run with `land --apply" not in captured.out


def test_main_land_passes_bypass_readiness_and_renders_apply_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    def fake_run_land(**kwargs):
        assert kwargs["bypass_readiness"] is True
        return SimpleNamespace(
            actions=(),
            applied=False,
            bypass_readiness=True,
            blocked=False,
            expect_pr_number=7,
            follow_up=None,
            github_repository="octo-org/stacked-review",
            remote_name="origin",
            selected_revset="@-",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.commands.land.run_land", fake_run_land)

    exit_code = main(
        [
            "land",
            "--repository",
            str(tmp_path),
            "--bypass-readiness",
            "--expect-pr",
            "7",
            "@-",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Re-run with `land --apply --bypass-readiness --expect-pr 7 @-`" in captured.out


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
                    pull_request_url=None,
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
    assert "Planned bookmarks:" in captured.out
    assert "- feature 1 [abcdefgh]" in captured.out
    assert "  -> review/feature-abcdefgh [new PR]" in captured.out
    assert "Top of stack:" not in captured.out


def test_main_submit_passes_draft_mode_to_submit_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    draft_modes: list[str] = []

    def fake_run_submit(**kwargs):
        draft_modes.append(kwargs["draft_mode"])
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.run_submit", fake_run_submit)

    exit_code = main(["submit", "--draft", "--current", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert draft_modes == ["draft"]
    assert "No reviewable commits" in captured.out


def test_main_submit_passes_draft_all_mode_to_submit_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    draft_modes: list[str] = []

    def fake_run_submit(**kwargs):
        draft_modes.append(kwargs["draft_mode"])
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.run_submit", fake_run_submit)

    exit_code = main(["submit", "--draft=all", "--current", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert draft_modes == ["draft_all"]
    assert "No reviewable commits" in captured.out


def test_main_submit_passes_reviewer_overrides_to_submit_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    reviewer_calls: list[tuple[list[str] | None, list[str] | None]] = []

    def fake_run_submit(**kwargs):
        reviewer_calls.append((kwargs["reviewers"], kwargs["team_reviewers"]))
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.run_submit", fake_run_submit)

    exit_code = main(
        [
            "submit",
            "--reviewers",
            "alice,bob",
            "--team-reviewers",
            "platform",
            "--reviewers",
            "bob,carol",
            "--team-reviewers",
            "infra,platform",
            "--current",
            "--repository",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert reviewer_calls == [(["alice", "bob", "carol"], ["platform", "infra"])]
    assert "No reviewable commits" in captured.out


def test_main_submit_passes_describe_with_to_submit_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    describe_with_calls: list[str | None] = []

    def fake_run_submit(**kwargs):
        describe_with_calls.append(kwargs["describe_with"])
        return SimpleNamespace(
            dry_run=False,
            remote=SimpleNamespace(name="origin"),
            revisions=(),
            selected_revset="@",
            trunk_branch="main",
            trunk_subject="base",
        )

    monkeypatch.setattr("jj_review.cli.run_submit", fake_run_submit)

    exit_code = main(
        [
            "submit",
            "-d",
            "scripts/describe_with_codex.py",
            "--current",
            "--repository",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 0
    assert describe_with_calls == ["scripts/describe_with_codex.py"]
    assert "No reviewable commits" in captured.out


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
        pull_request_url=None,
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
    assert captured.out.count("Planned bookmarks:") == 1
    assert captured.out.count("- feature 1 [abcdefgh]") == 1
    assert captured.out.count("  -> review/feature-abcdefgh [new PR]") == 1
    assert "Top of stack:" not in captured.out


def test_main_submit_prints_top_pull_request_url_at_end(
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
        pull_request_url="https://github.test/example/repo/pull/7",
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

    exit_code = main(["submit", "--current", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out.rstrip().endswith(
        "Top of stack: https://github.test/example/repo/pull/7"
    )


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
        pull_request_url="https://github.test/example/repo/pull/7",
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
        if "review/feature-abcdefgh [PR #7]" in line
    )
    top_pr_line = next(
        line
        for line in captured.out.splitlines()
        if "Top of stack: https://github.test/example/repo/pull/7" in line
    )
    assert summary_line.count("[") == 2
    assert top_pr_line.count("[") == 1


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
    assert "No merged changes on the selected stack need restacking." in captured.out


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

    monkeypatch.setattr("jj_review.cli.stream_cleanup", fake_stream_cleanup)

    exit_code = main(["cleanup", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" in captured.out
    assert "GitHub: octo-org/stacked-review" in captured.out
    assert "Planned cleanup actions:" in captured.out
    assert "[planned] tracking: remove saved jj-review data for abcdef12" in captured.out
    assert "[blocked] remote branch: cannot delete review/feature-abcdef12@origin" in (
        captured.out
    )
    assert "cleanup --apply" in captured.out


def test_main_relink_requires_explicit_revision_selection(
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

    def fake_run_relink(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("relink should not run without an explicit selector")

    monkeypatch.setattr("jj_review.commands.relink.run_relink", fake_run_relink)

    exit_code = main(["relink", "--repository", str(tmp_path), "123"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert not run_called
    assert "requires an explicit revision selection" in captured.err


def test_main_relink_current_passes_current_path_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    relink_calls: list[str | None] = []

    def fake_run_relink(**kwargs):
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

    monkeypatch.setattr("jj_review.commands.relink.run_relink", fake_run_relink)

    exit_code = main(["relink", "--current", "--repository", str(tmp_path), "7"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert relink_calls == [None]
    assert "Relinked PR #7 for feature 1 [abcdefgh] -> review/feature-abcdefgh" in (
        captured.out
    )


def test_main_unlink_requires_explicit_revision_selection(
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

    def fake_run_unlink(**kwargs):
        nonlocal run_called
        run_called = True
        raise AssertionError("unlink should not run without an explicit selector")

    monkeypatch.setattr("jj_review.commands.unlink.run_unlink", fake_run_unlink)

    exit_code = main(["unlink", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert not run_called
    assert "requires an explicit revision selection" in captured.err


def test_main_unlink_current_passes_current_path_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)
    calls: list[str | None] = []

    def fake_run_unlink(**kwargs):
        calls.append(kwargs["revset"])
        return SimpleNamespace(
            already_unlinked=False,
            bookmark="review/feature-abcdefgh",
            change_id="abcdefghijkl",
            selected_revset="@",
            subject="feature 1",
        )

    monkeypatch.setattr("jj_review.commands.unlink.run_unlink", fake_run_unlink)

    exit_code = main(["unlink", "--current", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert calls == [None]
    assert "Stopped review tracking for feature 1 [abcdefgh]" in captured.out


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

    monkeypatch.setattr("jj_review.cli.stream_cleanup", fake_stream_cleanup)

    exit_code = main(["cleanup", "--repository", str(tmp_path)])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Selected remote: origin" in checkpoints[0]
    assert "GitHub: octo-org/stacked-review" in checkpoints[0]
    assert "Planned cleanup actions:" in checkpoints[1]
    assert "[planned] tracking: remove saved jj-review data for abcdef12" in checkpoints[1]
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
    assert "jj-review lets you review a local jj stack on GitHub" in completed.stdout


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
