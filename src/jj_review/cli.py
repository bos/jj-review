"""CLI entrypoint for the standalone `jj-review` executable."""

from __future__ import annotations

import builtins
import io
import logging
import re
import sys
import textwrap
import time
from argparse import SUPPRESS, ArgumentParser, Namespace, _SubParsersAction
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

from jj_review import __version__
from jj_review.bootstrap import BootstrapError, bootstrap_context
from jj_review.commands.adopt import run_relink
from jj_review.commands.cleanup import (
    prepare_cleanup,
    prepare_restack,
    stream_cleanup,
    stream_restack,
)
from jj_review.commands.close import CloseResult, run_close
from jj_review.commands.import_ import run_import
from jj_review.commands.land import LandResult, run_land
from jj_review.commands.review_state import prepare_status, stream_status
from jj_review.commands.submit import run_submit
from jj_review.commands.unlink import run_unlink
from jj_review.completion import emit_shell_completion
from jj_review.errors import CliError, CommandNotImplementedError
from jj_review.intent import intent_change_ids, pid_is_alive
from jj_review.jj import UnsupportedStackError

logger = logging.getLogger(__name__)
_DISPLAY_CHANGE_ID_LENGTH = 8
_UNSUPPORTED_STACK_CHANGE_RE = re.compile(r"^Unsupported stack shape at (\w+): (.+)$")


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
    submit_parser = _add_revision_command(
        subparsers,
        command="submit",
        help_text="Project a local jj stack onto GitHub pull requests.",
        handler=_submit_handler,
        parents=[common_options],
    )
    submit_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the submit plan without mutating local, remote, or GitHub state.",
    )
    submit_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current review path instead of passing a revset.",
    )
    submit_draft_mode = submit_parser.add_mutually_exclusive_group()
    submit_draft_mode.add_argument(
        "--draft",
        action="store_true",
        help="Create newly opened pull requests as drafts.",
    )
    submit_draft_mode.add_argument(
        "--publish",
        action="store_true",
        help="Mark existing draft pull requests ready for review on submit.",
    )
    submit_parser.add_argument(
        "--reviewers",
        dest="reviewers",
        action="append",
        help=(
            "Comma-separated GitHub usernames to request on submitted pull requests. "
            "Repeat to add more. Overrides configured reviewers."
        ),
    )
    submit_parser.add_argument(
        "--team-reviewers",
        dest="team_reviewers",
        action="append",
        help=(
            "Comma-separated GitHub team slugs to request on submitted pull requests. "
            "Repeat to add more. Overrides configured team reviewers."
        ),
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
    _add_relink_parser(
        subparsers,
        command="relink",
        help_text="Advanced repair: reassociate an existing pull request with a local change.",
        parents=[common_options],
    )
    _add_relink_parser(
        subparsers,
        command="adopt",
        help_text=SUPPRESS,
        parents=[common_options],
    )
    unlink_parser = _add_revision_command(
        subparsers,
        command="unlink",
        help_text="Advanced repair: detach one local change from managed review ownership.",
        handler=_unlink_handler,
        parents=[common_options],
    )
    unlink_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current review path instead of passing a revset.",
    )
    land_parser = _add_revision_command(
        subparsers,
        command="land",
        help_text="Preview or land the trunk-open review prefix for a stack.",
        handler=_land_handler,
        parents=[common_options],
    )
    land_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the landing plan instead of only previewing it.",
    )
    land_parser.add_argument(
        "--expect-pr",
        help="Assert that the selected landable prefix ends at this pull request.",
    )
    land_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current review path instead of passing a revset.",
    )
    close_parser = _add_revision_command(
        subparsers,
        command="close",
        help_text="Preview or close the managed review path for a stack.",
        handler=_close_handler,
        parents=[common_options],
    )
    close_parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the close plan instead of only previewing it.",
    )
    close_parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Also clean up owned review branches and managed metadata.",
    )
    close_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current review path instead of passing a revset.",
    )
    _add_import_parser(
        subparsers,
        command="import",
        help_text=(
            "Materialize sparse local review state for an exact PR, head, "
            "current path, or explicit revset."
        ),
        parents=[common_options],
    )

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
    cleanup_parser.add_argument(
        "--restack",
        action="store_true",
        help="Preview or apply a local restack for merged review units on the selected path.",
    )
    cleanup_parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current review path instead of passing a revset.",
    )
    cleanup_parser.add_argument(
        "revset",
        nargs="?",
        help="Revision whose stack should be inspected or restacked.",
    )
    cleanup_parser.set_defaults(handler=_cleanup_handler)

    completion_parser = subparsers.add_parser(
        "completion",
        help="Print a shell completion script for bash, zsh, or fish.",
    )
    completion_parser.add_argument(
        "shell",
        choices=("bash", "zsh", "fish"),
        help="Shell to generate completion support for.",
    )
    completion_parser.set_defaults(handler=_completion_handler)
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


def _add_relink_parser(
    subparsers: _SubParsersAction[ArgumentParser],
    *,
    command: str,
    help_text: str,
    parents=None,
) -> ArgumentParser:
    parser = subparsers.add_parser(command, help=help_text, parents=parents or [])
    parser.add_argument("pull_request", help="Pull request number or URL.")
    parser.add_argument(
        "--current",
        action="store_true",
        help="Explicitly operate on the current review path instead of passing a revset.",
    )
    parser.add_argument(
        "revset",
        nargs="?",
        help="Revision to reassociate with the pull request.",
    )
    parser.set_defaults(handler=_relink_handler)
    return parser


