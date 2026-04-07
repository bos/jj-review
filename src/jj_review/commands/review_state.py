"""Show how the selected jj stack currently appears on GitHub.

This reports the pull requests and GitHub branches jj-review is using for each
change without changing anything. By default it starts with capped submitted
and unsubmitted summaries, then prints the trunk/base footer through the same
native `jj log` rendering path; `--verbose` expands the summary sections. In
interactive terminals, GitHub inspection also shows a progress bar on stderr
while the final summaries are prepared.
"""

from __future__ import annotations

import sys
import textwrap
from contextlib import contextmanager
from pathlib import Path

from tqdm import tqdm

from jj_review.bootstrap import bootstrap_context
from jj_review.errors import CliError
from jj_review.intent import intent_change_ids, pid_is_alive
from jj_review.jj import UnsupportedStackError
from jj_review.review_inspection import prepare_status, stream_status
from jj_review.stack_output import (
    display_change_id,
    format_pull_request_label,
    render_revision_with_suffix_lines,
    strip_revision_bookmark_from_rendered_lines,
)

_SUMMARY_SECTION_HEAD_COUNT = 3
_SUMMARY_SECTION_TAIL_COUNT = 3

HELP = "Check the review status of a jj stack"

_strip_revision_bookmark_from_rendered_lines = strip_revision_bookmark_from_rendered_lines


def status(
    *,
    config_path: Path | None,
    debug: bool,
    fetch: bool,
    repository: Path | None,
    revset: str | None,
    verbose: bool,
) -> int:
    """CLI entrypoint for `status`."""

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    try:
        prepared_status = prepare_status(
            change_overrides=context.config.change,
            config=context.config.repo,
            fetch_remote_state=fetch,
            repo_root=context.repo_root,
            revset=revset,
        )
    except UnsupportedStackError as error:
        raise CliError(describe_status_preparation_error(error)) from error

    selection_lines = render_status_selection_lines(prepared_status=prepared_status)
    for line in selection_lines:
        print(line)

    with _status_progress_bar(prepared_status=prepared_status) as progress:
        def advance_progress(_revision, _github_available: bool) -> None:
            if progress is not None:
                progress.update(1)

        result = stream_status(
            on_revision=advance_progress if progress is not None else None,
            prepared_status=prepared_status,
        )

    github_lines = render_status_github_lines(
        github_error=result.github_error,
        github_repository=result.github_repository,
        has_revisions=bool(result.revisions),
    )
    for line in github_lines:
        print(line)

    color_when = prepared_status.prepared.client.resolve_color_when(
        stdout_is_tty=sys.stdout.isatty()
    )

    if not prepared_status.prepared.status_revisions:
        for line in render_empty_status_lines(
            color_when=color_when,
            prepared_status=prepared_status,
            configured_trunk_branch=context.config.repo.trunk_branch,
        ):
            print(line)
        return 0

    github_available = result.github_repository is not None and result.github_error is None
    for line in render_status_summary_lines(
        client=prepared_status.prepared.client,
        color_when=color_when,
        result=result,
        github_available=github_available,
        leading_separator=bool(selection_lines or github_lines),
        verbose=verbose,
    ):
        print(line)
    for line in render_trunk_status_lines(
        color_when=color_when,
        prepared=prepared_status.prepared,
        configured_trunk_branch=context.config.repo.trunk_branch,
    ):
        print(line)
    for line in render_status_advisory_lines(result=result):
        print(line)
    for line in render_status_intent_lines(prepared_status=prepared_status):
        print(line)

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


def render_status_selection_lines(*, prepared_status) -> tuple[str, ...]:
    """Render exceptional local selection context lines."""

    prepared = prepared_status.prepared
    lines: list[str] = []
    if prepared.remote is None:
        if prepared.remote_error is None:
            lines.append("Selected remote: unavailable")
        else:
            lines.append(f"Selected remote: unavailable ({prepared.remote_error})")
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
        if github_error is None and not has_revisions:
            lines.append(
                f"GitHub target: {github_repository} (not inspected; no reviewable commits)"
            )
        elif github_error is not None:
            lines.append(f"GitHub target: {github_repository} ({github_error})")
    return tuple(lines)


def render_status_revision_line(revision, *, github_available: bool) -> str:
    """Render one streamed status row."""

    summary = _format_status_summary(revision, github_available=github_available)
    return f"- {revision.subject} [{display_change_id(revision.change_id)}]: {summary}"


def render_status_summary_lines(
    *,
    client,
    color_when: str,
    github_available: bool,
    leading_separator: bool,
    result,
    verbose: bool,
) -> tuple[str, ...]:
    """Render capped submitted and unsubmitted summaries before the trunk row."""

    unsubmitted_revisions = tuple(
        revision
        for revision in result.revisions
        if _classify_revision_for_summary(revision, github_available=github_available)
        == "unsubmitted"
    )
    submitted_revisions = tuple(
        revision
        for revision in result.revisions
        if _classify_revision_for_summary(revision, github_available=github_available)
        == "submitted"
    )

    lines: list[str] = []
    unsubmitted_lines = _render_summary_section(
        "Unsubmitted changes",
        include_leading_separator=leading_separator,
        revisions=unsubmitted_revisions,
        verbose=verbose,
        renderer=lambda revision: _render_summary_revision_lines(
            client=client,
            color_when=color_when,
            revision=revision,
            github_available=github_available,
            show_status=False,
        ),
    )
    if unsubmitted_lines:
        lines.extend(unsubmitted_lines)

    submitted_lines = _render_summary_section(
        _render_submitted_section_title(submitted_revisions),
        include_leading_separator=False,
        revisions=submitted_revisions,
        verbose=verbose,
        renderer=lambda revision: _render_summary_revision_lines(
            client=client,
            color_when=color_when,
            revision=revision,
            github_available=github_available,
            show_status=True,
        ),
    )
    if submitted_lines:
        if lines:
            lines.append("")
        lines.extend(submitted_lines)
    if lines:
        lines.append("")
    return tuple(lines)


