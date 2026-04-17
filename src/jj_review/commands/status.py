"""Show how the selected jj stack currently appears on GitHub.

This reports the pull requests and review branches jj-review is using for each
change without changing anything. By default it shows a summary of submitted
and unsubmitted changes above the trunk row; `--verbose` expands those
summaries. In interactive terminals a progress bar appears while GitHub is
being queried.
"""

from __future__ import annotations

import sys
from pathlib import Path

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.console import requested_color_mode
from jj_review.errors import ErrorMessage
from jj_review.formatting import (
    format_change_marker,
    format_pull_request_label,
    format_status_annotation,
    render_revision_lines,
    render_revision_with_suffix_lines,
)
from jj_review.jj import UnsupportedStackError
from jj_review.models.intent import (
    AbortIntent,
    CleanupIntent,
    CleanupRestackIntent,
    CloseIntent,
    LandIntent,
    RelinkIntent,
    SubmitIntent,
)
from jj_review.review.intents import (
    match_cleanup_restack_intent,
    match_close_intent,
)
from jj_review.review.status import (
    prepare_status,
    revision_has_merged_pull_request,
    revision_pull_request_number,
    status_preparation_cli_error,
    stream_status,
)
from jj_review.review.submit_recovery import (
    SubmitRecoveryIdentity,
    SubmitStatusDecision,
    submit_status_decision,
)
from jj_review.system import pid_is_alive

_SUMMARY_SECTION_HEAD_COUNT = 3
_SUMMARY_SECTION_TAIL_COUNT = 3

HELP = "Check the review status of a jj stack"


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
        raise status_preparation_cli_error(error) from error

    selection_lines = render_status_selection_lines(prepared_status=prepared_status)
    if selection_lines:
        _emit_lines(selection_lines, emitter=console.warning)

    github_repository = getattr(prepared_status, "github_repository", None)
    progress_total = (
        len(prepared_status.prepared.status_revisions) if github_repository is not None else 0
    )
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        result = stream_status(
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )

    github_lines = render_status_github_lines(
        github_error=result.github_error,
        github_repository=result.github_repository,
        has_revisions=bool(result.revisions),
    )
    if result.github_error is not None:
        _emit_lines(github_lines, emitter=console.warning, soft_wrap=False)
    else:
        _emit_lines(github_lines)

    if not prepared_status.prepared.status_revisions:
        _emit_lines(
            render_empty_status_lines(
                prepared_status=prepared_status,
            )
        )
        return 0

    github_available = result.github_repository is not None and result.github_error is None
    prerendered_blocks = _prefetch_revision_log_blocks(
        client=prepared_status.prepared.client,
        revisions=result.revisions,
        trunk=prepared_status.prepared.stack.trunk,
    )
    _emit_lines(
        render_status_summary_lines(
            client=prepared_status.prepared.client,
            result=result,
            github_available=github_available,
            leading_separator=bool(selection_lines or github_lines),
            verbose=verbose,
            prerendered_blocks=prerendered_blocks,
        )
    )
    _emit_lines(
        render_trunk_status_lines(
            prepared=prepared_status.prepared,
            prerendered_blocks=prerendered_blocks,
        )
    )
    _emit_lines(render_status_advisory_lines(result=result))
    _emit_lines(render_status_intent_lines(prepared_status=prepared_status))

    exit_code = 1 if result.incomplete else 0
    if any(
        _interrupted_intent_blocks_status(
            loaded=loaded,
            prepared_status=prepared_status,
        )
        for loaded in prepared_status.outstanding_intents
    ):
        exit_code = max(exit_code, 1)
    return exit_code
def render_status_selection_lines(*, prepared_status) -> tuple[object, ...]:
    """Render exceptional local selection context lines."""

    prepared = prepared_status.prepared
    lines: list[object] = []
    if prepared.remote is None:
        if prepared.remote_error is None:
            lines.append(_prefixed_status_line("Selected remote", "unavailable"))
        else:
            lines.append(
                _prefixed_status_line(
                    "Selected remote",
                    ("unavailable (", prepared.remote_error, ")"),
                )
            )
    return tuple(lines)


