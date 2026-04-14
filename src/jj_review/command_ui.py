"""Shared CLI argument helpers for command modules."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from jj_review.cache import ReviewStateStore
from jj_review.errors import CliError
from jj_review.github.resolution import parse_github_repo, select_submit_remote
from jj_review.jj import JjClient
from jj_review.pull_request_references import (
    parse_pull_request_number,
    parse_repository_pull_request_reference,
)


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
        raise CliError(f"`{command_label}` requires an explicit revision selection.")
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

    if revset is not None:
        raise CliError("Use either `<revset>` or `--pull-request`, not both.")

    pull_request_number = _parse_repo_pull_request_number(
        pull_request_reference=pull_request_reference,
        repo_root=repo_root,
    )
    state = ReviewStateStore.for_repo(repo_root).load()
    matching_change_ids = [
        change_id
        for change_id, cached_change in state.changes.items()
        if cached_change.link_state == "active"
        and cached_change.pr_number == pull_request_number
    ]
    if not matching_change_ids:
        raise CliError(
            f"PR #{pull_request_number} is not linked to any local change. "
            "Use a revision instead, or import or relink it first."
        )
    if len(matching_change_ids) > 1:
        raise CliError(
            f"PR #{pull_request_number} is linked to multiple local changes. "
            f"{action_name.capitalize()} by explicit revision after repairing the links."
        )

    change_id = matching_change_ids[0]
    visible_revisions = JjClient(repo_root).query_revisions_by_change_ids((change_id,)).get(
        change_id,
        (),
    )
    if not visible_revisions:
        raise CliError(
            f"PR #{pull_request_number} is linked to local change {change_id}, "
            f"but that change is not visible. {action_name.capitalize()} by revision once "
            "it is visible again."
        )
    if len(visible_revisions) > 1:
        raise CliError(
            f"PR #{pull_request_number} is linked to local change {change_id}, "
            f"but that change is divergent. {action_name.capitalize()} by explicit "
            "revision after resolving it."
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
            "Could not determine the GitHub repository for `--pull-request`; "
            "use a pull request number or fix the selected remote."
        ) from error
    github_repository = parse_github_repo(remote)
    if github_repository is None:
        raise CliError(
            "Could not determine the GitHub repository for `--pull-request`; "
            "use a pull request number or fix the selected remote."
        )

    return parse_repository_pull_request_reference(
        reference=pull_request_reference,
        github_repository=github_repository,
    )
