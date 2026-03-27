"""Show how the selected jj stack currently appears on GitHub.

This reports the pull requests and GitHub branches jj-review is using for each
change without changing anything.
"""

from __future__ import annotations

import textwrap
from collections.abc import Callable
from pathlib import Path

from jj_review import review_inspection as _review_inspection
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.intent import intent_change_ids, pid_is_alive
from jj_review.jj import UnsupportedStackError

_DISPLAY_CHANGE_ID_LENGTH = 8

for _name in dir(_review_inspection):
    if _name.startswith("__"):
        continue
    globals()[_name] = getattr(_review_inspection, _name)

del _name

prepare_status = _review_inspection.prepare_status
stream_status = _review_inspection.stream_status


def run_status_command(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    configured_trunk_branch: str | None,
    emit: Callable[[str], None],
    fetch_remote_state: bool,
    prepare_status_fn: Callable[..., object] | None = None,
    repo_root: Path,
    revset: str | None,
    stream_status_fn: Callable[..., object] | None = None,
) -> int:
    """Prepare, stream, and render the `status` command."""

    prepare_fn = prepare_status if prepare_status_fn is None else prepare_status_fn
    stream_fn = stream_status if stream_status_fn is None else stream_status_fn
    try:
        prepared_status = prepare_fn(
            change_overrides=change_overrides,
            config=config,
            fetch_remote_state=fetch_remote_state,
            repo_root=repo_root,
            revset=revset,
        )
    except UnsupportedStackError as error:
        raise CliError(describe_status_preparation_error(error)) from error

    for line in render_status_selection_lines(prepared_status=prepared_status):
        emit(line)

    stack_started = False

    def emit_github_status(github_repository: str | None, github_error: str | None) -> None:
        nonlocal stack_started
        for line in render_status_github_lines(
            github_error=github_error,
            github_repository=github_repository,
            has_revisions=bool(prepared_status.prepared.status_revisions),
        ):
            emit(line)
        if prepared_status.prepared.status_revisions:
            stack_started = True

    def emit_revision(revision, github_available: bool) -> None:
        emit(
            render_status_revision_line(
                revision,
                github_available=github_available,
            )
        )

    result = stream_fn(
        on_github_status=emit_github_status,
        on_revision=emit_revision,
        prepared_status=prepared_status,
    )
    if not prepared_status.prepared.status_revisions:
        for line in render_empty_status_lines(
            prepared_status=prepared_status,
            configured_trunk_branch=configured_trunk_branch,
        ):
            emit(line)
        return 0

    if not stack_started:
        emit("Stack:")
    emit(
        render_trunk_status_row(
            prepared_status.prepared,
            configured_trunk_branch=configured_trunk_branch,
        )
    )
    for line in render_status_advisory_lines(result=result):
        emit(line)
    for line in render_status_intent_lines(prepared_status=prepared_status):
        emit(line)

    exit_code = 1 if result.incomplete else 0
    selected_change_ids = {revision.change_id for revision in getattr(result, "revisions", ())}
    overlapping = any(
        intent_change_ids(loaded.intent) & selected_change_ids
        for loaded in prepared_status.outstanding_intents
    )
    if overlapping:
        exit_code = max(exit_code, 1)
    return exit_code


def describe_status_preparation_error(error: UnsupportedStackError) -> str:
    """Describe a `status` preparation failure for users."""

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


def display_change_id(change_id: str) -> str:
    """Render the short change ID shown in CLI output."""

    return change_id[:_DISPLAY_CHANGE_ID_LENGTH]


def format_pull_request_label(
    pull_request_number: int,
    *,
    is_draft: bool,
    prefix: str = "",
) -> str:
    """Render a pull request label for CLI output."""

    label = f"PR #{pull_request_number}"
    if is_draft:
        label = f"draft {label}"
    return f"{prefix}{label}"


def render_status_selection_lines(*, prepared_status) -> tuple[str, ...]:
    """Render the selected revset and remote lines."""

    prepared = prepared_status.prepared
    lines = [f"Selected revset: {prepared_status.selected_revset}"]
    if prepared.remote is None:
        if prepared.remote_error is None:
            lines.append("Selected remote: unavailable")
        else:
            lines.append(f"Selected remote: unavailable ({prepared.remote_error})")
    else:
        lines.append(f"Selected remote: {prepared.remote.name}")
    return tuple(lines)


def render_status_github_lines(
    *,
    github_error: str | None,
    github_repository: str | None,
    has_revisions: bool,
) -> tuple[str, ...]:
    """Render GitHub availability lines as status streaming begins."""

    lines: list[str] = []
    if github_repository is None:
        if github_error is not None:
            lines.append(f"GitHub target: unavailable ({github_error})")
    else:
        if github_error is None:
            lines.append(f"GitHub: {github_repository}")
        else:
            lines.append(f"GitHub target: {github_repository} ({github_error})")
    if has_revisions:
        lines.append("Stack:")
    return tuple(lines)


def render_status_revision_line(revision, *, github_available: bool) -> str:
    """Render one streamed status row."""

    summary = _format_status_summary(revision, github_available=github_available)
    return f"- {revision.subject} [{display_change_id(revision.change_id)}]: {summary}"


def render_trunk_status_row(
    prepared,
    *,
    configured_trunk_branch: str | None,
) -> str:
    """Render the trunk footer row."""

    trunk = prepared.stack.trunk
    trunk_name = _resolve_status_trunk_name(
        prepared,
        configured_trunk_branch=configured_trunk_branch,
    )
    suffix = "trunk()" if trunk_name is None else trunk_name
    return f"◆ {trunk.subject} [{display_change_id(trunk.change_id)}]: {suffix}"


