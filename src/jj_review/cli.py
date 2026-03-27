"""CLI entrypoint for the standalone `jj-review` executable."""

from __future__ import annotations

import builtins
import io
import logging
import shutil
import sys
import textwrap
import time
from argparse import SUPPRESS, ArgumentParser, HelpFormatter, Namespace, _SubParsersAction
from collections.abc import Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from jj_review import __version__
from jj_review.bootstrap import BootstrapError, bootstrap_context
from jj_review.commands.cleanup import (
    prepare_cleanup,
    prepare_restack,
    stream_cleanup,
    stream_restack,
)
from jj_review.commands.close import CloseResult, run_close
from jj_review.commands.import_ import run_import
from jj_review.commands.land import LandResult, run_land
from jj_review.commands.relink import run_relink
from jj_review.commands.review_state import prepare_status, stream_status
from jj_review.commands.submit import run_submit
from jj_review.commands.unlink import run_unlink
from jj_review.completion import emit_shell_completion
from jj_review.errors import CliError, CommandNotImplementedError
from jj_review.intent import intent_change_ids, pid_is_alive
from jj_review.jj import UnsupportedStackError

logger = logging.getLogger(__name__)
_DISPLAY_CHANGE_ID_LENGTH = 8
_TOP_LEVEL_HELP_WIDTH = 80
_TOP_LEVEL_HELP_USAGE = "jj-review [-h] [--version] <command> ..."
_TOP_LEVEL_HELP_USAGE_ALL = (
    "jj-review [-h] [--repository REPOSITORY] [--config CONFIG] [--debug] "
    "[--time-output] [--version] <command> ..."
)
_TOP_LEVEL_HELP_DESCRIPTION = """
jj-review lets you review a local jj stack on GitHub as stacked pull requests.

Use it to submit changes for review, inspect pull request status, land
reviewed changes, and clean up stale review state.
"""
_TOP_LEVEL_HIDDEN_OPTION_STRINGS = frozenset(
    {"--repository", "--config", "--debug", "--time-output"}
)