def render_status_github_lines(
    *,
    github_error: ErrorMessage | None,
    github_repository: str | None,
    has_revisions: bool,
) -> tuple[object, ...]:
    """Render GitHub availability lines as status streaming begins."""

    lines: list[object] = []
    if github_error is not None:
        if github_repository is None:
            lines.append(t"GitHub target error: {github_error}")
        else:
            lines.append(t"GitHub target: {github_repository} (error: {github_error})")
    elif github_repository is not None and not has_revisions:
        lines.append(
            _prefixed_status_line(
                "GitHub target",
                f"{github_repository} (not inspected; no reviewable commits)",
            )
        )
    return tuple(lines)


def render_status_summary_lines(
    *,
    client,
    github_available: bool,
    leading_separator: bool,
    result,
    verbose: bool,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
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
        "Unsubmitted stack",
        include_leading_separator=leading_separator,
        revisions=unsubmitted_revisions,
        verbose=verbose,
        renderer=lambda revision: _render_summary_revision_lines(
            client=client,
            revision=revision,
            github_available=github_available,
            show_status=False,
            prerendered_blocks=prerendered_blocks,
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
            revision=revision,
            github_available=github_available,
            show_status=True,
            prerendered_blocks=prerendered_blocks,
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
    prepared,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Render the trunk footer with native `jj log` formatting."""

    trunk = prepared.stack.trunk
    return render_revision_lines(
        client=prepared.client,
        revision=trunk,
        prerendered_lines=(
            prerendered_blocks.get(trunk.commit_id) if prerendered_blocks else None
        ),
    )


def render_empty_status_lines(
    *,
    prepared_status,
) -> tuple[object, ...]:
    """Render the empty-stack footer and explanation."""

    return (
        *render_trunk_status_lines(
            prepared=prepared_status.prepared,
        ),
        (
            "No reviewable commits between the selected revision and ",
            ui.revset("trunk()"),
            ".",
        ),
    )


def _prefetch_revision_log_blocks(
    *,
    client,
    revisions,
    trunk,
) -> dict[str, tuple[str, ...]]:
    """Render the `jj log` block for every revision we will print, in parallel."""

    seen: set[str] = set()
    ordered: list[object] = []
    for revision in (*revisions, trunk):
        if revision.commit_id in seen:
            continue
        seen.add(revision.commit_id)
        ordered.append(revision)
    color_when = client.resolve_color_when(
        cli_color=requested_color_mode(),
        stdout_is_tty=sys.stdout.isatty(),
    )
    return client.render_revision_log_blocks(ordered, color_when=color_when)


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
    lines.append(f"   ... {omitted} changes omitted ...")
    for block in rendered[-_SUMMARY_SECTION_TAIL_COUNT:]:
        lines.extend(block)
    return tuple(lines)


def _render_submitted_section_title(revisions: tuple) -> str:
    """Render the submitted-section heading, linking the newest submitted PR when possible."""

    if revisions:
        _lookup = revisions[0].pull_request_lookup
        top_pull_request_url = (
            getattr(_lookup.pull_request, "html_url", None)
            if _lookup is not None and _lookup.pull_request is not None
            else None
        )
    else:
        top_pull_request_url = None
    if top_pull_request_url is None:
        return "Submitted stack"
    return f"Submitted stack ({top_pull_request_url})"


def render_status_advisory_lines(*, result) -> tuple[object, ...]:
    """Render any advisories that follow the status stack output."""

    if not hasattr(result, "revisions") or not hasattr(result, "selected_revset"):
        return ()

    cleanup_revisions = [
        revision for revision in result.revisions if revision_has_merged_pull_request(revision)
    ]
    divergent_revisions = [
        revision
        for revision in result.revisions
        if getattr(revision, "local_divergent", False)
        and not revision_has_merged_pull_request(revision)
    ]
    link_revisions = [
        revision for revision in result.revisions if _revision_has_link_advisory(revision)
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

    lines: list[object] = ["", "Advisories:"]
    if cleanup_revisions:
        lines.append(
            _wrap_advisory(
                "Submit note: descendant PR bases still follow the old local ancestry "
                "until the stack is restacked"
            )
        )
        lines.append(
            _wrap_advisory(
                t"Next step: run {ui.cmd('jj-review cleanup --restack')} "
                t"{ui.revset(result.selected_revset)} to rewrite the local stack, or "
                t"{ui.cmd('jj-review cleanup --restack --dry-run')} to preview the "
                t"restack plan first"
            )
        )
        for revision in cleanup_revisions:
            pull_request_number = revision_pull_request_number(revision)
            pull_request_label = (
                f"PR #{pull_request_number}" if pull_request_number is not None else "merged PR"
            )
            lines.append(
                _wrap_advisory(
                    (
                        _status_revision_label(revision),
                        ": ",
                        pull_request_label,
                        " is merged, and later local changes are still based on it",
                    )
                )
            )

    if link_revisions:
        lines.append(
            _wrap_advisory(
                t"PR link note: refresh remote and GitHub observations with "
                t"{ui.cmd('jj-review status --fetch')} {ui.revset(result.selected_revset)}. "
                t"If the existing PR should stay attached to one of these changes, repair "
                t"that PR link intentionally with {ui.cmd('jj-review relink <pr>')} "
                t"{ui.revset(result.selected_revset)}."
            )
        )
        for revision in link_revisions:
            lines.append(
                _wrap_advisory(
                    (
                        _status_revision_label(revision),
                        ": ",
                        _describe_link_advisory(revision),
                    )
                )
            )

    for revision in policy_warnings:
        base_ref = revision.pull_request_lookup.pull_request.base.ref
        pull_request_number = revision_pull_request_number(revision)
        lines.append(
            _wrap_advisory(
                t"Repository policy warning: PR #{pull_request_number} merged into "
                t"{ui.bookmark(base_ref)}; configure GitHub to block merges of PRs "
                t"targeting {ui.bookmark('review/*')}"
            )
        )

    for revision in divergent_revisions:
        lines.append(
            _wrap_advisory(
                t"{_status_revision_label(revision)}: resolve the multiple visible "
                t"revisions for this change before retrying "
                t"({ui.cmd('jj log -r')} {ui.revset(f'change_id({revision.change_id})')})"
            )
        )
    return tuple(lines)


def render_status_intent_lines(*, prepared_status) -> tuple[object, ...]:
    """Render any stale or incomplete operation notices."""

    lines: list[object] = []
    if prepared_status.stale_intents:
        lines.extend(("", "Stale incomplete operations (change IDs no longer in repo):"))
        for loaded in prepared_status.stale_intents:
            alive = pid_is_alive(loaded.intent.pid)
            status_str = "process alive" if alive else "process dead"
            lines.append(
                _prefixed_intent_line(
                    _render_intent_description(loaded.intent),
                    format_status_annotation(f"{status_str}, {loaded.path.name}"),
                )
            )

    if prepared_status.outstanding_intents:
        lines.extend(("", "Interrupted operations recorded:"))
        for loaded in prepared_status.outstanding_intents:
            alive = pid_is_alive(loaded.intent.pid)
            description = _render_intent_description(loaded.intent)
            if alive:
                lines.append(
                    _prefixed_intent_line(
                        description,
                        format_status_annotation(f"in progress, PID {loaded.intent.pid}"),
                    )
                )
            elif isinstance(loaded.intent, SubmitIntent):
                lines.append(
                    _prefixed_intent_line(
                        description,
                        _render_interrupted_submit_status_line(
                            intent=loaded.intent,
                            prepared_status=prepared_status,
                        ),
                    )
                )
            elif isinstance(loaded.intent, CleanupRestackIntent):
                lines.append(
                    _prefixed_intent_line(
                        description,
                        _render_interrupted_cleanup_restack_status_line(
                            intent=loaded.intent,
                            prepared_status=prepared_status,
                        ),
                    )
                )
            elif isinstance(loaded.intent, CloseIntent):
                lines.append(
                    _prefixed_intent_line(
                        description,
                        _render_interrupted_close_status_line(
                            intent=loaded.intent,
                            prepared_status=prepared_status,
                        ),
                    )
                )
            elif isinstance(loaded.intent, RelinkIntent):
                lines.append(
                    _prefixed_intent_line(
                        description,
                        (
                            "interrupted, inspect before rerunning ",
                            ui.cmd("relink"),
                            " again",
                        ),
                    )
                )
            elif isinstance(loaded.intent, CleanupIntent):
                lines.append(
                    _prefixed_intent_line(
                        description,
                        (
                            "interrupted, inspect before rerunning ",
                            ui.cmd("cleanup"),
                            " again",
                        ),
                    )
                )
            elif isinstance(loaded.intent, AbortIntent):
                lines.append(
                    _prefixed_intent_line(
                        description,
                        (
                            "interrupted, inspect before rerunning ",
                            ui.cmd("abort"),
                            " again",
                        ),
                    )
                )
            elif isinstance(loaded.intent, LandIntent):
                lines.append(
                    _prefixed_intent_line(
                        description,
                        (
                            "interrupted, inspect before rerunning ",
                            ui.cmd("land"),
                            " again",
                        ),
                    )
                )
            else:
                lines.append(
                    _prefixed_intent_line(
                        description,
                        format_status_annotation("interrupted, inspect before re-running"),
                    )
                )
    return tuple(lines)


def _interrupted_intent_blocks_status(*, loaded, prepared_status) -> bool:
    """Return True when an interrupted intent should make `status` exit nonzero."""

    if pid_is_alive(loaded.intent.pid):
        return True

    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )

    if isinstance(loaded.intent, SubmitIntent):
        decision = submit_status_decision(
            intent=loaded.intent,
            current_change_ids=current_change_ids,
            current_commit_ids=current_commit_ids,
            current_identity=_current_submit_identity(prepared_status=prepared_status),
        )
        return decision is SubmitStatusDecision.INSPECT

    if isinstance(loaded.intent, CleanupRestackIntent):
        return (
            match_cleanup_restack_intent(
                intent=loaded.intent,
                current_change_ids=current_change_ids,
                current_commit_ids=current_commit_ids,
            )
            == "overlap"
        )

    if isinstance(loaded.intent, CloseIntent):
        return (
            match_close_intent(
                intent=loaded.intent,
                current_change_ids=current_change_ids,
                current_commit_ids=current_commit_ids,
            )
            == "overlap"
        )

    current_change_id_set = set(current_change_ids)
    return bool(loaded.intent.change_ids() & current_change_id_set)


def _render_interrupted_submit_status_line(
    *,
    intent: SubmitIntent,
    prepared_status,
) -> object:
    rerun_command = _render_rerun_command(
        command="submit",
        revset=prepared_status.selected_revset,
    )
    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    decision = submit_status_decision(
        intent=intent,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
        current_identity=_current_submit_identity(prepared_status=prepared_status),
    )
    if decision is SubmitStatusDecision.CONTINUE:
        status = (
            "interrupted, rerun ",
            rerun_command,
            " to continue on the current stack",
        )
    elif decision is SubmitStatusDecision.CURRENT_STACK:
        status = (
            "interrupted, recorded stack was rewritten; rerunning ",
            rerun_command,
            " will submit the current stack",
        )
    elif decision is SubmitStatusDecision.INSPECT:
        status = (
            "interrupted, current stack matches but the recorded submit target "
            "does not; inspect before running ",
            rerun_command,
            " again",
        )
    else:
        status = "interrupted, recorded stack differs from the current selection"
    return status


def _current_submit_identity(*, prepared_status) -> SubmitRecoveryIdentity | None:
    current_remote = prepared_status.prepared.remote
    current_github_repository = prepared_status.github_repository
    if current_remote is None or current_github_repository is None:
        return None
    return SubmitRecoveryIdentity.from_github_repository(
        remote_name=current_remote.name,
        github_repository=current_github_repository,
    )


def _render_rerun_command(*, command: str, revset: str) -> tuple[object, ...]:
    """Render an explicit rerun command for the current selection."""

    return (
        ui.cmd(command),
        " ",
        ui.revset(revset),
    )


def _render_interrupted_cleanup_restack_status_line(
    *,
    intent: CleanupRestackIntent,
    prepared_status,
) -> object:
    rerun_command = _render_rerun_command(
        command="cleanup --restack",
        revset=prepared_status.selected_revset,
    )
    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    match = match_cleanup_restack_intent(
        intent=intent,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )
    if match == "exact":
        status = (
            "interrupted, rerun ",
            rerun_command,
            " to continue on the current stack",
        )
    elif match == "same-logical":
        status = (
            "interrupted, recorded stack was rewritten; rerunning ",
            rerun_command,
            " will use the current stack",
        )
    elif match == "covered":
        status = (
            "interrupted, the recorded changes are all included in the current stack; ",
            "rerunning ",
            rerun_command,
            " will use the current stack",
        )
    elif match == "trimmed":
        status = (
            "interrupted, the recorded stack still includes changes that are no "
            "longer on the current stack; ",
            "rerunning ",
            rerun_command,
            " will use the current stack",
        )
    elif match == "overlap":
        status = (
            "interrupted, current stack differs; inspect before running ",
            rerun_command,
            " again",
        )
    else:
        status = "interrupted, recorded stack differs from the current selection"
    return status


def _render_interrupted_close_status_line(
    *,
    intent: CloseIntent,
    prepared_status,
) -> object:
    rerun_command = _render_rerun_command(
        command="close --cleanup" if intent.cleanup else "close",
        revset=prepared_status.selected_revset,
    )
    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    match = match_close_intent(
        intent=intent,
        current_change_ids=current_change_ids,
        current_commit_ids=current_commit_ids,
    )
    if match == "exact":
        status = (
            "interrupted, rerun ",
            rerun_command,
            " to continue on the current stack",
        )
    elif match == "same-logical":
        status = (
            "interrupted, recorded stack was rewritten; rerunning ",
            rerun_command,
            " will use the current stack",
        )
    elif match == "covered":
        status = (
            "interrupted, the recorded changes are all included in the current stack; ",
            "rerunning ",
            rerun_command,
            " will use the current stack",
        )
    elif match == "overlap":
        status = (
            "interrupted, current stack differs; inspect before running ",
            rerun_command,
            " again",
        )
    else:
        status = "interrupted, recorded stack differs from the current selection"
    return status


def _render_summary_revision_lines(
    *,
    client,
    revision,
    github_available: bool,
    show_status: bool,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[str, ...]:
    """Render one revision inside a submitted or unsubmitted summary section."""

    summary = _format_status_summary(revision, github_available=github_available)
    if not show_status and summary == "not submitted":
        summary = None
    return render_revision_with_suffix_lines(
        client=client,
        revision=revision,
        bookmark=revision.bookmark,
        suffix=summary,
        prerendered_lines=(
            prerendered_blocks.get(revision.commit_id) if prerendered_blocks else None
        ),
    )


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
        review_decision = getattr(lookup, "review_decision", None)
        if review_decision is None:
            if getattr(lookup, "review_decision_error", None) is None or cached_change is None:
                review_decision = None
            else:
                review_decision = cached_change.pr_review_decision
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

    if getattr(revision, "local_divergent", False) and not revision_has_merged_pull_request(
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


def _emit_lines(
    lines: tuple[object, ...], *, emitter=console.output, soft_wrap: bool = True
) -> None:
    for line in lines:
        if isinstance(line, str) and "\x1b[" in line:
            emitter(console.ansi_text(line), soft_wrap=soft_wrap)
            continue
        emitter(line, soft_wrap=soft_wrap)


def _prefixed_status_line(prefix: str, body: object) -> object:
    return ui.prefixed_line(
        f"{prefix}: ",
        body,
        prefix_labels=("prefix",),
    )


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
        _rd = cached_change.pr_review_decision
        details.append("changes requested" if _rd == "changes_requested" else _rd)
    return f"{label} ({', '.join(details)})"


def _wrap_advisory(message: object) -> object:
    return ui.prefixed_line("- ", message)


def _prefixed_intent_line(description: object, status: object) -> object:
    return ui.prefixed_line("  ", (description, "  ", status))


def _render_intent_description(intent) -> object:
    if isinstance(intent, SubmitIntent):
        return (
            t"{ui.cmd('submit')} for {ui.change_id(intent.head_change_id)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, CleanupRestackIntent):
        head_change_id = intent.ordered_change_ids[-1] if intent.ordered_change_ids else "stack"
        return (
            t"{ui.cmd('cleanup --restack')} for {ui.change_id(head_change_id)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, CloseIntent):
        head_change_id = intent.ordered_change_ids[-1] if intent.ordered_change_ids else "stack"
        return (
            t"{ui.cmd('close --cleanup' if intent.cleanup else 'close')} "
            t"for {ui.change_id(head_change_id)} "
            t"(from {ui.revset(intent.display_revset)})"
        )
    if isinstance(intent, LandIntent):
        head_change_id = intent.ordered_change_ids[-1] if intent.ordered_change_ids else "stack"
        return t"{ui.cmd('land')} for {ui.change_id(head_change_id)} " \
            t"(from {ui.revset(intent.display_revset)})"
    if isinstance(intent, RelinkIntent):
        return t"{ui.cmd('relink')} for {ui.change_id(intent.change_id)}"
    if isinstance(intent, CleanupIntent):
        return ui.cmd("cleanup")
    if isinstance(intent, AbortIntent):
        return ui.cmd("abort")
    return getattr(intent, "label", "operation")


def _status_revision_label(revision) -> str:
    return format_change_marker(revision.change_id)


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
