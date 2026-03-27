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

from jj_review import __version__
from jj_review.bootstrap import BootstrapError, bootstrap_context
from jj_review.commands import (
    cleanup as cleanup_command,
)
from jj_review.commands import (
    close as close_command,
)
from jj_review.commands import (
    import_ as import_command,
)
from jj_review.commands import (
    land as land_command,
)
from jj_review.commands import (
    relink as relink_command,
)
from jj_review.commands import (
    review_state as status_command,
)
from jj_review.commands import (
    submit as submit_command,
)
from jj_review.commands import (
    unlink as unlink_command,
)
from jj_review.completion import emit_shell_completion
from jj_review.errors import CliError, CommandNotImplementedError

logger = logging.getLogger(__name__)
_TOP_LEVEL_HELP_WIDTH = 80
_TOP_LEVEL_HELP_USAGE = "jj-review [-h] [--version] <command> ..."
_TOP_LEVEL_HELP_USAGE_ALL = (
    "jj-review [-h] [--repository REPOSITORY] [--config CONFIG] [--debug] "
    "[--time-output] [--version] <command> ..."
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

_describe_status_preparation_error = status_command.describe_status_preparation_error


@dataclass(frozen=True)
class _HelpCommand:
    name: str
    summary: str
    hidden: bool = False


_TOP_LEVEL_HELP_GROUPS: tuple[tuple[str, tuple[_HelpCommand, ...]], ...] = (
    (
        "Core commands",
        (
            _HelpCommand("submit", submit_command.HELP.strip()),
            _HelpCommand("status", status_command.HELP.strip()),
            _HelpCommand("land", land_command.HELP.strip()),
            _HelpCommand("close", close_command.HELP.strip()),
        ),
    ),
    (
        "Support commands",
        (
            _HelpCommand("cleanup", cleanup_command.HELP.strip()),
            _HelpCommand("import", import_command.HELP.strip()),
        ),
    ),
    (
        "Advanced repair",
        (
            _HelpCommand("relink", relink_command.HELP.strip(), hidden=True),
            _HelpCommand("unlink", unlink_command.HELP.strip(), hidden=True),
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
        help_text=_normalized_help_text(submit_command.HELP),
        description_text=submit_command.__doc__ or "",
        handler=submit_command.handle_submit_command,
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
        help_text=_normalized_help_text(status_command.HELP),
        description_text=status_command.__doc__ or "",
        handler=status_command.handle_status_command,
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
        help_text=_normalized_help_text(relink_command.HELP),
        description_text=relink_command.__doc__ or "",
        handler=lambda args: relink_command.handle_relink_command(
            config_path=args.config,
            current=args.current,
            debug=args.debug,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
        ),
    )
    unlink_parser = _add_revision_command(
        subparsers,
        command="unlink",
        help_text=_normalized_help_text(unlink_command.HELP),
        description_text=unlink_command.__doc__ or "",
        handler=lambda args: unlink_command.handle_unlink_command(
            config_path=args.config,
            current=args.current,
            debug=args.debug,
            repository=args.repository,
            revset=args.revset,
        ),
    )
    unlink_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    land_parser = _add_revision_command(
        subparsers,
        command="land",
        help_text=_normalized_help_text(land_command.HELP),
        description_text=land_command.__doc__ or "",
        handler=lambda args: land_command.handle_land_command(
            apply=args.apply,
            bypass_readiness=args.bypass_readiness,
            config_path=args.config,
            current=args.current,
            debug=args.debug,
            expect_pr=args.expect_pr,
            repository=args.repository,
            revset=args.revset,
        ),
    )
    land_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the landing plan instead of only previewing it",
    )
    land_parser.add_argument(
        "--expect-pr",
        help="Assert that the changes that can be landed now end at this pull request",
    )
    land_parser.add_argument(
        "--bypass-readiness",
        action="store_true",
        help=(
            "Ignore draft and review-decision readiness gates while keeping "
            "normal safety checks"
        ),
    )
    land_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    close_parser = _add_revision_command(
        subparsers,
        command="close",
        help_text=_normalized_help_text(close_command.HELP),
        description_text=close_command.__doc__ or "",
        handler=lambda args: close_command.handle_close_command(
            apply=args.apply,
            cleanup=args.cleanup,
            config_path=args.config,
            current=args.current,
            debug=args.debug,
            repository=args.repository,
            revset=args.revset,
        ),
    )
    close_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the close plan instead of only previewing it",
    )
    close_parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Also clean up pull request branches and saved jj-review data for the stack",
    )
    close_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current stack instead of passing a revset",
    )
    _add_import_parser(
        subparsers,
        command="import",
        help_text=_normalized_help_text(import_command.HELP),
        description_text=import_command.__doc__ or "",
        handler=lambda args: import_command.handle_import_command(
            config_path=args.config,
            current=args.current,
            debug=args.debug,
            fetch=args.fetch,
            head=args.head,
            pull_request=args.pull_request,
            repository=args.repository,
            revset=args.revset,
        ),
    )

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help=_normalized_help_text(cleanup_command.HELP),
        description=_normalized_help_text(cleanup_command.__doc__ or ""),
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
    cleanup_parser.set_defaults(handler=cleanup_command.handle_cleanup_command)

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
    with _time_output(enabled=args.time_output):
        handler = args.handler
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
    handler=None,
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
    parser.set_defaults(handler=handler or _stub_handler(command))
    return parser


def _add_import_parser[SubparserT: ArgumentParser](
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
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--pull-request",
        help="Pull request number or URL",
    )
    selector.add_argument(
        "--head",
        help="Branch name to import",
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
            `--pull-request` or `--head`, fetch only the branches needed
            to import that stack
            """
        ),
    )
    parser.set_defaults(handler=handler or _stub_handler(command))
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
        help="Use this config file",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=SUPPRESS if suppress_defaults else False,
        help="Enable debug logging",
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
        raise CliError(
            f"Invalid value for `--draft`: {draft_mode!r}. Expected `new` or `all`."
        )
    return normalized


def _stub_handler(command: str):
    def handler(args: Namespace) -> int:
        context = bootstrap_context(
            repository=args.repository,
            config_path=args.config,
            debug=args.debug,
        )
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
