from pathlib import Path

import pytest

import jj_stack.cli as cli_module
from jj_stack.cli import (
    _extract_config_overrides,
    _normalize_cli_args,
    build_parser,
    main,
)
from jj_stack.errors import EXIT_INTERRUPTED

pytestmark = pytest.mark.usefixtures("no_configured_color")


def test_main_preserves_partial_handler_output_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_view(**kwargs) -> int:
        print("before interrupt")
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_module.view_command, "view", fake_view)

    exit_code = main(["view"])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert "before interrupt" in captured.out
    assert captured.err.strip() == "Interrupted."
    assert "Traceback" not in captured.err


def test_config_overrides_preserve_argv_order_across_the_subcommand(
    tmp_path: Path,
) -> None:
    """Overrides before and after the subcommand must all be retained in argv order.

    Regression: argparse subparsers dispatch into a fresh namespace and copy
    it back, which used to drop any ``--config`` / ``--config-file`` passed
    before the subcommand when the subcommand also carried its own.
    """

    file_a = tmp_path / "a.toml"
    file_a.write_text("", encoding="utf-8")
    cli_args, remaining = _extract_config_overrides(
        [
            "--config-file",
            str(file_a),
            "view",
            "--config",
            "revset-aliases.myhead=@-",
            "--repository",
            ".",
        ]
    )

    assert cli_args.to_argv() == (
        "--config-file",
        str(file_a),
        "--config",
        "revset-aliases.myhead=@-",
    )
    assert remaining == ["view", "--repository", "."]


def test_config_file_paths_resolve_against_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caller_cwd = tmp_path / "work"
    caller_cwd.mkdir()
    config_file = caller_cwd / "jjr.toml"
    config_file.write_text("", encoding="utf-8")
    monkeypatch.chdir(caller_cwd)

    cli_args, _ = _extract_config_overrides(["--config-file", "jjr.toml", "view"])

    assert cli_args.to_argv() == ("--config-file", str(config_file.resolve()))


def test_config_overrides_leave_malformed_flag_for_argparse_to_report() -> None:
    """When the next token is another option, ``--config`` stays in argv so
    argparse raises its usual "expected one argument" error instead of silently
    eating the option as the value.
    """

    cli_args, remaining = _extract_config_overrides(["--config", "--repository", ".", "view"])

    assert cli_args.to_argv() == ()
    assert remaining == ["--config", "--repository", ".", "view"]


def test_config_overrides_stop_at_end_of_options_marker() -> None:
    """Tokens after ``--`` are positional and must not be pulled out as overrides."""

    cli_args, remaining = _extract_config_overrides(["view", "--", "--config", "x=1"])

    assert cli_args.to_argv() == ()
    assert remaining == ["view", "--", "--config", "x=1"]


def test_main_exits_130_when_interrupted_before_the_console_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The startup color read runs `jj`, so Ctrl-C can land before any console exists.

    The report itself goes to the process stderr no test console has replaced yet, so only
    the exit code is observable here.
    """

    def interrupt(**_kwargs) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_module, "_load_configured_jj_color", interrupt)

    assert main(["view"]) == EXIT_INTERRUPTED


def test_submit_edit_never_consumes_the_selected_revset() -> None:
    for argv in (
        ["submit", "--edit", "kwxsqkvomnrr"],
        ["submit", "--edit", "--dry-run", "kwxsqkvomnrr"],
        ["submit", "kwxsqkvomnrr", "--edit"],
    ):
        args = build_parser().parse_args(_normalize_cli_args(argv))
        assert (args.revset, args.edit) == ("kwxsqkvomnrr", True), argv

    saved = build_parser().parse_args(_normalize_cli_args(["submit", "--edit=saved-edit.md"]))

    assert (saved.revset, saved.edit) == (None, Path("saved-edit.md"))
