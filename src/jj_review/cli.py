"""CLI entrypoint for the standalone `jj-review` executable."""

from __future__ import annotations

import logging
import sys
from argparse import ArgumentParser, Namespace, _SubParsersAction
from collections.abc import Sequence
from pathlib import Path

from jj_review import __version__
from jj_review.bootstrap import BootstrapError, bootstrap_context
from jj_review.commands.review_state import run_status, run_sync
from jj_review.commands.submit import run_submit
from jj_review.errors import CliError, CommandNotImplementedError

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
        handler=_sync_handler,
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
    result = run_status(
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=args.revset,
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
            print(f"GitHub: unavailable ({result.github_error})")
    else:
        print(f"GitHub: {result.github_repository}")
        if result.github_error is not None:
            print(f"GitHub note: {result.github_error}")
    print(f"Trunk: {result.trunk_subject}")
    if not result.revisions:
        print("No reviewable commits between the selected revision and `trunk()`.")
        return 0

    print("Stack:")
    for revision in result.revisions:
        details: list[str] = [revision.bookmark_source]
        if result.remote is not None or result.remote_error is not None:
            details.append(_format_remote_status(revision))
        if result.github_repository is not None:
            details.append(_format_pull_request_status(revision))
            if (
                revision.pull_request_lookup is not None
                and revision.pull_request_lookup.state == "open"
            ):
                details.append(_format_stack_comment_status(revision))
        print(
            f"- {revision.subject} [{revision.change_id[:12]}] -> {revision.bookmark} "
            f"({', '.join(details)})"
        )
    return 0


def _sync_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    result = run_sync(
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=args.revset,
    )
    print(f"Selected revset: {result.selected_revset}")
    print(f"Selected remote: {result.remote.name}")
    print(f"GitHub: {result.github_repository}")
    print(f"Trunk: {result.trunk_subject}")
    if not result.revisions:
        print("No reviewable commits between the selected revision and `trunk()`.")
        return 0

    print("Synchronized review cache:")
    for revision in result.revisions:
        details: list[str] = [
            revision.bookmark_source,
            _format_pull_request_status(revision),
            _format_stack_comment_status(revision),
        ]
        print(
            f"- {revision.subject} [{revision.change_id[:12]}] -> {revision.bookmark} "
            f"({', '.join(details)})"
        )
    return 0


def _format_remote_status(revision) -> str:
    remote_state = revision.remote_state
    if remote_state is None:
        return "remote unavailable"
    if remote_state.remote == "":
        return "remote not configured"
    if len(remote_state.targets) > 1:
        return f"remote {remote_state.remote} conflicted"
    if remote_state.target is None:
        return f"remote {remote_state.remote} missing"
    tracking = "tracked" if remote_state.is_tracked else "untracked"
    return f"remote {remote_state.remote} {tracking}"


def _format_pull_request_status(revision) -> str:
    cached_change = revision.cached_change
    cached_label = "no cached PR"
    if cached_change is not None and cached_change.pr_number is not None:
        cached_label = f"cached PR #{cached_change.pr_number}"
    lookup = revision.pull_request_lookup
    if lookup is None:
        return cached_label
    if lookup.state == "open":
        if lookup.pull_request is None:
            raise AssertionError("Open pull request lookup must include a pull request.")
        return f"{cached_label}, GitHub PR #{lookup.pull_request.number}"
    if lookup.state == "missing":
        return f"{cached_label}, no GitHub PR"
    message = lookup.message or "pull request lookup failed"
    return f"{cached_label}, {message}"


def _format_stack_comment_status(revision) -> str:
    cached_change = revision.cached_change
    cached_label = "no cached stack comment"
    if cached_change is not None and cached_change.stack_comment_id is not None:
        cached_label = f"cached stack comment #{cached_change.stack_comment_id}"
    lookup = revision.stack_comment_lookup
    if lookup is None:
        return cached_label
    if lookup.state == "present":
        if lookup.comment is None:
            raise AssertionError("Present stack comment lookup must include a comment.")
        return f"{cached_label}, stack comment #{lookup.comment.id}"
    if lookup.state == "missing":
        return f"{cached_label}, no stack comment"
    message = lookup.message or "stack comment lookup failed"
    return f"{cached_label}, {message}"


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
    print(f"Trunk: {result.trunk_subject} -> {result.trunk_branch}")
    if not result.revisions:
        print("No reviewable commits between the selected revision and `trunk()`.")
        return 0

    print("Projected review bookmarks:")
    for revision in result.revisions:
        print(
            f"- {revision.subject} [{revision.change_id[:12]}] -> {revision.bookmark} "
            f"({revision.bookmark_source}, {revision.remote_action}, "
            f"PR #{revision.pull_request_number} {revision.pull_request_action})"
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
