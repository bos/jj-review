"""CLI entrypoint for the standalone `jj-review` executable."""

from __future__ import annotations

import builtins
import io
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
from jj_review.commands.adopt import run_adopt
from jj_review.commands.cleanup import prepare_cleanup, stream_cleanup
from jj_review.commands.review_state import prepare_status, stream_status
from jj_review.commands.submit import run_submit
from jj_review.errors import CliError, CommandNotImplementedError

logger = logging.getLogger(__name__)
_DISPLAY_CHANGE_ID_LENGTH = 8


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
    status_parser = _add_revision_command(
        subparsers,
        command="status",
        help_text="Show cached and remote review state for a stack.",
        handler=_status_handler,
        parents=[common_options],
    )
    status_parser.add_argument(
        "-f",
        "--fetch",
        action="store_true",
        help="Fetch remote bookmark state before inspecting review status.",
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
    adopt_parser.set_defaults(handler=_adopt_handler)

    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Report or apply conservative review cleanup actions.",
        parents=[common_options],
    )
    cleanup_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply safe cleanup actions instead of only reporting them.",
    )
    cleanup_parser.set_defaults(handler=_cleanup_handler)
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
) -> ArgumentParser:
    parser = subparsers.add_parser(command, help=help_text, parents=parents or [])
    parser.add_argument("revset", nargs="?", help="Revision to operate on.")
    parser.set_defaults(handler=handler or _stub_handler(command))
    return parser


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
        destination = kwargs.pop("file", sys.stdout)
        flush = kwargs.pop("flush", False)
        end = kwargs.get("end", "\n")
        buffer = io.StringIO()
        original_print(*args, file=buffer, flush=False, **kwargs)
        rendered = buffer.getvalue()
        if rendered:
            prefix = f"[{elapsed:0.6f}] "
            destination.write(_prefix_rendered_lines(rendered, prefix=prefix))
        elif end:
            destination.write(f"[{elapsed:0.6f}] {end}")
        if flush:
            destination.flush()

    builtins.print = cast(Any, timed_print)
    try:
        yield
    finally:
        builtins.print = original_print


def _prefix_rendered_lines(rendered: str, *, prefix: str) -> str:
    return "".join(f"{prefix}{line}" for line in rendered.splitlines(keepends=True))


def _status_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    prepared_status = prepare_status(
        change_overrides=context.config.change,
        config=context.config.repo,
        fetch_remote_state=args.fetch,
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
    return 1 if result.incomplete else 0
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
    if lookup is None:
        if github_available:
            summary = "not submitted"
        elif cached_label is not None:
            summary = cached_label
        else:
            summary = "GitHub status unknown"
    elif lookup.state == "open":
        if lookup.pull_request is None:
            raise AssertionError("Open pull request lookup must include a pull request.")
        summary = f"PR #{lookup.pull_request.number}"
        review_decision = _effective_review_decision(
            cached_change=cached_change,
            lookup=lookup,
        )
        if review_decision == "approved":
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
            summary = f"PR #{lookup.pull_request.number} merged"
        else:
            summary = f"PR #{lookup.pull_request.number} closed"
    else:
        message = lookup.message or "GitHub lookup failed"
        if cached_label is not None:
            summary = f"{cached_label}, {message}"
        else:
            summary = message

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

    label = f"cached PR #{cached_change.pr_number}"
    if cached_change.pr_state is None:
        return label

    details = [cached_change.pr_state]
    if (
        cached_change.pr_state == "open"
        and cached_change.pr_review_decision is not None
    ):
        details.append(_format_review_decision_label(cached_change.pr_review_decision))
    return f"{label} ({', '.join(details)})"


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
            f"- {revision.subject} [{_display_change_id(revision.change_id)}] -> "
            f"{revision.bookmark} "
            f"({revision.bookmark_source}, {revision.remote_action}, "
            f"PR #{revision.pull_request_number} {revision.pull_request_action})"
        )
    return 0


def _adopt_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    result = run_adopt(
        config=context.config.repo,
        pull_request_reference=args.pull_request,
        repo_root=context.repo_root,
        revset=args.revset,
    )
    print(f"Selected revset: {result.selected_revset}")
    print(f"Selected remote: {result.remote_name}")
    print(f"GitHub: {result.github_repository}")
    print(
        f"Adopted PR #{result.pull_request_number} for {result.subject} "
        f"[{_display_change_id(result.change_id)}] -> {result.bookmark}"
    )
    return 0


def _cleanup_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
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