def _add_import_parser(
    subparsers: _SubParsersAction[ArgumentParser],
    *,
    command: str,
    help_text: str,
    parents=None,
) -> ArgumentParser:
    parser = subparsers.add_parser(command, help=help_text, parents=parents or [])
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument(
        "--pull-request",
        help="Pull request number or URL.",
    )
    selector.add_argument(
        "--head",
        help="Review branch name to import.",
    )
    selector.add_argument(
        "--current",
        action="store_true",
        help="Import the current review path.",
    )
    selector.add_argument(
        "--revset",
        help="Explicit revset whose exact stack should be imported.",
    )
    parser.set_defaults(handler=_import_handler)
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

    builtins.print = cast(Any, timed_print)
    try:
        yield
    finally:
        builtins.print = original_print


def _prefix_rendered_lines(rendered: str, *, prefix: str) -> str:
    return "".join(f"{prefix}{line}" for line in rendered.splitlines(keepends=True))


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
    message = str(error)
    match = _UNSUPPORTED_STACK_CHANGE_RE.match(message)
    if match is None:
        return (
            "Could not inspect review status because local history no longer forms a "
            f"supported linear stack. {message}"
        )

    change_id, reason = match.groups()
    if reason == "divergent changes are not supported.":
        return (
            "Could not inspect review status because local history no longer forms a "
            f"supported linear stack. {message} Inspect the divergent revisions with "
            f"`jj log -r 'change_id({change_id})'` and reconcile them before retrying. "
            "This can happen after `status --fetch` or another fetch imports remote "
            "bookmark updates for merged PRs."
        )
    return (
        "Could not inspect review status because local history no longer forms a "
        f"supported linear stack. {message}"
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
    if getattr(revision, "link_state", "active") == "detached":
        if lookup is not None and lookup.pull_request is not None:
            pull_request = lookup.pull_request
            if pull_request.state == "open":
                summary = _format_pull_request_label(
                    pull_request.number,
                    is_draft=getattr(pull_request, "is_draft", False),
                    prefix="detached ",
                )
            else:
                summary = f"detached PR #{pull_request.number} {pull_request.state}"
        elif revision.remote_state is not None and revision.remote_state.targets:
            summary = "detached review branch"
        else:
            summary = "detached"
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
    linkage_revisions = [
        revision
        for revision in result.revisions
        if _revision_has_linkage_advisory(revision)
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
        and not linkage_revisions
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
    if linkage_revisions:
        next_status = f"jj-review status --fetch {result.selected_revset}"
        next_relink = f"jj-review relink <pr> {result.selected_revset}"
        _print_wrapped_advisory(
            f"Review linkage note: refresh remote and GitHub observations with "
            f"`{next_status}`. If the existing PR should stay attached to one of these "
            f"changes, repair that linkage intentionally with `{next_relink}`."
        )
        for revision in linkage_revisions:
            _print_wrapped_advisory(
                f"{_status_revision_label(revision)}: {_describe_linkage_advisory(revision)}"
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


def _revision_has_linkage_advisory(revision) -> bool:
    if getattr(revision, "link_state", "active") == "detached":
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


def _describe_linkage_advisory(revision) -> str:
    lookup = revision.pull_request_lookup
    if lookup is None:
        raise AssertionError("Linkage advisory requires a pull request lookup.")
    if lookup.state == "ambiguous":
        return lookup.message or "GitHub reports ambiguous pull request linkage"
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
    raise AssertionError(f"Unexpected linkage advisory state: {lookup.state}")


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
            print("Projected review bookmarks:")
        emitted_section_header = True

    result = run_submit(
        change_overrides=context.config.change,
        config=context.config.repo,
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
            print("Projected review bookmarks:")
    for revision in result.revisions:
        _print_submit_revision(revision)
    return 0


def _print_submit_revision(revision) -> None:
    print(f"- {revision.subject} [{_display_change_id(revision.change_id)}]")
    print(f"  -> {revision.bookmark}{_render_submit_revision_suffix(revision)}")


def _submit_draft_mode(args: Namespace) -> Literal["default", "draft", "publish"]:
    if getattr(args, "draft", False):
        return "draft"
    if getattr(args, "publish", False):
        return "publish"
    return "default"


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
    if result.already_detached:
        print(
            f"{result.subject} [{_display_change_id(result.change_id)}] is already detached "
            "from managed review."
        )
        return 0
    if result.bookmark is None:
        print(
            f"Detached managed review state for {result.subject} "
            f"[{_display_change_id(result.change_id)}]."
        )
    else:
        print(
            f"Detached managed review state for {result.subject} "
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
            print("No merged review units on the selected path need restacking.")
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
            print("No close actions were needed for the selected path.")
        else:
            print("No managed open pull requests on the selected path.")

    if not result.applied and not result.blocked and result.actions:
        print(
            f"Re-run with `{_format_close_apply_command(result)}` "
            "to close the selected path."
        )
    return 1 if result.blocked else 0


def _import_handler(args: Namespace) -> int:
    context = bootstrap_context(args)
    result = run_import(
        change_overrides=context.config.change,
        config=context.config.repo,
        current=bool(args.current),
        head=args.head,
        pull_request_reference=args.pull_request,
        repo_root=context.repo_root,
        revset=args.revset,
    )
    print(f"Selected selector: {result.selector}")
    print(f"Selected revset: {result.selected_revset}")
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
