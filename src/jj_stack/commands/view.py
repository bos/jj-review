"""Compare the local and GitHub state of the selected `jj` stacks.

By default it summarizes the submitted and unsubmitted changes in each selected stack;
`--verbose` expands those summaries.

It reads pull request state from GitHub, but finds the changes in a stack locally by walking
parents from the selected change down to your local `trunk()`. If your local copy of trunk is
behind, that list of changes can be stale even though the pull request state is current. Run
`jj git fetch` first when the view needs to reflect the latest trunk. Mix revsets and repeated
`--pull-request` values to inspect several stacks in one run.

Common examples:

- `jj-stack view` inspects the stack ending at `@` when the working-copy change is described and
  nonempty, otherwise `@-`.

- `jj-stack view --pull-request 123` finds the full local stack containing that PR.

- `jj-stack view <change-id>` finds the full local stack containing that change.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from rich.text import Text

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.commands._json_status import stack_change_json
from jj_stack.errors import EXIT_INCOMPLETE, CliError, error_message
from jj_stack.formatting import (
    CommitRenderClient,
    RenderableCommit,
    format_pr_label,
    render_commit_blocks,
    render_commit_lines,
)
from jj_stack.github.error_messages import remote_and_github_unavailable_messages
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import (
    JjCommandError,
    UnsupportedStackError,
    divergent_change_id_from_error,
)
from jj_stack.models.github import GithubPR
from jj_stack.models.tracking import PRIdentity
from jj_stack.pr_branch_namespace import current_pr_branch_namespace
from jj_stack.stack.change_state import (
    ChangeState,
    Closed,
    CompetingOpenPR,
    Landed,
    LookupFailed,
    Merged,
    NotInspected,
    PRAmbiguous,
    PRHeadMoved,
    PRIdentityMismatch,
    PRMissing,
    WithPR,
)
from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.stack.selected import is_change_id_prefix
from jj_stack.stack.selection import (
    resolve_linked_change_for_pr,
    resolve_selected_revset,
)
from jj_stack.stack.status import (
    PreparedStack,
    PreparedStatus,
    StackStatusChange,
    StatusResult,
    prepare_status,
    status_preparation_cli_error,
    stream_status,
)

_SUMMARY_SECTION_HEAD_COUNT = 3
_SUMMARY_SECTION_TAIL_COUNT = 3

HELP = "Check the PR status of one or more `jj` stacks"

ViewSelectorKind = Literal["pr", "revset"]


@dataclass(frozen=True, slots=True)
class ViewSelector:
    """One explicit selector from the `view` command line."""

    kind: ViewSelectorKind
    value: str


@dataclass(frozen=True, slots=True)
class _ResolvedViewSelector:
    note: ui.Message | None
    revset: str | None
    containing_change_id: str | None


def view(
    *,
    as_json: bool,
    cli_args: JjCliArgs,
    debug: bool,
    pr: str | Sequence[str] | None,
    repo: Path | None,
    revset: str | Sequence[str] | None,
    selectors: Sequence[ViewSelector] | None = None,
    verbose: bool,
) -> int:
    """CLI entrypoint for `view`."""

    context = bootstrap_context(
        repo=repo,
        cli_args=cli_args,
        debug=debug,
    )
    return _run_status(
        context=context,
        selectors=_normalize_status_selectors(
            pr=pr,
            revset=revset,
            selectors=selectors,
        ),
        as_json=as_json,
        verbose=verbose,
    )


def _run_status(
    *,
    as_json: bool,
    context: CommandContext,
    selectors: tuple[ViewSelector, ...],
    verbose: bool,
) -> int:
    if not selectors:
        prepared_status = _prepare_status_with_spinner(
            context=context,
            revset=None,
        )
        if as_json:
            rendered, incomplete = _json_prepared_status(
                prepared_status=prepared_status,
            )
            console.machine_output(json.dumps(_view_json_payload(stacks=(rendered,)), indent=2))
            return EXIT_INCOMPLETE if incomplete else 0
        exit_code = _render_prepared_status(
            prepared_status=prepared_status,
            verbose=verbose,
        )
        return exit_code

    exit_code = 0
    multi_selector = len(selectors) > 1
    rendered_stack_keys: set[tuple[str, ...]] = set()
    json_stacks: list[dict[str, object]] = []
    printed_blocks = 0
    for selector in selectors:
        try:
            resolved_selector = _resolve_status_selector(
                context=context,
                selector=selector,
            )
            prepared_status = _prepare_status_with_spinner(
                containing_change_id=resolved_selector.containing_change_id,
                context=context,
                revset=resolved_selector.revset,
            )
        except CliError as error:
            if not multi_selector:
                # A single selector that yields no report matches the bare-view
                # behavior: fail with the error's category code instead of
                # degrading to an incomplete report.
                raise
            if not as_json and printed_blocks:
                console.output("")
            if not as_json:
                console.output(_status_heading(selector))
            console.warning(ui.prefixed_line("Error: ", error_message(error)))
            hint = error.hint
            if hint is not None:
                console.warning(ui.prefixed_line("Hint: ", hint))
            exit_code = EXIT_INCOMPLETE
            if not as_json:
                printed_blocks += 1
            continue

        change_ids = tuple(
            change.change.change_id for change in prepared_status.prepared.status_changes
        )
        stack_key = (prepared_status.prepared.stack.base_parent.commit_id, *change_ids)
        if stack_key in rendered_stack_keys:
            continue
        rendered_stack_keys.add(stack_key)
        if as_json:
            rendered, incomplete = _json_prepared_status(
                prepared_status=prepared_status,
                selector=selector,
            )
            json_stacks.append(rendered)
            exit_code = max(exit_code, EXIT_INCOMPLETE if incomplete else 0)
            continue

        if printed_blocks:
            console.output("")
        if multi_selector:
            console.output(_status_heading(selector))
        if resolved_selector.note is not None:
            console.note(resolved_selector.note)
        exit_code = max(
            exit_code,
            _render_prepared_status(
                prepared_status=prepared_status,
                verbose=verbose,
            ),
        )
        printed_blocks += 1
    if as_json:
        console.machine_output(
            json.dumps(
                _view_json_payload(
                    stacks=tuple(json_stacks),
                ),
                indent=2,
            )
        )
        return exit_code
    return exit_code


def _normalize_status_selectors(
    *,
    pr: str | Sequence[str] | None,
    revset: str | Sequence[str] | None,
    selectors: Sequence[ViewSelector] | None,
) -> tuple[ViewSelector, ...]:
    if selectors is not None:
        return tuple(selectors)

    ordered: list[ViewSelector] = []
    if pr is not None:
        if isinstance(pr, str):
            ordered.append(ViewSelector(kind="pr", value=pr))
        else:
            ordered.extend(ViewSelector(kind="pr", value=value) for value in pr)
    if revset is not None:
        if isinstance(revset, str):
            ordered.append(ViewSelector(kind="revset", value=revset))
        else:
            ordered.extend(ViewSelector(kind="revset", value=value) for value in revset)
    return tuple(ordered)


def _resolve_status_selector(
    *,
    context: CommandContext,
    selector: ViewSelector,
) -> _ResolvedViewSelector:
    if selector.kind == "pr":
        pr_number, resolved_revset, repo = resolve_linked_change_for_pr(
            jj_client=context.jj_client,
            pr_reference=selector.value,
            revset=None,
        )
        return _ResolvedViewSelector(
            note=t"Using {format_pr_label(pr_number, repo=repo)} for change "
            t"{ui.change_id(resolved_revset)}",
            revset=None,
            containing_change_id=resolved_revset,
        )
    resolved_revset = resolve_selected_revset(
        command_label="view",
        default_revset=None,
        require_explicit=False,
        revset=selector.value,
    )
    containing_change_id = _change_id_selector(
        context=context,
        value=resolved_revset,
    )
    return _ResolvedViewSelector(
        note=None,
        revset=None if containing_change_id is not None else resolved_revset,
        containing_change_id=containing_change_id,
    )


def _change_id_selector(*, context: CommandContext, value: str | None) -> str | None:
    """Recognize a bare change ID without misclassifying a bookmark."""

    if not is_change_id_prefix(value):
        return None
    assert value is not None
    try:
        change = context.jj_client.resolve_commit(value)
    except JjCommandError as error:
        if divergent_change_id_from_error(error) == value:
            return value
        raise
    except CliError as error:
        # A change-ID selector that matched nothing is a selection that does not form a
        # supported local stack, which is what the commit-ID form already reports. Only the
        # base class means that: every subclass `resolve_commit` can raise is a different
        # answer that keeps its own exit code and hint, including a stale workspace, an
        # ambiguous prefix and an unparseable revset. No `reason`: this cannot tell a hidden
        # change from a change ID that never existed.
        if type(error) is not CliError:
            raise
        raise UnsupportedStackError(error.message) from error
    return value if change.change_id.startswith(value) else None


def _prepare_status_for_revset(
    *,
    containing_change_id: str | None = None,
    context: CommandContext,
    revset: str | None,
) -> PreparedStatus:
    try:
        prepared_status = prepare_status(
            context=context,
            containing_change_id=containing_change_id,
            fetch_remote_state=False,
            inspection_mode=True,
            revset=revset,
        )
    except UnsupportedStackError as error:
        raise status_preparation_cli_error(error) from error
    return prepared_status


def _prepare_status_with_spinner(
    *,
    containing_change_id: str | None = None,
    context: CommandContext,
    revset: str | None,
) -> PreparedStatus:
    with console.spinner(description="Inspecting jj stack"):
        prepared_status = _prepare_status_for_revset(
            containing_change_id=containing_change_id,
            context=context,
            revset=revset,
        )
    for warning in _local_history_warnings(prepared_status):
        console.warning(warning)
    return prepared_status


def _local_history_warnings(prepared_status: PreparedStatus) -> tuple[ui.Message, ...]:
    """Describe local states that inspection tolerates but stack mutation rejects."""

    warnings: list[ui.Message] = []
    for change in prepared_status.prepared.stack.changes:
        change_id = ui.change_id(change.change_id)
        if len(change.parents) > 1:
            warnings.append(
                t"Change {change_id} is a merge change. Showing its first-parent path; "
                t"commands that change stack state require a linear stack."
            )
        if change.empty:
            warnings.append(
                t"Change {change_id} is empty. Showing it for inspection; it cannot be "
                t"submitted until it has content."
            )
        elif not change.description.strip():
            warnings.append(
                t"Change {change_id} has no description. Showing it for inspection, but it "
                t"cannot be submitted until it is described with "
                t"{ui.cmd(f'jj describe {short_change_id(change.change_id)}')}."
            )
        if change.divergent:
            warnings.append(
                t"Change {change_id} has divergent local commits. Showing the selected one; "
                t"commands that change the stack will stop until the divergence is resolved."
            )
        if change.conflict:
            warnings.append(
                t"Change {change_id} has unresolved conflicts. Showing it for inspection; "
                t"submit and merge remain blocked until the conflicts are resolved."
            )
    return tuple(warnings)


def _status_heading(selector: ViewSelector) -> ui.Message:
    if selector.kind == "pr":
        return f"Status for PR {selector.value}:"
    return t"Status for {ui.revset(selector.value)}:"


def _json_prepared_status(
    *,
    prepared_status: PreparedStatus,
    selector: ViewSelector | None = None,
) -> tuple[dict[str, object], bool]:
    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        result = stream_status(
            on_change=lambda _change, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    _warn_about_unavailable_github(result)
    return (
        _json_status_result(
            prepared_status=prepared_status,
            result=result,
            selector=selector,
        ),
        result.incomplete,
    )


def _warn_about_unavailable_github(result: StatusResult) -> tuple[ui.Message, ...]:
    """Explain an unreachable remote or GitHub target on stderr in every output mode.

    A `--json` caller reads a well-formed payload on stdout, so the reason for a non-zero
    exit code has nowhere to go but stderr.
    """

    lines = remote_and_github_unavailable_messages(
        github_error=result.github_error,
        github_repo=result.github_repo,
        remote=result.remote,
        remote_error=result.remote_error,
    )
    _emit_lines(lines, emitter=console.warning, soft_wrap=False)
    return lines


def _view_json_payload(
    *,
    stacks: tuple[dict[str, object], ...],
) -> dict[str, object]:
    return {
        "stacks": list(stacks),
    }


def _json_status_result(
    *,
    prepared_status: PreparedStatus,
    result: StatusResult,
    selector: ViewSelector | None,
) -> dict[str, object]:
    stack_model = prepared_status.prepared.stack
    current_change_ids = {
        change.change_id for change in stack_model.changes if change.current_working_copy
    }
    stack: dict[str, object] = {
        "changes": [
            stack_change_json(
                change,
                current=change.change_id in current_change_ids,
            )
            for change in result.changes
        ],
    }
    if selector is not None:
        stack["selector"] = f"PR {selector.value}" if selector.kind == "pr" else selector.value
    return stack


def _render_prepared_status(
    *,
    prepared_status: PreparedStatus,
    verbose: bool,
) -> int:
    progress_total = prepared_status.github_inspection_count()
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        result = stream_status(
            on_change=lambda _change, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    warning_lines = _warn_about_unavailable_github(result)

    if not prepared_status.prepared.status_changes:
        _emit_lines(
            render_empty_status_lines(
                prepared_status=prepared_status,
            )
        )
        return 0

    with console.spinner(description="Rendering jj log"):
        prerendered_blocks = _prefetch_commit_log_blocks(
            client=prepared_status.prepared.client,
            changes=result.changes,
            trunk=prepared_status.prepared.stack.base_parent,
        )
    _emit_lines(
        render_status_summary_lines(
            client=prepared_status.prepared.client,
            result=result,
            leading_separator=bool(warning_lines),
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
    _emit_lines(
        render_status_advisory_lines(
            result=result,
        )
    )

    return EXIT_INCOMPLETE if result.incomplete else 0


def render_status_summary_lines(
    *,
    client,
    leading_separator: bool,
    result,
    verbose: bool,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[ui.Renderable, ...]:
    """Render capped submitted and unsubmitted summaries before the trunk row."""

    unsubmitted_changes = tuple(change for change in result.changes if change.tracked is None)
    submitted_changes = tuple(change for change in result.changes if change.tracked is not None)

    lines: list[ui.Renderable] = []
    unsubmitted_lines = _render_summary_section(
        "Unsubmitted stack",
        include_leading_separator=leading_separator,
        changes=unsubmitted_changes,
        verbose=verbose,
        renderer=lambda change: _render_summary_change_lines(
            change=change,
            client=client,
            repo=result.github_repo,
            show_status=False,
            prerendered_blocks=prerendered_blocks,
        ),
    )
    if unsubmitted_lines:
        lines.extend(unsubmitted_lines)

    submitted_lines = _render_summary_section(
        _render_submitted_section_title(submitted_changes),
        include_leading_separator=False,
        changes=submitted_changes,
        verbose=verbose,
        renderer=lambda change: _render_summary_change_lines(
            change=change,
            client=client,
            repo=result.github_repo,
            show_status=True,
            prerendered_blocks=prerendered_blocks,
        ),
    )
    if submitted_lines:
        if lines:
            lines.append("")
        lines.extend(submitted_lines)
    return tuple(lines)


def render_trunk_status_lines(
    *,
    prepared: PreparedStack,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[ui.Renderable, ...]:
    """Render the trunk footer with the user's `jj log` formatting."""

    trunk = prepared.stack.base_parent
    lines = render_commit_lines(
        client=prepared.client,
        change=trunk,
        prerendered_lines=(
            prerendered_blocks.get(trunk.commit_id) if prerendered_blocks else None
        ),
    )
    if len(lines) > 1 and _plain_terminal_text(lines[-1]) in {"|", "│", "┃"}:
        return lines[:-1]
    return lines