_SUBMIT_HELP = "Send a jj stack to GitHub for review"
_STATUS_HELP = "Check the review status of a jj stack"
_LAND_HELP = "Land the merge-ready part of a stack"
_CLOSE_HELP = "Stop reviewing a jj stack on GitHub"
_CLEANUP_HELP = "Clean up stale review state for a jj stack"
_IMPORT_HELP = "Import an existing review stack into local jj-review state"
_RELINK_HELP = "Reconnect an existing pull request to a local change"
_UNLINK_HELP = "Stop managing one local change as part of review"
_COMPLETION_HELP = "Print shell completion setup for bash, zsh, or fish"
_HELP_HELP = "Show help for this command or another command"
_SUBMIT_DESCRIPTION = """
Create or update the GitHub pull requests for the selected stack of changes.
This pushes or updates the GitHub branches for that stack, then opens or
refreshes one pull request per change from bottom to top.
"""
_STATUS_DESCRIPTION = """
Show how the selected jj stack currently appears on GitHub. This reports the
pull requests and GitHub branches jj-review is using for each change without
changing anything.
"""
_LAND_DESCRIPTION = """
Land the consecutive changes above `trunk()` whose pull requests are still
open. Landing moves those changes onto `trunk()`, pushes the new trunk tip to
the remote trunk branch, and closes their pull requests.

Without `--apply`, this command only shows what would be landed. With `--apply`,
it performs the landing.

If later changes remain above that point, run `cleanup --restack` and then
`submit` to keep those remaining changes under review.
"""
_CLOSE_DESCRIPTION = """
Close the GitHub pull requests for the selected stack. Without `--apply`, this
command shows what would be closed. With `--apply`, it closes those pull
requests, and `--cleanup` also removes jj-review's GitHub branches and any local
bookmarks for them.
"""
_CLEANUP_DESCRIPTION = """
Find stale jj-review branches and local records left behind by earlier review
work. With `--apply`, this removes the safe ones, and with `--restack` it can
also restack local descendants after earlier pull requests were merged.
"""
_IMPORT_DESCRIPTION = """
Import one existing reviewed stack into local jj-review state. Without
`--fetch`, this uses the selected stack only if its commits and review link
are already available locally. With `--fetch`, it fetches the selected pull
request or review branch first so the stack can be imported into a repo that
does not have it yet. Import does not rewrite commits, restack changes, or
change GitHub.
"""
_RELINK_DESCRIPTION = """
Reconnect an existing GitHub pull request to the selected local change. Use
this to repair a missing or wrong local link between a change and its pull
request.
"""
_UNLINK_DESCRIPTION = """
Stop tracking one local change with jj-review while leaving the rest of the
stack alone. Later jj-review commands will ignore that change unless you link
it again.
"""
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
            _HelpCommand("submit", _SUBMIT_HELP),
            _HelpCommand("status", _STATUS_HELP),
            _HelpCommand("land", _LAND_HELP),
            _HelpCommand("close", _CLOSE_HELP),
        ),
    ),
    (
        "Support commands",
        (
            _HelpCommand("cleanup", _CLEANUP_HELP),
            _HelpCommand("import", _IMPORT_HELP),
        ),
    ),
    (
        "Advanced repair",
        (
            _HelpCommand("relink", _RELINK_HELP, hidden=True),
            _HelpCommand("unlink", _UNLINK_HELP, hidden=True),
        ),
    ),
    (
        "Configuration",
        (_HelpCommand("completion", _COMPLETION_HELP, hidden=True),),
    ),
    (
        "Help",
        (_HelpCommand("help", _HELP_HELP),),
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
    _add_common_options(parser)
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
        help_text=_SUBMIT_HELP,
        description_text=_SUBMIT_DESCRIPTION,
        handler=_submit_handler,
    )
    submit_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the submit plan without mutating local, remote, or GitHub state",
    )
    submit_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    submit_parser.add_argument(
        "-d",
        "--describe-with",
        help=_normalized_help_text(
            """
            Executable to invoke as `helper --pr <revset>` for each PR and
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
        help_text=_STATUS_HELP,
        description_text=_STATUS_DESCRIPTION,
        handler=_status_handler,
    )
    status_parser.add_argument(
        "-f",
        "--fetch",
        action="store_true",
        help="Fetch remote bookmark state before inspecting review status",
    )
    _add_relink_parser(
        subparsers,
        command="relink",
        help_text=_RELINK_HELP,
        description_text=_RELINK_DESCRIPTION,
    )
    unlink_parser = _add_revision_command(
        subparsers,
        command="unlink",
        help_text=_UNLINK_HELP,
        description_text=_UNLINK_DESCRIPTION,
        handler=_unlink_handler,
    )
    unlink_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    land_parser = _add_revision_command(
        subparsers,
        command="land",
        help_text=_LAND_HELP,
        description_text=_LAND_DESCRIPTION,
        handler=_land_handler,
    )
    land_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the landing plan instead of only previewing it",
    )
    land_parser.add_argument(
        "--expect-pr",
        help="Assert that the selected landable prefix ends at this pull request",
    )
    land_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    close_parser = _add_revision_command(
        subparsers,
        command="close",
        help_text=_CLOSE_HELP,
        description_text=_CLOSE_DESCRIPTION,
        handler=_close_handler,
    )
    close_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the close plan instead of only previewing it",
    )
    close_parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Also clean up owned review branches and managed metadata",
    )
    close_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    _add_import_parser(
        subparsers,
        command="import",
        help_text=_IMPORT_HELP,
        description_text=_IMPORT_DESCRIPTION,
    )

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help=_CLEANUP_HELP,
        description=_normalized_help_text(_CLEANUP_DESCRIPTION),
    )
    _add_common_options(cleanup_parser)
    _normalize_help_action_text(cleanup_parser)
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply safe cleanup actions instead of only reporting them",
    )
    cleanup_parser.add_argument(
        "--restack",
        action="store_true",
        help="Preview or apply a local restack for merged changes on the selected stack",
    )
    cleanup_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    cleanup_parser.add_argument(
        "revset",
        nargs="?",
        help="Revision whose stack should be inspected or restacked",
    )
    cleanup_parser.set_defaults(handler=_cleanup_handler)

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
        help=_HELP_HELP,
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
                "Run `jj-review help --all` to show advanced commands and "
                "options.",
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
                entry.summary,
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


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    try:
        normalized_argv = _normalize_cli_args(sys.argv[1:] if argv is None else argv)
    except CliError as error:
        print(error, file=sys.stderr)
        return error.exit_code
    args = parser.parse_args(normalized_argv)
    with _time_output(enabled=getattr(args, "time_output", False)):
        handler = getattr(args, "handler", None)
        if handler is None:
            print(parser.format_help(), end="")
            return 0

        try:
            return handler(args)
        except (BootstrapError, CliError) as error:
            print(error, file=sys.stderr)
            return error.exit_code
        except KeyboardInterrupt:
            print("Interrupted.", file=sys.stderr)
            return 130


def _add_revision_command[SubparserT: ArgumentParser](
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    help_text: str,
    description_text: str,
    handler=None,
) -> SubparserT:
    parser = subparsers.add_parser(
        command,
        help=help_text,
        description=_normalized_help_text(description_text),
    )
    _add_common_options(parser)
    _normalize_help_action_text(parser)
    parser.add_argument("revset", nargs="?", help="Revision to operate on")
    parser.set_defaults(handler=handler or _stub_handler(command))
    return parser


def _add_relink_parser[SubparserT: ArgumentParser](
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    help_text: str,
    description_text: str,
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
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    parser.add_argument(
        "revset",
        nargs="?",
        help="Revision to reassociate with the pull request",
    )
    parser.set_defaults(handler=_relink_handler)
    return parser


def _add_import_parser[SubparserT: ArgumentParser](
    subparsers: _SubParsersAction[SubparserT],
    *,
    command: str,
    help_text: str,
    description_text: str,
) -> SubparserT:
    parser = subparsers.add_parser(
        command,
        help=help_text,
        description=_normalized_help_text(description_text),
    )
    _add_common_options(parser)
    _normalize_help_action_text(parser)
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--pull-request",
        help="Pull request number or URL",
    )
    selector.add_argument(
        "--head",
        help="Review branch name to import",
    )
    selector.add_argument(
        "--current",
        action="store_true",
        help="Import the current stack",
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
            `--pull-request` or `--head`, fetch only the review branches needed
            to import that stack
            """
        ),
    )
    parser.set_defaults(handler=_import_handler)
    return parser