def render_empty_status_lines(
    *,
    configured_trunk_branch: str | None,
    prepared_status,
) -> tuple[str, ...]:
    """Render the empty-stack footer and explanation."""

    return (
        render_trunk_status_row(
            prepared_status.prepared,
            configured_trunk_branch=configured_trunk_branch,
        ),
        "No reviewable commits between the selected revision and `trunk()`.",
    )


def render_status_advisory_lines(*, result) -> tuple[str, ...]:
    """Render any advisories that follow the status stack output."""

    if not hasattr(result, "revisions") or not hasattr(result, "selected_revset"):
        return ()

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
        return ()

    lines = ["", "Advisories:"]
    if cleanup_revisions:
        next_command = f"jj-review cleanup --restack {result.selected_revset}"
        lines.append(
            _wrap_advisory(
                "Submit note: descendant PR bases still follow the old local ancestry "
                "until the stack is restacked"
            )
        )
        lines.append(
            _wrap_advisory(
                f"Next step: run `{next_command}` to preview the local restack plan, "
                "then rerun it with `--apply`"
            )
        )
        for revision in cleanup_revisions:
            pull_request_number = _revision_pull_request_number(revision)
            pull_request_label = (
                f"PR #{pull_request_number}" if pull_request_number is not None else "merged PR"
            )
            lines.append(
                _wrap_advisory(
                    f"{_status_revision_label(revision)}: {pull_request_label} is merged, "
                    "and later local changes are still based on it"
                )
            )

    if link_revisions:
        next_status = f"jj-review status --fetch {result.selected_revset}"
        next_relink = f"jj-review relink <pr> {result.selected_revset}"
        lines.append(
            _wrap_advisory(
                "PR link note: refresh remote and GitHub observations with "
                f"`{next_status}`. If the existing PR should stay attached to one of these "
                f"changes, repair that PR link intentionally with `{next_relink}`."
            )
        )
        for revision in link_revisions:
            lines.append(
                _wrap_advisory(
                    f"{_status_revision_label(revision)}: "
                    f"{_describe_link_advisory(revision)}"
                )
            )

    for revision in policy_warnings:
        base_ref = revision.pull_request_lookup.pull_request.base.ref
        pull_request_number = _revision_pull_request_number(revision)
        lines.append(
            _wrap_advisory(
                f"Repository policy warning: PR #{pull_request_number} merged into "
                f"{base_ref}; configure GitHub to block merges of PRs targeting "
                "`review/*`"
            )
        )

    for revision in divergent_revisions:
        lines.append(
            _wrap_advisory(
                f"{_status_revision_label(revision)}: resolve the multiple visible "
                "revisions for this change before retrying "
                f"(`jj log -r 'change_id({revision.change_id})'`)"
            )
        )
    return tuple(lines)


def render_status_intent_lines(*, prepared_status) -> tuple[str, ...]:
    """Render any stale or incomplete operation notices."""

    lines: list[str] = []
    if prepared_status.stale_intents:
        lines.extend(("", "Stale incomplete operations (change IDs no longer in repo):"))
        for loaded in prepared_status.stale_intents:
            alive = pid_is_alive(loaded.intent.pid)
            status_str = "process alive" if alive else "process dead"
            lines.append(f"  {loaded.intent.label}  [{status_str}, {loaded.path.name}]")

    if prepared_status.outstanding_intents:
        lines.extend(("", "Incomplete operations detected:"))
        for loaded in prepared_status.outstanding_intents:
            alive = pid_is_alive(loaded.intent.pid)
            if alive:
                lines.append(
                    f"  {loaded.intent.label}  [in progress, PID {loaded.intent.pid}]"
                )
            else:
                lines.append(
                    f"  {loaded.intent.label}  [interrupted, re-run to complete]"
                )
    return tuple(lines)


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
                summary = format_pull_request_label(
                    pull_request.number,
                    is_draft=getattr(pull_request, "is_draft", False),
                    prefix="unlinked ",
                )
            else:
                summary = f"unlinked PR #{pull_request.number} {pull_request.state}"
        elif revision.remote_state is not None and revision.remote_state.targets:
            summary = "unlinked branch"
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
        summary = format_pull_request_label(
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
        message = stack_comment_lookup.message or "stack summary comment lookup failed"
        return f"{summary}, {message}"
    return summary


def _format_cached_pull_request_label(cached_change) -> str | None:
    if cached_change is None or cached_change.pr_number is None:
        return None

    label = format_pull_request_label(
        cached_change.pr_number,
        is_draft=bool(cached_change.pr_is_draft) and cached_change.pr_state == "open",
        prefix="saved ",
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


def _wrap_advisory(message: str) -> str:
    return textwrap.fill(
        message,
        width=80,
        initial_indent="- ",
        subsequent_indent="  ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def _status_revision_label(revision) -> str:
    return f"[{display_change_id(revision.change_id)}]"


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
            return "GitHub no longer reports a pull request for this branch"
        return f"{cached_label} is no longer present on GitHub for this branch"
    if lookup.state == "closed":
        pull_request = lookup.pull_request
        if pull_request is None:
            raise AssertionError("Closed pull request advisory requires a pull request.")
        return (
            f"PR #{pull_request.number} is {pull_request.state}; submit will not reuse a "
            "closed review automatically"
        )
    raise AssertionError(f"Unexpected link advisory state: {lookup.state}")