def _plain_terminal_text(line: ui.Renderable) -> str:
    """Return visible text from one ANSI-styled `jj log` line."""

    if not isinstance(line, str):
        raise AssertionError("Expected a plain `jj log` line without a status suffix.")
    return Text.from_ansi(line).plain.strip()


def render_empty_status_lines(
    *,
    prepared_status: PreparedStatus,
) -> tuple[ui.Renderable, ...]:
    """Render the empty-stack footer and explanation."""

    return (
        *render_trunk_status_lines(
            prepared=prepared_status.prepared,
        ),
        "The selected stack has no changes to show.",
    )


def _prefetch_commit_log_blocks(
    *,
    client: CommitRenderClient,
    changes: tuple[StackStatusChange, ...],
    trunk: RenderableCommit,
) -> dict[str, tuple[str, ...]]:
    """Render the `jj log` block for every change we will print, in parallel."""

    seen: set[str] = set()
    ordered: list[RenderableCommit] = []
    for change in (*changes, trunk):
        if change.commit_id in seen:
            continue
        seen.add(change.commit_id)
        ordered.append(change)
    return render_commit_blocks(client=client, changes=tuple(ordered))


def _render_summary_section(
    title: ui.Message,
    *,
    include_leading_separator: bool,
    changes: tuple,
    renderer,
    verbose: bool,
) -> tuple[ui.Renderable, ...]:
    """Render one capped summary section."""

    if not changes and not verbose:
        return ()

    heading: ui.Message = f"{title}:" if isinstance(title, str) else t"{title}:"
    lines: list[ui.Renderable] = [heading]
    if include_leading_separator:
        lines.insert(0, "")
    if not changes:
        lines.append("  (none)")
        return tuple(lines)

    rendered = [renderer(change) for change in changes]
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