def _add_common_options(parser: ArgumentParser) -> None:
    parser.add_argument(
        "--repository",
        type=Path,
        default=SUPPRESS,
        help="Workspace path to operate on; defaults to the current directory",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=SUPPRESS,
        help="Use this config file",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=SUPPRESS,
        help="Enable debug logging",
    )
    parser.add_argument(
        "--time-output",
        action="store_true",
        default=SUPPRESS,
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


def _status_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    try:
        prepared_status = prepare_status(
            change_overrides=context.config.change,
            config=context.config.repo,
            fetch_remote_state=args.fetch,
            repo_root=context.repo_root,
            revset=args.revset,
        )
    except UnsupportedStackError as error:
        raise CliError(_describe_status_preparation_error(error)) from error
    prepared = prepared_status.prepared
    print(f"Selected revset: {prepared_status.selected_revset}")
    if prepared.remote is None:
        if prepared.remote_error is None:
            print("Selected remote: unavailable")
        else:
            print(f"Selected remote: unavailable ({prepared.remote_error})")
    else:
        print(f"Selected remote: {prepared.remote.name}")

    stack_started = False

    def emit_github_status(github_repository: str | None, github_error: str | None) -> None:
        nonlocal stack_started
        if github_repository is None:
            if github_error is not None:
                print(f"GitHub target: unavailable ({github_error})")
        else:
            if github_error is None:
                print(f"GitHub: {github_repository}")
            else:
                print(f"GitHub target: {github_repository} ({github_error})")
        if prepared.status_revisions:
            print("Stack:")
            stack_started = True

    def emit_revision(revision, github_available: bool) -> None:
        summary = _format_status_summary(
            revision,
            github_available=github_available,
        )
        print(f"- {revision.subject} [{_display_change_id(revision.change_id)}]: {summary}")

    result = stream_status(
        on_github_status=emit_github_status,
        on_revision=emit_revision,
        prepared_status=prepared_status,
    )
    if not prepared.status_revisions:
        print(
            _format_trunk_status_row(
                prepared,
                configured_trunk_branch=context.config.repo.trunk_branch,
            )
        )
        print("No reviewable commits between the selected revision and `trunk()`.")
        return 0
    if not stack_started:
        print("Stack:")
    print(
        _format_trunk_status_row(
            prepared,
            configured_trunk_branch=context.config.repo.trunk_branch,
        )
    )
    _emit_status_advisories(result)

    # Render intent file notices
    _stale_intents = prepared_status.stale_intents
    _outstanding_intents = prepared_status.outstanding_intents

    if _stale_intents:
        print()
        print("Stale incomplete operations (change IDs no longer in repo):")
        for loaded in _stale_intents:
            alive = pid_is_alive(loaded.intent.pid)
            status_str = "process alive" if alive else "process dead"
            print(f"  {loaded.intent.label}  [{status_str}, {loaded.path.name}]")

    if _outstanding_intents:
        print()
        print("Incomplete operations detected:")
        for loaded in _outstanding_intents:
            alive = pid_is_alive(loaded.intent.pid)
            if alive:
                print(f"  {loaded.intent.label}  [in progress, PID {loaded.intent.pid}]")
            else:
                print(f"  {loaded.intent.label}  [interrupted — re-run to complete]")

    exit_code = 1 if result.incomplete else 0

    # Outstanding intents overlapping selected changes increase exit code
    selected_change_ids = {r.change_id for r in getattr(result, "revisions", ())}
    overlapping = any(
        intent_change_ids(loaded.intent) & selected_change_ids
        for loaded in _outstanding_intents
    )
    if overlapping:
        exit_code = max(exit_code, 1)

    return exit_code


def _describe_status_preparation_error(error: UnsupportedStackError) -> str:
    if error.reason == "divergent_change" and error.change_id is not None:
        return (
            "Could not inspect review status because local history no longer forms a "
            f"supported linear stack. {error} Inspect the divergent revisions with "
            f"`jj log -r 'change_id({error.change_id})'` and reconcile them before "
            "retrying. "
            "This can happen after `status --fetch` or another fetch imports remote "
            "bookmark updates for merged PRs."
        )
    return (
        "Could not inspect review status because local history no longer forms a "
        f"supported linear stack. {error}"
    )


def _format_trunk_status_row(
    prepared,
    *,
    configured_trunk_branch: str | None,
) -> str:
    trunk = prepared.stack.trunk
    trunk_name = _resolve_status_trunk_name(
        prepared,
        configured_trunk_branch=configured_trunk_branch,
    )
    suffix = "trunk()" if trunk_name is None else trunk_name
    return f"◆ {trunk.subject} [{_display_change_id(trunk.change_id)}]: {suffix}"


def _display_change_id(change_id: str) -> str:
    return change_id[:_DISPLAY_CHANGE_ID_LENGTH]


def _resolve_status_trunk_name(
    prepared,
    *,
    configured_trunk_branch: str | None,
) -> str | None:
    if configured_trunk_branch:
        return configured_trunk_branch

    trunk_commit_id = prepared.stack.trunk.commit_id
    bookmark_states = prepared.client.list_bookmark_states()
    local_matches = tuple(
        sorted(
            name
            for name, bookmark_state in bookmark_states.items()
            if bookmark_state.local_target == trunk_commit_id
        )
    )
    if len(local_matches) == 1:
        return local_matches[0]

    remote = prepared.remote
    if remote is None:
        return None

    remote_matches = tuple(
        sorted(
            name
            for name, bookmark_state in bookmark_states.items()
            if (remote_state := bookmark_state.remote_target(remote.name)) is not None
            and remote_state.target == trunk_commit_id
        )
    )
    if len(remote_matches) == 1:
        return remote_matches[0]
    return None


def _format_status_summary(revision, *, github_available: bool) -> str:
    lookup = revision.pull_request_lookup
    cached_change = revision.cached_change
    cached_label = _format_cached_pull_request_label(cached_change)
    summary: str
    if getattr(revision, "link_state", "active") == "unlinked":
        if lookup is not None and lookup.pull_request is not None:
            pull_request = lookup.pull_request
            if pull_request.state == "open":
                summary = _format_pull_request_label(
                    pull_request.number,
                    is_draft=getattr(pull_request, "is_draft", False),
                    prefix="unlinked ",
                )
            else:
                summary = f"unlinked PR #{pull_request.number} {pull_request.state}"
        elif revision.remote_state is not None and revision.remote_state.targets:
            summary = "unlinked review branch"
        else:
            summary = "unlinked"
    elif lookup is None:
        if github_available:
            summary = "not submitted"
        elif cached_label is not None:
            summary = cached_label
        else:
            summary = "GitHub status unknown"
    elif lookup.state == "open":
        if lookup.pull_request is None:
            raise AssertionError("Open pull request lookup must include a pull request.")
        summary = _format_pull_request_label(
            lookup.pull_request.number,
            is_draft=getattr(lookup.pull_request, "is_draft", False),
        )
        review_decision = _effective_review_decision(
            cached_change=cached_change,
            lookup=lookup,
        )
        if getattr(lookup.pull_request, "is_draft", False):
            pass
        elif review_decision == "approved":
            summary = f"{summary} approved"
        elif review_decision == "changes_requested":
            summary = f"{summary} changes requested"
    elif lookup.state == "missing":
        if cached_label is not None:
            summary = f"{cached_label}, no GitHub PR"
        else:
            summary = "not submitted"
    elif lookup.state == "closed":
        if lookup.pull_request is None:
            raise AssertionError("Closed pull request lookup must include a pull request.")
        if lookup.pull_request.state == "merged":
            summary = f"PR #{lookup.pull_request.number} merged, cleanup needed"
        else:
            summary = f"PR #{lookup.pull_request.number} closed"
    else:
        message = lookup.message or "GitHub lookup failed"
        if cached_label is not None:
            summary = f"{cached_label}, {message}"
        else:
            summary = message

    if getattr(revision, "local_divergent", False) and not _revision_has_merged_pull_request(
        revision
    ):
        summary = f"{summary}, multiple visible revisions"

    stack_comment_lookup = revision.stack_comment_lookup
    if stack_comment_lookup is not None and stack_comment_lookup.state in {
        "ambiguous",
        "error",
    }:
        message = stack_comment_lookup.message or "stack comment lookup failed"
        return f"{summary}, {message}"
    return summary


def _format_cached_pull_request_label(cached_change) -> str | None:
    if cached_change is None or cached_change.pr_number is None:
        return None

    label = _format_pull_request_label(
        cached_change.pr_number,
        is_draft=bool(cached_change.pr_is_draft) and cached_change.pr_state == "open",
        prefix="cached ",
    )
    if cached_change.pr_state is None:
        return label

    details = [cached_change.pr_state]
    if (
        cached_change.pr_state == "open"
        and not cached_change.pr_is_draft
        and cached_change.pr_review_decision is not None
    ):
        details.append(_format_review_decision_label(cached_change.pr_review_decision))
    return f"{label} ({', '.join(details)})"


def _format_pull_request_label(
    pull_request_number: int,
    *,
    is_draft: bool,
    prefix: str = "",
) -> str:
    label = f"PR #{pull_request_number}"
    if is_draft:
        label = f"draft {label}"
    return f"{prefix}{label}"


def _effective_review_decision(*, cached_change, lookup) -> str | None:
    review_decision = getattr(lookup, "review_decision", None)
    if review_decision is not None:
        return review_decision
    if getattr(lookup, "review_decision_error", None) is None or cached_change is None:
        return None
    return cached_change.pr_review_decision


def _format_review_decision_label(review_decision: str) -> str:
    if review_decision == "changes_requested":
        return "changes requested"
    return review_decision


def _emit_status_advisories(result) -> None:
    if not hasattr(result, "revisions") or not hasattr(result, "selected_revset"):
        return
    cleanup_revisions = [
        revision for revision in result.revisions if _revision_has_merged_pull_request(revision)
    ]
    divergent_revisions = [
        revision
        for revision in result.revisions
        if getattr(revision, "local_divergent", False)
        and not _revision_has_merged_pull_request(revision)
    ]
    link_revisions = [
        revision
        for revision in result.revisions
        if _revision_has_link_advisory(revision)
    ]
    policy_warnings = [
        revision
        for revision in cleanup_revisions
        if (
            revision.pull_request_lookup is not None
            and revision.pull_request_lookup.pull_request is not None
            and revision.pull_request_lookup.pull_request.base.ref.startswith("review/")
        )
    ]
    if (
        not cleanup_revisions
        and not divergent_revisions
        and not link_revisions
        and not policy_warnings
    ):
        return

    print()
    print("Advisories:")
    if cleanup_revisions:
        next_command = f"jj-review cleanup --restack {result.selected_revset}"
        _print_wrapped_advisory(
            "Submit note: descendant PR bases still follow the old local ancestry "
            "until the stack is restacked"
        )
        _print_wrapped_advisory(
            f"Next step: run `{next_command}` to preview the local restack plan, "
            "then rerun it with `--apply`"
        )
        for revision in cleanup_revisions:
            pull_request_number = _revision_pull_request_number(revision)
            pull_request_label = (
                f"PR #{pull_request_number}" if pull_request_number is not None else "merged PR"
            )
            _print_wrapped_advisory(
                f"{_status_revision_label(revision)}: {pull_request_label} is merged, "
                "and later local changes are still based on it"
            )
    if link_revisions:
        next_status = f"jj-review status --fetch {result.selected_revset}"
        next_relink = f"jj-review relink <pr> {result.selected_revset}"
        _print_wrapped_advisory(
            f"PR link note: refresh remote and GitHub observations with "
            f"`{next_status}`. If the existing PR should stay attached to one of these "
            f"changes, repair that PR link intentionally with `{next_relink}`."
        )
        for revision in link_revisions:
            _print_wrapped_advisory(
                f"{_status_revision_label(revision)}: {_describe_link_advisory(revision)}"
            )
    for revision in policy_warnings:
        base_ref = revision.pull_request_lookup.pull_request.base.ref
        pull_request_number = _revision_pull_request_number(revision)
        _print_wrapped_advisory(
            f"Repository policy warning: PR #{pull_request_number} merged into "
            f"{base_ref}; configure GitHub to block merges of PRs targeting "
            "`review/*`"
        )
    for revision in divergent_revisions:
        _print_wrapped_advisory(
            f"{_status_revision_label(revision)}: resolve the multiple visible "
            "revisions for this change before retrying "
            f"(`jj log -r 'change_id({revision.change_id})'`)"
        )


def _print_wrapped_advisory(message: str) -> None:
    print(
        textwrap.fill(
            message,
            width=80,
            initial_indent="- ",
            subsequent_indent="  ",
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def _status_revision_label(revision) -> str:
    return f"[{_display_change_id(revision.change_id)}]"


def _revision_has_merged_pull_request(revision) -> bool:
    lookup = revision.pull_request_lookup
    return (
        lookup is not None
        and lookup.state == "closed"
        and lookup.pull_request is not None
        and lookup.pull_request.state == "merged"
    )


def _revision_pull_request_number(revision) -> int | None:
    lookup = revision.pull_request_lookup
    if lookup is None or lookup.pull_request is None:
        return None
    return lookup.pull_request.number


def _revision_has_link_advisory(revision) -> bool:
    if getattr(revision, "link_state", "active") == "unlinked":
        return False
    lookup = revision.pull_request_lookup
    if lookup is None:
        return False
    if lookup.state == "ambiguous":
        return True
    if lookup.state == "missing":
        cached_change = revision.cached_change
        return cached_change is not None and (
            cached_change.pr_number is not None or cached_change.pr_url is not None
        )
    if lookup.state == "closed":
        pull_request = lookup.pull_request
        return pull_request is not None and pull_request.state != "merged"
    return False


def _describe_link_advisory(revision) -> str:
    lookup = revision.pull_request_lookup
    if lookup is None:
        raise AssertionError("Link advisory requires a pull request lookup.")
    if lookup.state == "ambiguous":
        return lookup.message or "GitHub reports an ambiguous pull request link"
    if lookup.state == "missing":
        cached_label = _format_cached_pull_request_label(revision.cached_change)
        if cached_label is None:
            return "GitHub no longer reports a pull request for this review branch"
        return f"{cached_label} is no longer present on GitHub for this review branch"
    if lookup.state == "closed":
        pull_request = lookup.pull_request
        if pull_request is None:
            raise AssertionError("Closed pull request advisory requires a pull request.")
        return (
            f"PR #{pull_request.number} is {pull_request.state}; submit will not reuse a "
            "closed review automatically"
        )
    raise AssertionError(f"Unexpected link advisory state: {lookup.state}")


def _resolve_selected_revset(
    args: Namespace,
    *,
    command_label: str,
    require_explicit: bool,
) -> str | None:
    revset = getattr(args, "revset", None)
    current = bool(getattr(args, "current", False))
    if current and revset is not None:
        raise CliError(
            f"`{command_label}` accepts either `<revset>` or `--current`, not both."
        )
    if current:
        return None
    if revset is not None:
        return cast(str, revset)
    if require_explicit:
        raise CliError(
            f"`{command_label}` requires an explicit revision selection; "
            "pass `<revset>` or `--current`."
        )
    return None


def _submit_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    selected_revset = _resolve_selected_revset(
        args,
        command_label="submit",
        require_explicit=True,
    )
    reviewers = _parse_comma_separated_flag_values(getattr(args, "reviewers", None))
    team_reviewers = _parse_comma_separated_flag_values(
        getattr(args, "team_reviewers", None)
    )
    emitted_prepared = False
    emitted_section_header = False
    emitted_trunk = False

    def emit_prepared(selected_revset: str, remote, has_revisions: bool) -> None:
        del has_revisions
        nonlocal emitted_prepared
        print(f"Selected revset: {selected_revset}")
        print(f"Selected remote: {remote.name}")
        emitted_prepared = True

    def emit_trunk(trunk_subject: str, trunk_branch: str, has_revisions: bool) -> None:
        nonlocal emitted_section_header, emitted_trunk
        print(f"Trunk: {trunk_subject} -> {trunk_branch}")
        emitted_trunk = True
        if not has_revisions:
            return
        if args.dry_run:
            print("Dry run: no local, remote, or GitHub changes applied.")
            print("Planned review bookmarks:")
        else:
            print("Submitted review bookmarks:")
        emitted_section_header = True

    result = run_submit(
        change_overrides=context.config.change,
        config=context.config.repo,
        describe_with=getattr(args, "describe_with", None),
        draft_mode=_submit_draft_mode(args),
        dry_run=bool(args.dry_run),
        on_prepared=emit_prepared,
        on_trunk_resolved=emit_trunk,
        repo_root=context.repo_root,
        revset=selected_revset,
        reviewers=reviewers,
        team_reviewers=team_reviewers,
    )
    if not emitted_prepared:
        print(f"Selected revset: {result.selected_revset}")
        print(f"Selected remote: {result.remote.name}")
    if not emitted_trunk:
        print(f"Trunk: {result.trunk_subject} -> {result.trunk_branch}")
    if not result.revisions:
        print("No reviewable commits between the selected revision and `trunk()`.")
        return 0

    if not emitted_section_header:
        if result.dry_run:
            print("Dry run: no local, remote, or GitHub changes applied.")
            print("Planned review bookmarks:")
        else:
            print("Submitted review bookmarks:")
    for revision in result.revisions:
        _print_submit_revision(revision)
    if not result.dry_run:
        top_pull_request_url = result.revisions[-1].pull_request_url
        if top_pull_request_url is not None:
            print(f"Top of stack: {top_pull_request_url}")
    return 0


def _print_submit_revision(revision) -> None:
    print(f"- {revision.subject} [{_display_change_id(revision.change_id)}]")
    print(f"  -> {revision.bookmark}{_render_submit_revision_suffix(revision)}")


def _submit_draft_mode(args: Namespace) -> Literal["default", "draft", "draft_all", "publish"]:
    if getattr(args, "draft_all", False):
        return "draft_all"
    if getattr(args, "draft", False):
        return "draft"
    if getattr(args, "publish", False):
        return "publish"
    return "default"


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
        raise CliError(
            f"Invalid value for `--draft`: {draft_mode!r}. Expected `new` or `all`."
        )
    return normalized


def _parse_comma_separated_flag_values(values: Sequence[str] | None) -> list[str] | None:
    if values is None:
        return None

    parsed_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in value.split(","):
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            parsed_values.append(normalized)
    return parsed_values


def _render_submit_revision_suffix(revision) -> str:
    pr_suffix = _render_submit_pr_suffix(
        action=revision.pull_request_action,
        is_draft=getattr(revision, "pull_request_is_draft", None),
        pull_request_number=revision.pull_request_number,
    )
    if revision.pull_request_action == "created":
        return pr_suffix
    return _render_submit_remote_suffix(revision.remote_action) + pr_suffix


def _render_submit_remote_suffix(remote_action: str) -> str:
    if remote_action == "up to date":
        return " [already pushed]"
    return " [pushed]"


def _render_submit_pr_suffix(
    *,
    action: str,
    is_draft: bool | None,
    pull_request_number: int | None,
) -> str:
    if pull_request_number is None:
        if action == "created":
            return " [new PR]"
        if action == "updated":
            return " [PR #n updated]"
        return " [PR unchanged]"
    label = _format_pull_request_label(
        pull_request_number,
        is_draft=bool(is_draft),
    )
    if action == "created":
        return f" [{label}]"
    return f" [{label} {action}]"


def _relink_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    selected_revset = _resolve_selected_revset(
        args,
        command_label="relink",
        require_explicit=True,
    )
    result = run_relink(
        config=context.config.repo,
        pull_request_reference=args.pull_request,
        repo_root=context.repo_root,
        revset=selected_revset,
    )
    print(f"Selected revset: {result.selected_revset}")
    print(f"Selected remote: {result.remote_name}")
    print(f"GitHub: {result.github_repository}")
    print(
        f"Relinked PR #{result.pull_request_number} for {result.subject} "
        f"[{_display_change_id(result.change_id)}] -> {result.bookmark}"
    )
    return 0


def _unlink_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    selected_revset = _resolve_selected_revset(
        args,
        command_label="unlink",
        require_explicit=True,
    )
    result = run_unlink(
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=selected_revset,
    )
    print(f"Selected revset: {result.selected_revset}")
    if result.already_unlinked:
        print(
            f"{result.subject} [{_display_change_id(result.change_id)}] is already unlinked "
            "from review tracking."
        )
        return 0
    if result.bookmark is None:
        print(
            f"Stopped review tracking for {result.subject} "
            f"[{_display_change_id(result.change_id)}]."
        )
    else:
        print(
            f"Stopped review tracking for {result.subject} "
            f"[{_display_change_id(result.change_id)}], preserving {result.bookmark}."
        )
    return 0


def _cleanup_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    if args.restack:
        selected_revset = _resolve_selected_revset(
            args,
            command_label="cleanup --restack --apply" if args.apply else "cleanup --restack",
            require_explicit=bool(args.apply),
        )
        try:
            prepared_restack = prepare_restack(
                apply=bool(args.apply),
                change_overrides=context.config.change,
                config=context.config.repo,
                repo_root=context.repo_root,
                revset=selected_revset,
            )
        except UnsupportedStackError as error:
            raise CliError(_describe_status_preparation_error(error)) from error
        prepared_status = prepared_restack.prepared_status
        prepared = prepared_status.prepared
        print(f"Selected revset: {prepared_status.selected_revset}")
        if prepared.remote is None:
            if prepared.remote_error is None:
                print("Selected remote: unavailable")
            else:
                print(f"Selected remote: unavailable ({prepared.remote_error})")
        else:
            print(f"Selected remote: {prepared.remote.name}")
        if prepared_status.github_repository is None:
            if prepared_status.github_repository_error is not None:
                print(
                    "GitHub target: unavailable "
                    f"({prepared_status.github_repository_error})"
                )
        else:
            print(f"GitHub: {prepared_status.github_repository.full_name}")

        header_printed = False

        def emit_action(action) -> None:
            nonlocal header_printed
            if not header_printed:
                header = (
                    "Applied restack actions:"
                    if prepared_restack.apply
                    else "Planned restack actions:"
                )
                print(header)
                header_printed = True
            print(f"- [{action.status}] {action.kind}: {action.message}")

        result = stream_restack(
            on_action=emit_action,
            prepared_restack=prepared_restack,
        )
        if not result.actions:
            print("No merged changes on the selected stack need restacking.")
            return 0
        if not result.applied:
            print(
                "Re-run with `cleanup --restack --apply"
                f"{' ' + result.selected_revset if result.selected_revset else ''}` "
                "to rewrite surviving local changes."
            )
        return 1 if result.blocked else 0

    prepared_cleanup = prepare_cleanup(
        apply=bool(args.apply),
        config=context.config.repo,
        repo_root=context.repo_root,
    )
    if prepared_cleanup.remote is None:
        if prepared_cleanup.remote_error is None:
            print("Selected remote: unavailable")
        else:
            print(f"Selected remote: unavailable ({prepared_cleanup.remote_error})")
    else:
        print(f"Selected remote: {prepared_cleanup.remote.name}")

    if prepared_cleanup.github_repository is None:
        if prepared_cleanup.github_repository_error is not None:
            print(
                "GitHub target: unavailable "
                f"({prepared_cleanup.github_repository_error})"
            )
    else:
        print(f"GitHub: {prepared_cleanup.github_repository.full_name}")

    header_printed = False

    def emit_action(action) -> None:
        nonlocal header_printed
        if not header_printed:
            header = (
                "Applied cleanup actions:"
                if prepared_cleanup.apply
                else "Planned cleanup actions:"
            )
            print(header)
            header_printed = True
        print(f"- [{action.status}] {action.kind}: {action.message}")

    result = stream_cleanup(
        on_action=emit_action,
        prepared_cleanup=prepared_cleanup,
    )
    if not result.actions:
        print("No cleanup actions planned.")
        return 0
    if not result.applied:
        print("Re-run with `cleanup --apply` to perform safe actions.")
    return 0


def _close_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    selected_revset = _resolve_selected_revset(
        args,
        command_label="close --cleanup --apply" if args.apply and args.cleanup else (
            "close --cleanup" if args.cleanup else "close --apply" if args.apply else "close"
        ),
        require_explicit=True,
    )
    result = run_close(
        apply=bool(args.apply),
        cleanup=bool(args.cleanup),
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=selected_revset,
    )
    print(f"Selected revset: {result.selected_revset}")
    if result.remote is None:
        if result.remote_error is None:
            print("Selected remote: unavailable")
        else:
            print(f"Selected remote: unavailable ({result.remote_error})")
    else:
        print(f"Selected remote: {result.remote.name}")

    if result.github_repository is None:
        if result.github_error is not None:
            print(f"GitHub target: unavailable ({result.github_error})")
    else:
        print(f"GitHub: {result.github_repository}")

    if result.actions:
        if result.blocked:
            header = "Close blocked:"
        elif result.applied:
            header = "Applied close actions:"
        else:
            header = "Planned close actions:"
        print(header)
        for action in result.actions:
            print(f"- [{action.status}] {action.kind}: {action.message}")
    else:
        if result.applied:
            print("No close actions were needed for the selected stack.")
        else:
            print("No managed open pull requests on the selected stack.")

    if not result.applied and not result.blocked and result.actions:
        print(
            f"Re-run with `{_format_close_apply_command(result)}` "
            "to close the selected stack."
        )
    return 1 if result.blocked else 0


def _import_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    result = run_import(
        change_overrides=context.config.change,
        config=context.config.repo,
        current=bool(args.current),
        fetch=bool(args.fetch),
        head=args.head,
        pull_request_reference=args.pull_request,
        repo_root=context.repo_root,
        revset=args.revset,
    )
    print(f"Selected selector: {result.selector}")
    print(f"Selected revset: {result.selected_revset}")
    if result.fetched_tip_commit is not None:
        print(f"Fetched tip commit: {result.fetched_tip_commit}")
    if result.remote is None:
        if result.remote_error is None:
            print("Selected remote: unavailable")
        else:
            print(f"Selected remote: unavailable ({result.remote_error})")
    else:
        print(f"Selected remote: {result.remote.name}")
    if result.github_repository is None:
        if result.github_error is None:
            print("GitHub: unavailable")
        else:
            print(f"GitHub: unavailable ({result.github_error})")
    else:
        print(f"GitHub: {result.github_repository}")
    if result.actions:
        print("Imported review state:")
        for action in result.actions:
            print(f"- [{action.status}] {action.kind}: {action.message}")
    else:
        if result.reviewable_revision_count:
            print("Review state is already up to date for the selected stack.")
        else:
            print("No reviewable commits between the selected revision and `trunk()`.")
    return 0


def _land_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    selected_revset = _resolve_selected_revset(
        args,
        command_label="land",
        require_explicit=True,
    )
    result = run_land(
        apply=bool(args.apply),
        change_overrides=context.config.change,
        config=context.config.repo,
        expect_pr_reference=args.expect_pr,
        repo_root=context.repo_root,
        revset=selected_revset,
    )
    print(f"Selected revset: {result.selected_revset}")
    print(f"Selected remote: {result.remote_name}")
    print(f"GitHub: {result.github_repository}")
    print(f"Trunk: {result.trunk_subject} -> {result.trunk_branch}")
    if result.actions:
        if result.applied:
            header = "Applied land actions:"
        elif result.blocked:
            header = "Land blocked:"
        else:
            header = "Planned land actions:"
        print(header)
        for action in result.actions:
            print(f"- [{action.status}] {action.kind}: {action.message}")
    if result.follow_up is not None:
        print(result.follow_up)
    if not result.applied and not result.blocked:
        print(
            "Re-run with "
            f"`{_format_land_apply_command(result)}` "
            "to update trunk and finalize the prefix."
        )
    return 1 if result.blocked else 0


def _format_land_apply_command(result: LandResult) -> str:
    parts = ["land", "--apply"]
    if result.expect_pr_number is not None:
        parts.extend(("--expect-pr", str(result.expect_pr_number)))
    if result.selected_revset:
        parts.append(result.selected_revset)
    return " ".join(parts)


def _format_close_apply_command(result: CloseResult) -> str:
    parts = ["close", "--apply"]
    if result.cleanup:
        parts.append("--cleanup")
    if result.selected_revset:
        parts.append(result.selected_revset)
    return " ".join(parts)


def _stub_handler(command: str):
    def handler(args: Namespace) -> int:
        context = bootstrap_context(args)
        logger.debug(
            "bootstrapped %s in %s with config %s",
            command,
            context.repo_root,
            context.options.config_path,
        )
        raise CommandNotImplementedError(command)

    return handler


if __name__ == "__main__":
    raise SystemExit(main())
