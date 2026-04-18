"""Revision and pull-request selection helpers for command modules."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from jj_review import ui
from jj_review.errors import CliError
from jj_review.github.pull_request_refs import (
    parse_pull_request_number,
    parse_repository_pull_request_reference,
)
from jj_review.github.resolution import parse_github_repo, select_submit_remote
from jj_review.jj import JjClient
from jj_review.state.store import ReviewStateStore


def resolve_selected_revset(
    *,
    command_label: str,
    default_revset: str | None = None,
    require_explicit: bool,
    revset: str | None,
) -> str | None:
    """Resolve an optional `<revset>` for revision-oriented commands."""

    if revset is not None:
        return revset
    if require_explicit:
        raise CliError(
            t"{ui.cmd(command_label)} requires an explicit revision selection."
        )
    return default_revset


def parse_comma_separated_flag_values(
    values: Sequence[str] | None,
) -> list[str] | None:
    """Parse repeated comma-separated flag values into a deduplicated list."""

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


def resolve_linked_change_for_pull_request(
    *,
    action_name: str,
    pull_request_reference: str,
    repo_root: Path,
    revset: str | None,
) -> tuple[int, str]:
    """Resolve `--pull-request` to one linked visible local change ID."""

    action_label = action_name.capitalize()
    if revset is not None:
        raise CliError(
            t"Use either {ui.cmd('<revset>')} or {ui.cmd('--pull-request')}, "
            t"not both."
        )

    pull_request_number = _parse_repo_pull_request_number(
        pull_request_reference=pull_request_reference,
        repo_root=repo_root,
    )
    state = ReviewStateStore.for_repo(repo_root).load()
    matching_change_ids = [
        change_id
        for change_id, cached_change in state.changes.items()
        if cached_change.link_state == "active" and cached_change.pr_number == pull_request_number
    ]
    if not matching_change_ids:
        raise CliError(
            t"PR #{pull_request_number} is not linked to any local change.",
            hint=(
                t"Use an explicit revision instead, or run {ui.cmd('import')} or "
                t"{ui.cmd('relink')} first."
            ),
        )
    if len(matching_change_ids) > 1:
        raise CliError(
            t"PR #{pull_request_number} is linked to multiple local changes.",
            hint=t"{action_label} by explicit revision after repairing the links.",
        )

    change_id = matching_change_ids[0]
    visible_revisions = (
        JjClient(repo_root)
        .query_revisions_by_change_ids((change_id,))
        .get(
            change_id,
            (),
        )
    )
    if not visible_revisions:
        raise CliError(
            t"PR #{pull_request_number} is linked to local change {ui.change_id(change_id)}, "
            t"but that change is not visible.",
            hint=t"{action_label} by revision once it is visible again.",
        )
    if len(visible_revisions) > 1:
        raise CliError(
            t"PR #{pull_request_number} is linked to local change {ui.change_id(change_id)}, "
            t"but that change is divergent.",
            hint=t"{action_label} by explicit revision after resolving it.",
        )
    return pull_request_number, change_id


def _parse_repo_pull_request_number(
    *,
    pull_request_reference: str,
    repo_root: Path,
) -> int:
    """Resolve a pull-request selector as a pull request number for this repo."""

    pull_request_number = parse_pull_request_number(pull_request_reference)
    if pull_request_number is not None:
        return pull_request_number

    remotes = JjClient(repo_root).list_git_remotes()
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        raise CliError(
            t"Could not determine the GitHub repository for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote.",
            hint=error.hint,
        ) from error
    github_repository = parse_github_repo(remote)
    if github_repository is None:
        raise CliError(
            t"Could not determine the GitHub repository for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote."
        )

    return parse_repository_pull_request_reference(
        reference=pull_request_reference,
        github_repository=github_repository,
    )