def _render_submitted_section_title(changes: tuple) -> ui.Message:
    """Render the submitted-section heading, linking the newest submitted PR when possible."""

    top_pr = changes[0].pr if changes else None
    if top_pr is None:
        return "Submitted stack"
    label = format_pr_label(top_pr.number, url=top_pr.html_url)
    return t"Submitted stack ({label})"


def render_status_advisory_lines(
    *,
    result: StatusResult,
) -> tuple[ui.Renderable, ...]:
    """Render any advisories that follow the status stack output."""

    namespace = current_pr_branch_namespace()
    cleanup_changes = [
        change for change in result.changes if isinstance(change.state, (Landed, Merged))
    ]
    divergent_changes = [
        change
        for change in result.changes
        if change.state.divergent and not isinstance(change.state, (Landed, Merged))
    ]
    link_changes = [
        change for change in result.changes if _link_advisory_kind(change.state) is not None
    ]
    moved_changes = [change for change in result.changes if isinstance(change.state, PRHeadMoved)]
    # A moved PR branch stops submit, so "submit needed" would be the wrong next step.
    submitted_disagreements = () if moved_changes else result.submitted_state_disagreements
    policy_warning_rows: list[tuple[ui.TableCell, ui.TableCell]] = []
    for change in cleanup_changes:
        pr = change.pr
        if pr is None:
            continue
        base_ref = pr.base.ref
        if not namespace.contains(base_ref):
            continue
        pr_label = format_pr_label(pr.number, url=pr.html_url)
        policy_warning_rows.append(
            (
                "Repo policy",
                t"Repo policy warning: {pr_label} merged into "
                t"{ui.bookmark(base_ref)}; configure GitHub to block merges of PRs "
                t"targeting {ui.bookmark(namespace.branch_glob)}",
            )
        )
    if (
        not cleanup_changes
        and not divergent_changes
        and not link_changes
        and not moved_changes
        and not submitted_disagreements
        and not policy_warning_rows
    ):
        return ()

    rows: list[tuple[ui.TableCell, ui.TableCell]] = []
    if submitted_disagreements:
        rows.append(
            (
                "Submit needed",
                "PR branches are behind the current local stack",
            )
        )
        rows.append(
            (
                "Meaning",
                "Submit will push the current commit IDs and PR bases to GitHub",
            )
        )
        if cleanup_changes:
            rows.append(
                (
                    "After cleanup",
                    (
                        ui.cmd("jj-stack submit"),
                        " ",
                        ui.revset(result.selected_revset),
                    ),
                )
            )
        else:
            rows.append(
                (
                    "Next step",
                    (
                        ui.cmd("jj-stack submit"),
                        " ",
                        ui.revset(result.selected_revset),
                    ),
                )
            )
        rows.extend(_submitted_state_disagreement_rows(submitted_disagreements))

    if cleanup_changes:
        rows.append(
            (
                "Sync needed",
                "Submit note: descendant PR bases still follow the old local ancestry "
                "until the remaining selected changes are synced",
            )
        )
        rows.append(
            (
                "Preview first",
                (
                    ui.cmd("jj-stack sync --dry-run"),
                    " ",
                    ui.revset(result.selected_revset),
                ),
            )
        )
        rows.append(
            (
                "If the plan is safe",
                (
                    ui.cmd("jj-stack sync"),
                    " ",
                    ui.revset(result.selected_revset),
                ),
            )
        )
        for change in cleanup_changes:
            pr = change.pr
            pr_label: ui.Message = (
                format_pr_label(pr.number, url=pr.html_url) if pr is not None else "merged PR"
            )
            rows.append(
                (
                    ui.change_id(change.change_id),
                    (
                        pr_label,
                        " is merged, and later local changes are still based on it",
                    ),
                )
            )

    if moved_changes:
        single = len(moved_changes) == 1
        noun = "PR branch" if single else "PR branches"
        rows.append(
            (
                f"{noun} moved",
                (
                    f"The {noun} of the change{'' if single else 's'} shown above "
                    f"{'was' if single else 'were'} updated outside this repo, for example by "
                    "another clone, a reviewer's commit, or a GitHub stack rebase or merge. "
                    f"Submit and merge stop until {'it is' if single else 'they are'} resolved. "
                    "If GitHub rewrote the stack, ",
                    ui.cmd(f"jj-stack sync {result.selected_revset}"),
                    " applies its result (preview with ",
                    ui.option("--dry-run"),
                    "); otherwise choose below.",
                ),
            )
        )
        for change in moved_changes:
            state = change.state
            if not isinstance(state, PRHeadMoved):
                raise AssertionError("A moved PR branch advisory requires a moved head.")
            rows.append((ui.change_id(change.change_id), (state.reason, "; ", state.repair)))

    if link_changes:
        rows.append(
            _link_advisory_summary_row(
                link_changes=tuple(link_changes),
                selected_revset=result.selected_revset,
            )
        )
        for change in link_changes:
            rows.append(
                (
                    ui.change_id(change.change_id),
                    _describe_link_advisory(change.state, repo=result.github_repo),
                )
            )

    rows.extend(policy_warning_rows)

    for change in divergent_changes:
        rows.append(
            (
                ui.change_id(change.change_id),
                divergence_recovery_hint(
                    change.change_id,
                    retry="retry the jj-stack command",
                ),
            )
        )
    return ("", "Advisories:", _advisory_table(tuple(rows)))


