import subprocess
import sys
from argparse import Namespace
from pathlib import Path

import pytest

import jj_review.cli as cli_module
from jj_review.cli import (
    _completion_handler,
    _find_subcommand_parser,
    _normalize_cli_args,
    build_parser,
    main,
)
from jj_review.config import CONFIG_DIRNAME, CONFIG_FILENAME


def test_main_without_command_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "submit" in captured.out
    assert "land" in captured.out
    assert "close" in captured.out
    assert "import" in captured.out
    assert "cleanup" in captured.out
    assert "Show help for this command or another command" not in captured.out
    assert "unlink" not in captured.out
    assert "relink" not in captured.out
    assert "completion" not in captured.out


def test_main_help_all_prints_hidden_commands(
    capsys: pytest.CaptureFixture[str],
) -> None:
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
            "By default, this command performs the landing",
        ),
        (
            "close",
            "By default, this closes those pull requests",
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


def test_subcommand_help_preserves_description_paragraph_breaks() -> None:
    submit_parser = _find_subcommand_parser(build_parser(), "submit")

    assert submit_parser is not None
    assert (
        "Create or update the GitHub pull requests for the selected stack of changes.\n\n"
        "This pushes or updates the GitHub branches for that stack"
        in submit_parser.format_help()
    )


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
    assert "Show help for this command or another command" not in captured.out


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
    assert "Show help for this command or another command." not in top_level_help
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


def test_submit_help_describe_with_uses_change_ids() -> None:
    submit_parser = _find_subcommand_parser(build_parser(), "submit")

    assert submit_parser is not None
    submit_help = submit_parser.format_help()

    assert "`helper --pr <change_id>`" in submit_help
    assert "`helper --pr <revset>`" not in submit_help


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("submit", "Revision to submit; required unless --current is passed"),
        ("unlink", "Revision to unlink; required unless --current is passed"),
        ("land", "Revision to land; required unless --current is passed"),
        ("close", "Revision to close; required unless --current is passed"),
        (
            "cleanup",
            "when mutating with --restack, pass this or --current",
        ),
        ("status", "Revision to inspect; defaults to the current stack"),
    ],
)
def test_revision_help_text_matches_selector_rules(command: str, expected: str) -> None:
    parser = _find_subcommand_parser(build_parser(), command)

    assert parser is not None
    normalized_help = " ".join(parser.format_help().split())
    assert expected in normalized_help


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


def test_main_help_command_is_hidden_from_default_top_level_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main([])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "\nHelp:\n" not in captured.out
    assert "Show help for this command or another command" not in captured.out


def test_main_help_all_includes_hidden_help_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["help", "--all"])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert "Show help for this command or another command" in captured.out


def test_main_help_matches_top_level_help(capsys: pytest.CaptureFixture[str]) -> None:
    parser = build_parser()

    help_exit_code = main(["help"])
    help_output = capsys.readouterr()

    assert help_exit_code == 0
    assert help_output.err == ""
    assert help_output.out == parser.format_help()


def test_main_help_submit_matches_submit_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    submit_parser = _find_subcommand_parser(build_parser(), "submit")

    help_exit_code = main(["help", "submit"])
    help_output = capsys.readouterr()

    assert help_exit_code == 0
    assert submit_parser is not None
    assert help_output.err == ""
    assert help_output.out == submit_parser.format_help()


def test_main_time_output_prefixes_help_lines(
    capsys: pytest.CaptureFixture[str],
) -> None:
    exit_code = main(["--time-output"])
    captured = capsys.readouterr()

    assert exit_code == 0
    lines = [line for line in captured.out.splitlines() if line]
    assert lines
    assert all(line.startswith("[") for line in lines)
    assert any("submit" in line for line in lines)


def test_completion_handler_prints_shell_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(cli_module, "emit_shell_completion", lambda parser, shell: f"{shell}-out")

    exit_code = _completion_handler(Namespace(shell="zsh"))
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == "zsh-out"


def test_main_reports_invalid_config_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "bad.toml"
    config_path.write_text("[repo]\nremote = [\n", encoding="utf-8")

    exit_code = main(["--config", str(config_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert captured.err.startswith("Error: ")
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
    monkeypatch.setattr("jj_review.bootstrap.resolve_repo_root", lambda _: tmp_path)

    exit_code = main(["--repository", str(tmp_path), "submit"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "Invalid logging level" in captured.err
    assert "DEBIG" in captured.err
    assert "Traceback" not in captured.err


def test_normalize_cli_args_rewrites_draft_new() -> None:
    assert _normalize_cli_args(["submit", "--draft=new", "@"]) == [
        "submit",
        "--draft",
        "@",
    ]


def test_normalize_cli_args_rewrites_draft_all() -> None:
    assert _normalize_cli_args(["submit", "--draft=all", "@"]) == [
        "submit",
        "--draft-all",
        "@",
    ]


def test_normalize_cli_args_rejects_invalid_draft_mode() -> None:
    with pytest.raises(cli_module.CliError, match="Invalid value for `--draft`"):
        _normalize_cli_args(["submit", "--draft=oops", "@"])


def test_main_submit_rejects_draft_and_publish_together() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["submit", "--draft", "--publish", "@"])

    assert exc_info.value.code == 2


def test_main_import_requires_exactly_one_selector() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["import"])

    assert exc_info.value.code == 2


def test_main_import_rejects_multiple_selectors() -> None:
    with pytest.raises(SystemExit) as exc_info:
        build_parser().parse_args(["import", "--current", "--revset", "@"])

    assert exc_info.value.code == 2


def test_main_reports_non_jj_directory_without_traceback(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
