"""CLI entrypoint for the standalone `jj-review` executable."""

from __future__ import annotations

import builtins
import io
import logging
import re
import shutil
import subprocess
import sys
import textwrap
import time
from argparse import SUPPRESS, ArgumentParser, HelpFormatter, Namespace, _SubParsersAction
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

from jj_review import __version__, commands, console
from jj_review.completion import emit_shell_completion
from jj_review.console import ColorMode, RequestedColorMode, configured_console, rich_color_mode
from jj_review.errors import CliError, error_message

logger = logging.getLogger(__name__)
SubparserT = TypeVar("SubparserT", bound=ArgumentParser)
_COLOR_CHOICES: tuple[RequestedColorMode, ...] = ("always", "never", "debug", "auto")
_TOP_LEVEL_HELP_WIDTH = 80
_TOP_LEVEL_HELP_USAGE = "jj-review [-h] [--color WHEN] [--version] <command> ..."
_TOP_LEVEL_HELP_USAGE_ALL = (
    "jj-review [-h] [--repository REPOSITORY] [--config CONFIG] [--debug] "
    "[--color WHEN] [--time-output] [--version] <command> ..."
)
_TOP_LEVEL_HELP_DESCRIPTION = """
jj-review lets you review a local jj stack on GitHub as stacked pull requests.

Use it to submit changes for review, inspect pull request status, land
ready changes, and clean up stale jj-review data.
"""
_TOP_LEVEL_HIDDEN_OPTION_STRINGS = frozenset(
    {"--repository", "--config", "--debug", "--time-output"}
)
_COMPLETION_HELP = "Print shell completion setup for bash, zsh, or fish"
_HELP_HELP = "Show help for this command or another command"
_COMPLETION_DESCRIPTION = """
Print the shell completion script for bash, zsh, or fish. This only prints
local shell setup text and does not inspect the repository or GitHub.
"""
_HELP_DESCRIPTION = """
Show top-level help or the detailed help for one command. Use `--all` to also
show the advanced repair commands and hidden global options.
"""


@dataclass(frozen=True)
class _HelpCommand:
    name: str
    summary: str
    hidden: bool = False


_TOP_LEVEL_HELP_GROUPS: tuple[tuple[str, tuple[_HelpCommand, ...]], ...] = (
    (
        "Core commands",
        (
            _HelpCommand("submit", commands.submit.HELP),
            _HelpCommand("status", commands.status.HELP),
            _HelpCommand("land", commands.land.HELP),
            _HelpCommand("close", commands.close.HELP),
        ),
    ),
    (
        "Support commands",
        (
            _HelpCommand("cleanup", commands.cleanup.HELP),
            _HelpCommand("import", commands.import_.HELP),
            _HelpCommand("abort", commands.abort.HELP),
            _HelpCommand("doctor", commands.doctor.HELP),
        ),
    ),
    (
        "Advanced repair",
        (
            _HelpCommand("relink", commands.relink.HELP, hidden=True),
            _HelpCommand("unlink", commands.unlink.HELP, hidden=True),
        ),
    ),
    (
        "Configuration",
        (_HelpCommand("completion", _COMPLETION_HELP, hidden=True),),
    ),
    (
        "Help",
        (_HelpCommand("help", _HELP_HELP, hidden=True),),
    ),
)


class _TopLevelArgumentParser(ArgumentParser):
    """ArgumentParser with custom grouped help for the top-level CLI."""

    def format_help(self) -> str:
        return _format_top_level_help(self, include_hidden=False)

    def format_usage(self) -> str:
        return _format_top_level_usage(include_hidden=False) + "\n"


class _TitleCaseHelpFormatter(HelpFormatter):
    """Help formatter that title-cases the usage heading."""

    def add_usage(self, usage, actions, groups, prefix=None):
        return super().add_usage(usage, actions, groups, prefix="Usage: ")

    def _fill_text(self, text: str, width: int, indent: str) -> str:
        return _fill_help_paragraphs(
            text,
            width=width,
            initial_indent=indent,
            subsequent_indent=indent,
        )

    def _split_lines(self, text: str, width: int) -> list[str]:
        rendered = _fill_help_paragraphs(
            text,
            width=width,
            initial_indent="",
            subsequent_indent="",
        )
        return rendered.splitlines()