def _submitted_state_disagreement_rows(
    disagreements: Sequence[str],
) -> tuple[tuple[ui.TableCell, ui.TableCell], ...]:
    if not disagreements:
        return ()
    return (
        (
            "New commit IDs",
            _format_submit_baseline_reason(
                change_ids=tuple(disagreements),
                noun="change",
            ),
        ),
    )


def _format_submit_baseline_reason(
    *,
    change_ids: Sequence[str],
    noun: str,
) -> ui.Message:
    if len(change_ids) == 1:
        return ui.change_id(change_ids[0])
    plural_noun = f"{noun}s" if len(change_ids) != 1 else noun
    return (f"{len(change_ids)} {plural_noun}: ", *_format_change_id_list(change_ids))


def _format_change_id_list(
    change_ids: Sequence[str], *, limit: int = 5
) -> tuple[ui.Message, ...]:
    visible = tuple(change_ids[:limit])
    rendered = list(ui.join(ui.change_id, visible))
    remaining = len(change_ids) - limit
    if remaining > 0:
        if rendered:
            rendered.append(", ")
        rendered.append(f"... {remaining} more")
    return tuple(rendered)


def _advisory_table(rows: tuple[tuple[ui.TableCell, ui.TableCell], ...]) -> ui.DataTable:
    return ui.DataTable(
        columns=(
            ui.TableColumn("advisory", no_wrap=True),
            ui.TableColumn("detail"),
        ),
        rows=rows,
        box="none",
        padding=(0, 2),
        show_header=False,
    )


