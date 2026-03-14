"""CLI entrypoint for the standalone `jj-review` executable."""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser, Namespace, _SubParsersAction
from collections.abc import Sequence
from pathlib import Path

from jj_review import __version__
from jj_review.bookmarks import BookmarkResolver
from jj_review.bootstrap import BootstrapError, bootstrap_context
from jj_review.cache import ReviewStateStore
from jj_review.commands.submit import run_submit
from jj_review.errors import CliError, CommandNotImplementedError
from jj_review.jj import JjClient

logger = logging.getLogger(__name__)


def build_parser() -> ArgumentParser:
    """Build the top-level CLI parser and subcommands."""

    parser = ArgumentParser(
        prog="jj-review",
        description="JJ-native stacked GitHub review tooling.",
    )
    parser.add_argument(
        "--repository",
        type=Path,
        help="Workspace path to operate on. Defaults to the current directory.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Explicit path to a TOML config file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )

    subparsers = parser.add_subparsers(dest="command")
    _add_revision_command(
        subparsers,
        command="submit",
        help_text="Project a local jj stack onto GitHub pull requests.",
        handler=_submit_handler,
    )
    _add_revision_command(
        subparsers,
        command="status",
        help_text="Show cached and remote review state for a stack.",
        handler=_status_handler,
    )
    _add_revision_command(
        subparsers,
        command="sync",
        help_text="Refresh cached review linkage from GitHub.",
    )

    adopt_parser = subparsers.add_parser(
        "adopt",
        help="Associate an existing pull request with a local change.",
    )
    adopt_parser.add_argument("pull_request", help="Pull request number or URL.")
    adopt_parser.add_argument(
        "revset",
        nargs="?",
        help="Revision to associate with the pull request.",
    )
    adopt_parser.set_defaults(handler=_stub_handler("adopt"))

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Report or apply conservative review cleanup actions.",
    )
    cleanup_parser.set_defaults(handler=_stub_handler("cleanup"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
    handler = getattr(args, "handler", None)
    if handler is None:
        parser.print_help()
        return 0

    try:
        return handler(args)
    except (BootstrapError, CliError) as error:
        print(error, file=sys.stderr)
        return error.exit_code


def _add_revision_command(
    subparsers: _SubParsersAction[ArgumentParser],
    *,
    command: str,
    help_text: str,
    handler=None,
) -> None:
    parser = subparsers.add_parser(command, help=help_text)
    parser.add_argument("revset", nargs="?", help="Revision to operate on.")
    parser.set_defaults(handler=handler or _stub_handler(command))


def _status_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    stack = JjClient(context.repo_root).discover_review_stack(args.revset)
    state_store = ReviewStateStore.for_repo(context.repo_root)
    state = state_store.load()
    bookmark_result = BookmarkResolver(state, context.config.change).pin_revisions(
        stack.revisions
    )
    if bookmark_result.changed:
        state_store.save(bookmark_result.state)
    bookmarks_by_change = {
        resolution.change_id: resolution for resolution in bookmark_result.resolutions
    }
    print(f"Selected revset: {stack.selected_revset}")
    print(f"Trunk: {stack.trunk.subject} [{stack.trunk.change_id[:12]}]")
    if not stack.revisions:
        print("No reviewable commits between the selected revision and `trunk()`.")
        return 0

    print("Stack:")
    for revision in stack.revisions:
        bookmark = bookmarks_by_change[revision.change_id]
        print(
            f"- {revision.subject} [{revision.change_id[:12]}] "
            f"-> {bookmark.bookmark} ({bookmark.source})"
        )
    return 0


def _submit_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    result = run_submit(
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=args.revset,
    )
    print(f"Selected revset: {result.selected_revset}")
    print(f"Selected remote: {result.remote.name}")
    print(f"Trunk: {result.trunk_subject}")
    if not result.revisions:
        print("No reviewable commits between the selected revision and `trunk()`.")
        return 0

    print("Projected review bookmarks:")
    for revision in result.revisions:
        print(
            f"- {revision.subject} [{revision.change_id[:12]}] -> {revision.bookmark} "
            f"({revision.bookmark_source}, {revision.remote_action})"
        )
    return 0


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
