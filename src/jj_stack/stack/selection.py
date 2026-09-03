"""Change and pull-request selection helpers for command modules."""

from __future__ import annotations

from collections.abc import Sequence

import jj_stack.ui as ui
from jj_stack.errors import AmbiguousSelectionError, CliError, UsageError
from jj_stack.formatting import format_pr_label
from jj_stack.github.pr_refs import (
    parse_pr_number,
    parse_repo_pr_reference,
)
from jj_stack.github.resolution import GithubRepoAddress, parse_github_repo, select_submit_remote
from jj_stack.jj.client import JjClient
from jj_stack.state.store import TrackingStore


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


def resolve_linked_change_for_pr(
    *,
    jj_client: JjClient,
    pr_reference: str,
    revset: str | None,
) -> tuple[int, str, GithubRepoAddress | None]:
    """Resolve `--pull-request` to one linked visible local change ID."""

    if revset is not None:
        raise UsageError(
            t"Use either {ui.cmd('<revset>')} or {ui.cmd('--pull-request')}, not both."
        )

    pr_number, repo = resolve_pr_reference(
        jj_client=jj_client,
        pr_reference=pr_reference,
    )
    pr_label = format_pr_label(pr_number, repo=repo)
    state = TrackingStore.for_repo(jj_client.repo_root).load()
    matching_change_ids = [
        change_id
        for change_id, pr_identity in state.pr_identities.items()
        if pr_identity.pr_number == pr_number
    ]
    if not matching_change_ids:
        raise CliError(
            t"{pr_label} is not linked to any local change.",
            hint=(
                t"Use an explicit change instead, or run {ui.cmd('jj-stack checkout')} or "
                t"{ui.cmd('jj-stack relink')} first."
            ),
        )
    if len(matching_change_ids) > 1:
        raise AmbiguousSelectionError(
            t"{pr_label} is linked to multiple local changes.",
            hint=t"Use an explicit change ID after pointing the remote at the intended repo.",
        )

    return pr_number, matching_change_ids[0], repo


def resolve_pr_reference(
    *,
    jj_client: JjClient,
    pr_reference: str,
) -> tuple[int, GithubRepoAddress | None]:
    """Resolve a pull-request selector and its repo when one is available."""

    pr_number = parse_pr_number(pr_reference)
    remotes = jj_client.list_git_remotes()
    try:
        remote = select_submit_remote(remotes)
    except CliError as error:
        if pr_number is not None:
            return pr_number, None
        raise CliError(
            t"Could not determine the GitHub repo for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote.",
            hint=error.hint,
        ) from error
    github_repo = parse_github_repo(remote)
    if github_repo is None:
        if pr_number is not None:
            return pr_number, None
        raise CliError(
            t"Could not determine the GitHub repo for {ui.cmd('--pull-request')}; "
            t"use a pull request number or fix the selected remote."
        )

    return (
        pr_number
        if pr_number is not None
        else parse_repo_pr_reference(reference=pr_reference, github_repo=github_repo),
        github_repo,
    )