def _link_advisory_summary_row(
    *,
    link_changes: tuple[StackStatusChange, ...],
    selected_revset: str,
) -> tuple[ui.TableCell, ui.TableCell]:
    states = {_link_advisory_kind(change.state) for change in link_changes}
    change_phrase = (
        "the change shown above" if len(link_changes) == 1 else "one or more changes shown above"
    )
    cleanup_command = ui.cmd(f"jj-stack cleanup {selected_revset}")
    if states == {"closed"}:
        label = "Closed GitHub PR" if len(link_changes) == 1 else "Closed GitHub PRs"
        closed_phrase = "a closed PR" if len(link_changes) == 1 else "closed PRs"
        detail = (
            f"GitHub reports {closed_phrase} for {change_phrase}; submit will not "
            "reuse closed pull requests. Reopen the PR on GitHub to continue using it, "
            "relink an open replacement, or remove the closed PR's leftovers with ",
            cleanup_command,
            " before submitting again.",
        )
        return label, detail
    if states == {"missing"}:
        label = "Missing GitHub PR" if len(link_changes) == 1 else "Missing GitHub PRs"
        detail = (
            f"GitHub did not report a PR for the saved PR branch of {change_phrase}. Run ",
            ui.cmd("jj git fetch"),
            " if branch state may be stale. Relink an open PR if one exists; otherwise forget "
            "the missing PR link with ",
            ui.cmd(f"jj-stack unstack --local {selected_revset}"),
            " before submitting again.",
        )
        return label, detail
    if states == {"ambiguous"}:
        label = "Ambiguous GitHub PR" if len(link_changes) == 1 else "Ambiguous GitHub PRs"
        detail = (
            f"GitHub reports multiple PRs for the saved PR branch of {change_phrase}. Run ",
            ui.cmd("jj git fetch"),
            " to refresh, then relink the intended open PR.",
        )
        return label, detail
    if states == {"saved"}:
        label = "Saved GitHub PR" if len(link_changes) == 1 else "Saved GitHub PRs"
        detail = (
            f"Submit cannot use the saved PR of {change_phrase} as it stands; see its row. "
            "Relink the intended open PR, or forget the saved link with ",
            ui.cmd(f"jj-stack unstack --local {selected_revset}"),
            " before submitting again.",
        )
        return label, detail
    detail = (
        "GitHub reports closed, missing, or ambiguous PR state for one or more "
        "changes shown above. Inspect the per-change rows, then reopen, relink, clean up, or "
        "forget a saved link as appropriate.",
    )
    return "GitHub PRs need repair", detail


