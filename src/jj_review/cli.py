"""CLI entrypoint for the standalone `jj-review` executable."""

from __future__ import annotations

import builtins
import logging
import sys
import time
from argparse import SUPPRESS, ArgumentParser, Namespace, _SubParsersAction
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from jj_review import __version__
from jj_review.bootstrap import BootstrapError, bootstrap_context
from jj_review.commands.review_state import prepare_status, run_sync, stream_status
from jj_review.commands.submit import run_submit
from jj_review.errors import CliError, CommandNotImplementedError

logger = logging.getLogger(__name__)


def build_parser() -> ArgumentParser:
    """Build the top-level CLI parser and subcommands."""

    common_options = _build_common_options_parser()
    parser = ArgumentParser(
        prog="jj-review",
        description="JJ-native stacked GitHub review tooling.",
        parents=[common_options],
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
        parents=[common_options],
    )
    _add_revision_command(
        subparsers,
        command="status",
        help_text="Show cached and remote review state for a stack.",
        handler=_status_handler,
        parents=[common_options],
    )
    _add_revision_command(
        subparsers,
        command="sync",
        help_text="Refresh cached review linkage from GitHub.",
        handler=_sync_handler,
        parents=[common_options],
    )

    adopt_parser = subparsers.add_parser(
        "adopt",
        help="Associate an existing pull request with a local change.",
        parents=[common_options],
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
        parents=[common_options],
    )
    cleanup_parser.set_defaults(handler=_stub_handler("cleanup"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return a process exit code."""

    parser = build_parser()
    args = parser.parse_args(argv)
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


def _add_revision_command(
    subparsers: _SubParsersAction[ArgumentParser],
    *,
    command: str,
    help_text: str,
    handler=None,
    parents=None,
) -> None:
    parser = subparsers.add_parser(command, help=help_text, parents=parents or [])
    parser.add_argument("revset", nargs="?", help="Revision to operate on.")
    parser.set_defaults(handler=handler or _stub_handler(command))


def _build_common_options_parser() -> ArgumentParser:
    parser = ArgumentParser(add_help=False)
    parser.add_argument(
        "--repository",
        type=Path,
        default=SUPPRESS,
        help="Workspace path to operate on. Defaults to the current directory.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=SUPPRESS,
        help="Explicit path to a TOML config file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=SUPPRESS,
        help="Enable debug logging.",
    )
    parser.add_argument(
        "--time-output",
        action="store_true",
        default=SUPPRESS,
        help="Prefix each printed line with elapsed seconds since process start.",
    )
    return parser


@contextmanager
def _time_output(*, enabled: bool):
    if not enabled:
        yield
        return

    start = time.perf_counter()
    original_print = builtins.print

    def timed_print(*args, **kwargs) -> None:
        elapsed = time.perf_counter() - start
        original_print(f"[{elapsed:0.6f}]", *args, **kwargs)

    builtins.print = cast(Any, timed_print)
    try:
        yield
    finally:
        builtins.print = original_print


def _status_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    prepared_status = prepare_status(
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=args.revset,
    )
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
        print(f"- {revision.subject} [{revision.change_id[:12]}]: {summary}")

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
    return 1 if result.incomplete else 0


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
    return f"◆ {trunk.subject} [{trunk.change_id[:12]}]: {suffix}"


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
    cached_pr_number = None if cached_change is None else cached_change.pr_number
    if lookup is None:
        if github_available:
            return "not submitted"
        if cached_pr_number is not None:
            return f"cached PR #{cached_pr_number}"
        return "GitHub status unknown"
    if lookup.state == "open":
        if lookup.pull_request is None:
            raise AssertionError("Open pull request lookup must include a pull request.")
        return f"PR #{lookup.pull_request.number}"
    if lookup.state == "missing":
        if cached_pr_number is not None:
            return f"cached PR #{cached_pr_number}, no GitHub PR"
        return "not submitted"
    if lookup.state == "closed":
        if lookup.pull_request is None:
            raise AssertionError("Closed pull request lookup must include a pull request.")
        return f"PR #{lookup.pull_request.number} is {lookup.pull_request.state}"
    message = lookup.message or "GitHub lookup failed"
    if cached_pr_number is not None:
        return f"cached PR #{cached_pr_number}, {message}"
    return message


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