def render_trunk_status_lines(
    *,
    color_when: str,
    prepared,
    configured_trunk_branch: str | None,
) -> tuple[str, ...]:
    """Render the trunk footer with native `jj log` formatting."""

    del configured_trunk_branch
    trunk = prepared.stack.trunk
    return tuple(
        prepared.client.render_revision_log_lines(
            trunk,
            color_when=color_when,
        )
    )


def render_empty_status_lines(
    *,
    color_when: str,
    configured_trunk_branch: str | None,
    prepared_status,
) -> tuple[str, ...]:
    """Render the empty-stack footer and explanation."""

    return (
        *render_trunk_status_lines(
            color_when=color_when,
            prepared=prepared_status.prepared,
            configured_trunk_branch=configured_trunk_branch,
        ),
        "No reviewable commits between the selected revision and `trunk()`.",
    )


def _render_summary_section(
    title: str,
    *,
    include_leading_separator: bool,
    revisions: tuple,
    renderer,
    verbose: bool,
) -> tuple[str, ...]:
    """Render one capped summary section."""

    if not revisions and not verbose:
        return ()

    lines = [f"{title}:"]
    if include_leading_separator:
        lines.insert(0, "")
    if not revisions:
        lines.append("  (none)")
        return tuple(lines)

    rendered = [renderer(revision) for revision in revisions]
    if verbose or len(rendered) <= _SUMMARY_SECTION_HEAD_COUNT + _SUMMARY_SECTION_TAIL_COUNT + 1:
        for block in rendered:
            lines.extend(block)
        return tuple(lines)

    omitted = len(rendered) - _SUMMARY_SECTION_HEAD_COUNT - _SUMMARY_SECTION_TAIL_COUNT
    for block in rendered[:_SUMMARY_SECTION_HEAD_COUNT]:
        lines.extend(block)
    lines.append(f"  [...{omitted} changes omitted...]")
    for block in rendered[-_SUMMARY_SECTION_TAIL_COUNT:]:
        lines.extend(block)
    return tuple(lines)


def _render_submitted_section_title(revisions: tuple) -> str:
    """Render the submitted-section heading, linking the newest submitted PR when possible."""

    top_pull_request_url = _revision_pull_request_url(revisions[0]) if revisions else None
    if top_pull_request_url is None:
        return "Submitted changes"
    return f"Submitted changes ({top_pull_request_url})"


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


def _render_summary_revision_lines(
    *,
    client,
    color_when: str,
    revision,
    github_available: bool,
    show_status: bool,
) -> tuple[str, ...]:
    """Render one revision inside a submitted or unsubmitted summary section."""

    summary = _format_status_summary(revision, github_available=github_available)
    if not show_status and summary == "not submitted":
        summary = None
    return render_revision_with_suffix_lines(
        client=client,
        color_when=color_when,
        revision=revision,
        bookmark=revision.bookmark,
        suffix=summary,
    )


def _revision_pull_request_url(revision) -> str | None:
    """Return the GitHub URL for a revision's linked pull request when one is available."""

    lookup = revision.pull_request_lookup
    if lookup is None or lookup.pull_request is None:
        return None
    return getattr(lookup.pull_request, "html_url", None)


@contextmanager
def _status_progress_bar(*, prepared_status):
    """Render a TTY-only progress bar while GitHub inspection runs."""

    if (
        prepared_status.github_repository is None
        or not prepared_status.prepared.status_revisions
        or not sys.stderr.isatty()
    ):
        yield None
        return

    with tqdm(
        total=len(prepared_status.prepared.status_revisions),
        desc="Inspecting GitHub",
        dynamic_ncols=True,
        file=sys.stderr,
        leave=False,
        unit="change",
    ) as progress:
        yield progress


def _classify_revision_for_summary(
    revision,
    *,
    github_available: bool,
) -> str:
    """Classify a revision into submitted, unsubmitted, or other."""

    if getattr(revision, "link_state", "active") == "unlinked":
        return "submitted"

    lookup = revision.pull_request_lookup
    if lookup is None:
        if _has_cached_pull_request(revision.cached_change):
            return "submitted"
        return "unsubmitted"

    if lookup.state in {"open", "closed"}:
        return "submitted"
    if lookup.state == "missing":
        return "submitted" if _has_cached_pull_request(revision.cached_change) else "unsubmitted"
    if lookup.state in {"ambiguous", "error"}:
        return "submitted" if _has_cached_pull_request(revision.cached_change) else "unsubmitted"
    return "unsubmitted"


def _has_cached_pull_request(cached_change) -> bool:
    return cached_change is not None and cached_change.pr_number is not None


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