def _link_advisory_kind(state: ChangeState) -> str | None:
    """Name the repair advisory a state needs, or None when its saved link is sound."""

    if isinstance(state, PRAmbiguous) or (isinstance(state, CompetingOpenPR) and state.ambiguous):
        return "ambiguous"
    if isinstance(state, (PRIdentityMismatch, CompetingOpenPR)):
        return "saved"
    if isinstance(state, PRMissing):
        return "missing"
    if isinstance(state, Closed):
        return "closed"
    return None


def _render_summary_change_lines(
    *,
    change: StackStatusChange,
    client,
    repo: GithubRepoAddress | None,
    show_status: bool,
    prerendered_blocks: dict[str, tuple[str, ...]] | None = None,
) -> tuple[ui.Renderable, ...]:
    """Render one change inside a submitted or unsubmitted summary section."""

    summary = _format_status_summary(change, repo=repo)
    if not show_status and summary == "not submitted":
        summary = None
    return render_commit_lines(
        client=client,
        change=change,
        suffix=summary,
        prerendered_lines=(
            prerendered_blocks.get(change.commit_id) if prerendered_blocks else None
        ),
    )


def _format_status_summary(
    change: StackStatusChange,
    *,
    repo: GithubRepoAddress | None,
) -> ui.Message:
    state = change.state
    saved_label = _format_saved_pr_label(
        change.tracked.pr_identity if change.tracked is not None else None,
        repo=repo,
    )
    saved: ui.Message = saved_label if saved_label is not None else "saved PR"
    summary: ui.Message
    if isinstance(state, WithPR):
        summary = _format_live_pr_summary(state.pr)
    elif isinstance(state, NotInspected):
        summary = saved
    elif isinstance(state, PRMissing):
        summary = t"{saved}, no PR found for branch"
    elif isinstance(state, (LookupFailed, PRAmbiguous)):
        summary = t"{saved}, {ui.plain_text(state.reason)}"
    else:
        # Untracked changes are never inspected, so the remaining states are Unpublished.
        summary = "not submitted"
    if isinstance(state, PRHeadMoved):
        summary = t"{summary}, PR branch moved"
    if state.divergent and not isinstance(state, (Landed, Merged)):
        summary = t"{summary}, multiple visible commits"
    return summary