class _CommandArgumentParser(ArgumentParser):
    """ArgumentParser with title-cased built-in help headings."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("formatter_class", _TitleCaseHelpFormatter)
        super().__init__(*args, **kwargs)
        _normalize_argument_section_titles(self)


def build_parser() -> ArgumentParser:
    """Build the top-level CLI parser and subcommands."""

    parser = _TopLevelArgumentParser(
        prog="jj-review",
        description=_normalized_help_text(_TOP_LEVEL_HELP_DESCRIPTION),
    )
    _add_common_options(parser, suppress_defaults=False)
    parser.set_defaults(handler=None)
    _normalize_help_action_text(parser)
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program's version number and exit",
    )

    subparsers = parser.add_subparsers(
        dest="command",
        parser_class=_CommandArgumentParser,
    )
    submit_parser = _add_revision_command(
        subparsers,
        command="submit",
        help_text=_normalized_help_text(commands.submit.HELP),
        description_text=commands.submit.__doc__ or "",
        handler=lambda args: commands.submit.submit(
            config_path=args.config,
            debug=args.debug,
            describe_with=args.describe_with,
            draft=args.draft,
            draft_all=args.draft_all,
            dry_run=args.dry_run,
            labels=args.labels,
            publish=args.publish,
            repository=args.repository,
            reviewers=args.reviewers,
            revset=args.revset,
            team_reviewers=args.team_reviewers,
        ),
        revset_help="Revision to submit; defaults to @- (the current stack head)",
    )
    submit_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the submit plan without mutating local, remote, or GitHub state",
    )
    submit_parser.add_argument(
        "-d",
        "--describe-with",
        help=_normalized_help_text(
            """
            Executable to invoke as `helper --pr <change_id>` for each PR and
            `helper --stack <revset>` for stack-comment prose; the helper must
            print JSON with string `title` and `body` fields
            """
        ),
    )
    submit_draft_mode = submit_parser.add_mutually_exclusive_group()
    submit_draft_mode.add_argument(
        "--draft",
        action="store_true",
        help=_normalized_help_text(
            """
            Create newly opened pull requests as drafts; use `--draft=all` to
            also return existing published pull requests on the selected stack
            to draft
            """
        ),
    )
    submit_draft_mode.add_argument(
        "--draft-all",
        action="store_true",
        help=SUPPRESS,
    )
    submit_draft_mode.add_argument(
        "--publish",
        action="store_true",
        help="Mark existing draft pull requests ready for review on submit",
    )
    submit_parser.add_argument(
        "--label",
        dest="labels",
        action="append",
        help=_normalized_help_text(
            """
            Comma-separated GitHub labels to apply to submitted pull requests;
            repeat to add more; overrides configured labels
            """
        ),
    )
    submit_parser.add_argument(
        "--reviewers",
        dest="reviewers",
        action="append",
        help=_normalized_help_text(
            """
            Comma-separated GitHub usernames to request on submitted pull
            requests; repeat to add more; overrides configured reviewers
            """
        ),
    )
    submit_parser.add_argument(
        "--team-reviewers",
        dest="team_reviewers",
        action="append",
        help=_normalized_help_text(
            """
            Comma-separated GitHub team slugs to request on submitted pull
            requests; repeat to add more; overrides configured team reviewers
            """
        ),
    )
    status_parser = _add_revision_command(
        subparsers,
        command="status",
        help_text=_normalized_help_text(commands.status.HELP),
        description_text=commands.status.__doc__ or "",
        handler=lambda args: commands.status.status(
            config_path=args.config,
            debug=args.debug,
            fetch=args.fetch,
            repository=args.repository,
            revset=args.revset,
            verbose=args.verbose,
        ),
        revset_help="Revision to inspect; defaults to the current stack",
    )
    status_parser.add_argument(
        "-f",
        "--fetch",
        action="store_true",
        help="Fetch remote bookmark state before inspecting review status",
    )
    status_parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Expand submitted and unsubmitted summary sections",
    )
    _add_relink_parser(
        subparsers,
        command="relink",
        help_text=_normalized_help_text(commands.relink.HELP),
        description_text=commands.relink.__doc__ or "",
        handler=lambda args: commands.relink.relink(
            config_path=args.config,
            debug=args.debug,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
        ),
    )
    _add_revision_command(
        subparsers,
        command="unlink",
        help_text=_normalized_help_text(commands.unlink.HELP),
        description_text=commands.unlink.__doc__ or "",
        handler=lambda args: commands.unlink.unlink(
            config_path=args.config,
            debug=args.debug,
            repository=args.repository,
            revset=args.revset,
        ),
        revset_nargs=None,
        revset_help="Revision to unlink",
    )
    land_parser = _add_revision_command(
        subparsers,
        command="land",
        help_text=_normalized_help_text(commands.land.HELP),
        description_text=commands.land.__doc__ or "",
        handler=lambda args: commands.land.land(
            dry_run=args.dry_run,
            bypass_readiness=args.bypass_readiness,
            config_path=args.config,
            debug=args.debug,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
            skip_cleanup=args.skip_cleanup,
        ),
        revset_help=(
            "Revision to land; defaults to @- (the current stack head); "
            "cannot be combined with --pull-request"
        ),
    )
    land_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the landing plan without mutating jj or GitHub state",
    )
    land_parser.add_argument(
        "--pull-request",
        help="Select the local change linked to this pull request number or URL",
    )
    land_parser.add_argument(
        "--bypass-readiness",
        action="store_true",
        help=("Skip draft and review-decision checks while keeping normal safety checks"),
    )
    land_parser.add_argument(
        "--skip-cleanup",
        action="store_true",
        help="Keep landed local review bookmarks instead of forgetting them",
    )
    close_parser = _add_revision_command(
        subparsers,
        command="close",
        help_text=_normalized_help_text(commands.close.HELP),
        description_text=commands.close.__doc__ or "",
        handler=lambda args: commands.close.close(
            dry_run=args.dry_run,
            cleanup=args.cleanup,
            config_path=args.config,
            debug=args.debug,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
        ),
        revset_help=(
            "Revision to close; defaults to @- (the current stack head); "
            "cannot be combined with --pull-request"
        ),
    )
    close_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the close plan without mutating jj-review or GitHub state",
    )
    close_parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Also delete the review branches and tracking data for the stack",
    )
    close_parser.add_argument(
        "--pull-request",
        help="Select the local change linked to this pull request number or URL",
    )
    _add_import_parser(
        subparsers,
        command="import",
        help_text=_normalized_help_text(commands.import_.HELP),
        description_text=commands.import_.__doc__ or "",
        handler=lambda args: commands.import_.import_(
            config_path=args.config,
            debug=args.debug,
            fetch=args.fetch,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
        ),
    )

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help=_normalized_help_text(commands.cleanup.HELP),
        description=_normalized_help_text(commands.cleanup.__doc__ or ""),
    )
    _add_common_options(cleanup_parser)
    _normalize_help_action_text(cleanup_parser)
    cleanup_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print cleanup actions without mutating jj-review or GitHub state",
    )
    cleanup_parser.add_argument(
        "--restack",
        action="store_true",
        help="Preview or apply a local restack for merged changes on the selected stack",
    )
    cleanup_parser.add_argument(
        "revset",
        nargs="?",
        help=(
            "Revision whose stack should be restacked; ignored unless "
            "`--restack` is passed, and defaults to @- for restack"
        ),
    )
    cleanup_parser.set_defaults(
        handler=lambda args: commands.cleanup.cleanup(
            dry_run=args.dry_run,
            config_path=args.config,
            debug=args.debug,
            repository=args.repository,
            restack=args.restack,
            revset=args.revset,
        )
    )

    abort_parser = subparsers.add_parser(
        "abort",
        help=_normalized_help_text(commands.abort.HELP),
        description=_normalized_help_text(commands.abort.__doc__ or ""),
    )
    _add_common_options(abort_parser)
    _normalize_help_action_text(abort_parser)
    abort_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be undone without changing anything",
    )
    abort_parser.set_defaults(
        handler=lambda args: commands.abort.abort(
            config_path=args.config,
            debug=args.debug,
            dry_run=args.dry_run,
            repository=args.repository,
        )
    )

    doctor_parser = subparsers.add_parser(
        "doctor",
        help=_normalized_help_text(commands.doctor.HELP),
        description=_normalized_help_text(commands.doctor.__doc__ or ""),
    )
    _add_common_options(doctor_parser)
    _normalize_help_action_text(doctor_parser)
    doctor_parser.set_defaults(
        handler=lambda args: commands.doctor.doctor(
            config_path=args.config,
            debug=args.debug,
            repository=args.repository,
        )
    )

    completion_parser = subparsers.add_parser(
        "completion",
        help=_COMPLETION_HELP,
        description=_normalized_help_text(_COMPLETION_DESCRIPTION),
    )
    _normalize_help_action_text(completion_parser)
    completion_parser.add_argument(
        "shell",
        choices=("bash", "zsh", "fish"),
        help="Shell to generate completion support for",
    )
    completion_parser.set_defaults(handler=_completion_handler)
    help_parser = subparsers.add_parser(
        "help",
        help=SUPPRESS,
        description=_normalized_help_text(_HELP_DESCRIPTION),
    )
    _normalize_help_action_text(help_parser)
    help_parser.add_argument(
        "--all",
        action="store_true",
        help="Include advanced repair and shell integration commands",
    )
    help_parser.add_argument(
        "command",
        nargs="?",
        help="Command to describe",
    )
    help_parser.set_defaults(handler=_help_handler)
    return parser


def _format_top_level_help(parser: ArgumentParser, *, include_hidden: bool) -> str:
    width = _top_level_help_width()
    sections: list[str] = [
        _format_top_level_usage(include_hidden=include_hidden, width=width),
        _format_top_level_description(parser.description or "", width=width),
    ]
    for title, entries in _TOP_LEVEL_HELP_GROUPS:
        visible_entries = [entry for entry in entries if include_hidden or not entry.hidden]
        if not visible_entries:
            continue
        sections.append(_format_help_command_section(title, visible_entries, width=width))

    if not include_hidden:
        sections.append(
            textwrap.fill(
                "Run `jj-review help --all` to show advanced commands and options.",
                width=width,
                break_long_words=False,
                break_on_hyphens=False,
            )
        )

    sections.append(
        _format_help_option_section(
            parser,
            include_hidden=include_hidden,
            width=width,
        )
    )
    return "\n\n".join(section for section in sections if section).rstrip() + "\n"


def _format_top_level_usage(*, include_hidden: bool, width: int | None = None) -> str:
    effective_width = _top_level_help_width() if width is None else width
    return textwrap.fill(
        _TOP_LEVEL_HELP_USAGE_ALL if include_hidden else _TOP_LEVEL_HELP_USAGE,
        width=effective_width,
        initial_indent="usage: ",
        subsequent_indent="       ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def _format_top_level_description(description: str, *, width: int | None = None) -> str:
    normalized_description = _normalized_help_text(description)
    if not normalized_description:
        return ""

    effective_width = _top_level_help_width() if width is None else width
    paragraphs = [
        textwrap.fill(
            paragraph,
            width=effective_width,
            break_long_words=False,
            break_on_hyphens=False,
        )
        for paragraph in normalized_description.split("\n\n")
    ]
    return "\n\n".join(paragraphs)


def _format_help_command_section(
    title: str,
    entries: Sequence[_HelpCommand],
    *,
    width: int | None = None,
) -> str:
    effective_width = _top_level_help_width() if width is None else width
    label_width = max(len(entry.name) for entry in entries) + 2
    lines = [f"{title}:"]
    for entry in entries:
        initial_indent = f"  {entry.name.ljust(label_width)}"
        lines.append(
            textwrap.fill(
                _normalized_help_text(entry.summary),
                width=effective_width,
                initial_indent=initial_indent,
                subsequent_indent=" " * len(initial_indent),
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n".join(lines)


def _format_help_option_section(
    parser: ArgumentParser,
    *,
    include_hidden: bool,
    width: int | None = None,
) -> str:
    effective_width = _top_level_help_width() if width is None else width
    actions = [
        action
        for action in parser._actions
        if action.option_strings
        and action.help is not SUPPRESS
        and (
            include_hidden
            or not any(
                option in _TOP_LEVEL_HIDDEN_OPTION_STRINGS for option in action.option_strings
            )
        )
    ]
    label_width = max(len(_format_option_label(action)) for action in actions) + 2
    lines = ["Options:"]
    for action in actions:
        label = _format_option_label(action)
        initial_indent = f"  {label.ljust(label_width)}"
        lines.append(
            textwrap.fill(
                action.help or "",
                width=effective_width,
                initial_indent=initial_indent,
                subsequent_indent=" " * len(initial_indent),
                break_long_words=False,
                break_on_hyphens=False,
            )
        )
    return "\n".join(lines)


def _format_option_label(action) -> str:
    if action.nargs == 0:
        return ", ".join(action.option_strings)
    metavar = action.metavar or action.dest.upper()
    return ", ".join(f"{option} {metavar}" for option in action.option_strings)


def _normalized_help_text(text: str) -> str:
    return textwrap.dedent(text).strip()


def _fill_help_paragraphs(
    text: str,
    *,
    width: int,
    initial_indent: str,
    subsequent_indent: str,
) -> str:
    paragraphs = re.split(r"\n\s*\n", text)
    return "\n\n".join(
        textwrap.fill(
            " ".join(paragraph.split()),
            width=width,
            initial_indent=initial_indent,
            subsequent_indent=subsequent_indent,
            break_long_words=False,
            break_on_hyphens=False,
        )
        for paragraph in paragraphs
    )


def _top_level_help_width() -> int:
    if not any(stream.isatty() for stream in (sys.stdout, sys.stderr)):
        return _TOP_LEVEL_HELP_WIDTH

    columns = shutil.get_terminal_size(fallback=(_TOP_LEVEL_HELP_WIDTH, 24)).columns
    return columns if columns > 0 else _TOP_LEVEL_HELP_WIDTH


def _help_handler(args: Namespace) -> int:
    parser = build_parser()
    if args.command is None:
        print(_format_top_level_help(parser, include_hidden=args.all), end="")
        return 0

    command_parser = _find_subcommand_parser(parser, args.command)
    if command_parser is None:
        raise CliError(f"Unknown command {args.command!r}.")
    print(command_parser.format_help(), end="")
    return 0


def _find_subcommand_parser(
    parser: ArgumentParser,
    command_name: str,
) -> ArgumentParser | None:
    for action in parser._actions:
        if not isinstance(action, _SubParsersAction):
            continue
        parser_choice = action.choices.get(command_name)
        if parser_choice is not None:
            return parser_choice
    return None


def _print_cli_error(error: CliError) -> None:
    message = error_message(error)
    if str(error).startswith("Error:"):
        console.error(message, soft_wrap=True)
    else:
        console.error(("Error: ", message), soft_wrap=True)


def _load_configured_jj_color(*, repository: Path | None) -> RequestedColorMode | None:
    """Read `ui.color` from `jj` config without requiring repository bootstrap."""

    cwd = (
        repository
        if repository is not None and repository.exists() and repository.is_dir()
        else Path.cwd()
    )
    try:
        completed = subprocess.run(
            ["jj", "config", "get", "ui.color"],
            capture_output=True,
            check=False,
            cwd=cwd,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None

    if completed.returncode != 0:
        return None

    configured = completed.stdout.strip()
    if configured in _COLOR_CHOICES:
        return cast(RequestedColorMode, configured)
    return None


def _resolve_rich_color_mode(
    *,
    cli_color: RequestedColorMode | None,
    repository: Path | None,
) -> tuple[RequestedColorMode | None, ColorMode]:
    raw_color = cli_color
    if raw_color is None:
        raw_color = _load_configured_jj_color(repository=repository)
    return raw_color, rich_color_mode(raw_color)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    try:
        normalized_argv = _normalize_cli_args(sys.argv[1:] if argv is None else argv)
    except CliError as error:
        _print_cli_error(error)
        return error.exit_code
    args = parser.parse_args(normalized_argv)
    _, effective_rich_color_mode = _resolve_rich_color_mode(
        cli_color=args.color,
        repository=args.repository,
    )
    with configured_console(
        color_mode=effective_rich_color_mode,
        repository=args.repository,
        requested_color_mode=args.color,
        time_output=args.time_output,
    ):
        with _time_output(enabled=args.time_output):
            handler = args.handler
            if handler is None:
                print(parser.format_help(), end="")
                return 0

            try:
                return handler(args)
            except CliError as error:
                _print_cli_error(error)
                return error.exit_code
            except KeyboardInterrupt:
                print("Interrupted.", file=sys.stderr)
                return 130


def _add_revision_command(
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler,
    revset_nargs: str | int | None = "?",
    revset_help: str = "Revision to operate on",
) -> SubparserT:
    parser = subparsers.add_parser(
        command,
        help=help_text,
        description=_normalized_help_text(description_text),
    )
    _add_common_options(parser)
    _normalize_help_action_text(parser)
    parser.add_argument("revset", nargs=revset_nargs, help=revset_help)
    parser.set_defaults(handler=handler)
    return parser


def _add_relink_parser(
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler,
) -> SubparserT:
    parser = subparsers.add_parser(
        command,
        help=help_text,
        description=_normalized_help_text(description_text),
    )
    _add_common_options(parser)
    _normalize_help_action_text(parser)
    parser.add_argument("pull_request", help="Pull request number or URL")
    parser.add_argument(
        "revset",
        help="Revision to reassociate with the pull request",
    )
    parser.set_defaults(handler=handler)
    return parser


def _add_import_parser(
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler,
) -> SubparserT:
    parser = subparsers.add_parser(
        command,
        help=help_text,
        description=_normalized_help_text(description_text),
    )
    _add_common_options(parser)
    _normalize_help_action_text(parser)
    selector = parser.add_mutually_exclusive_group(required=False)
    selector.add_argument(
        "--pull-request",
        help="Pull request number or URL",
    )
    selector.add_argument(
        "--revset",
        help="Explicit revset whose exact stack should be imported",
    )
    parser.add_argument(
        "--fetch",
        action="store_true",
        help=_normalized_help_text(
            """
            Refresh the selected stack's remote bookmark state and, for
            `--pull-request`, fetch only the branches needed to import
            that stack
            """
        ),
    )
    parser.set_defaults(handler=handler)
    return parser


def _add_common_options(
    parser: ArgumentParser,
    *,
    suppress_defaults: bool = True,
) -> None:
    parser.add_argument(
        "--repository",
        type=Path,
        default=SUPPRESS if suppress_defaults else None,
        help="Workspace path to operate on; defaults to the current directory",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=SUPPRESS if suppress_defaults else None,
        help="Use this jj config file instead of the default jj config scopes",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=SUPPRESS if suppress_defaults else False,
        help="Enable debug logging",
    )
    parser.add_argument(
        "--color",
        choices=_COLOR_CHOICES,
        default=SUPPRESS if suppress_defaults else None,
        metavar="WHEN",
        help="When to colorize output; possible values: always, never, debug, auto",
    )
    parser.add_argument(
        "--time-output",
        action="store_true",
        default=SUPPRESS if suppress_defaults else False,
        help="Prefix each printed line with elapsed seconds since process start",
    )


def _normalize_help_action_text(parser: ArgumentParser) -> None:
    for action in parser._actions:
        if action.option_strings == ["-h", "--help"]:
            action.help = "Show help"
            return


def _normalize_argument_section_titles(parser: ArgumentParser) -> None:
    parser._positionals.title = "Positional Arguments"
    parser._optionals.title = "Options"


def _completion_handler(args: Namespace) -> int:
    print(emit_shell_completion(build_parser(), args.shell), end="")
    return 0


@contextmanager
def _time_output(*, enabled: bool):
    if not enabled:
        yield
        return

    start = time.perf_counter()
    original_print = builtins.print
    at_line_start: dict[int, bool] = {}

    def timed_print(*args, **kwargs) -> None:
        elapsed = time.perf_counter() - start
        destination = kwargs.pop("file", sys.stdout)
        flush = kwargs.pop("flush", False)
        end = kwargs.get("end", "\n")
        buffer = io.StringIO()
        original_print(*args, file=buffer, flush=False, **kwargs)
        rendered = buffer.getvalue()
        if rendered:
            prefix = f"[{elapsed:0.6f}] "
            key = id(destination)
            rendered_output, next_at_line_start = _prefix_rendered_output(
                rendered,
                prefix=prefix,
                at_line_start=at_line_start.get(key, True),
            )
            destination.write(rendered_output)
            at_line_start[key] = next_at_line_start
        elif end:
            key = id(destination)
            rendered_output, next_at_line_start = _prefix_rendered_output(
                end,
                prefix=f"[{elapsed:0.6f}] ",
                at_line_start=at_line_start.get(key, True),
            )
            destination.write(rendered_output)
            at_line_start[key] = next_at_line_start
        if flush:
            destination.flush()

    builtins.print = timed_print  # noqa: B010
    try:
        yield
    finally:
        builtins.print = original_print  # noqa: B010


def _prefix_rendered_output(
    rendered: str,
    *,
    prefix: str,
    at_line_start: bool,
) -> tuple[str, bool]:
    if not rendered:
        return "", at_line_start

    chunks: list[str] = []
    current_at_line_start = at_line_start
    for chunk in rendered.splitlines(keepends=True):
        if current_at_line_start:
            chunks.append(prefix)
        chunks.append(chunk)
        current_at_line_start = chunk.endswith("\n")
    return "".join(chunks), current_at_line_start


def _normalize_cli_args(argv: Sequence[str]) -> list[str]:
    normalized = list(argv)
    for index, arg in enumerate(normalized):
        if not arg.startswith("--draft="):
            continue
        draft_mode = arg.removeprefix("--draft=")
        if draft_mode == "new":
            normalized[index] = "--draft"
            continue
        if draft_mode == "all":
            normalized[index] = "--draft-all"
            continue
        raise CliError(f"Invalid value for `--draft`: {draft_mode!r}. Expected `new` or `all`.")
    return normalized


if __name__ == "__main__":
    raise SystemExit(main())