def _format_live_pr_summary(pr: GithubPR) -> ui.Message:
    pr_label = format_pr_label(
        pr.number, is_draft=pr.state == "open" and pr.is_draft, url=pr.html_url
    )
    if pr.state == "merged":
        return t"{pr_label} merged into {pr.base.ref}, cleanup needed"
    if pr.state == "closed":
        return t"{pr_label} closed"
    summary: ui.Message = pr_label
    if pr.is_queued:
        summary = t"{summary} queued"
    elif pr.is_draft:
        pass
    elif pr.review_decision == "approved":
        summary = t"{summary} approved"
    elif pr.review_decision == "changes_requested":
        summary = t"{summary} changes requested"
    if pr.check_rollup_status is not None:
        summary = t"{summary}, checks {pr.check_rollup_status}"
    return summary


def _emit_lines(
    lines: tuple[ui.Renderable, ...], *, emitter=console.output, soft_wrap: bool = True
) -> None:
    for line in lines:
        emitter(line, soft_wrap=soft_wrap)


def _format_saved_pr_label(
    pr_identity: PRIdentity | None,
    *,
    repo: GithubRepoAddress | None,
) -> ui.Message | None:
    if pr_identity is None:
        return None
    # Identity-only tracking has no lifecycle to show; --fetch reports it live.
    return format_pr_label(pr_identity.pr_number, prefix="saved ", repo=repo)


def _describe_link_advisory(
    state: ChangeState,
    *,
    repo: GithubRepoAddress | None,
) -> ui.Message:
    if isinstance(state, (PRIdentityMismatch, CompetingOpenPR, PRAmbiguous)):
        return state.reason
    if isinstance(state, PRMissing):
        if state.tracked is None:
            return "GitHub did not report a pull request for this branch"
        saved_label = format_pr_label(
            state.tracked.pr_identity.pr_number,
            prefix="saved ",
            repo=repo,
        )
        return t"GitHub did not report {saved_label} for this branch"
    if isinstance(state, Closed):
        pr_label = format_pr_label(state.pr.number, url=state.pr.html_url)
        return t"{pr_label} is closed; submit will not reuse a closed pull request automatically"
    raise AssertionError(f"Unexpected link advisory state: {type(state).__name__}")
