"""Observe the additional repo facts needed to plan selected convergence."""

from __future__ import annotations

from dataclasses import replace

from jj_stack.bootstrap import CommandContext
from jj_stack.github.client import GithubClient
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.stack.pr_facts import (
    PRFacts,
    RepoFacts,
    observe_prs,
)
from jj_stack.stack.repo import observe_repo_paths


async def complete_sync_observation(
    *,
    context: CommandContext,
    github: GithubClient,
    initial: RepoFacts,
    remote_name: str,
    selected: tuple[LocalCommit, ...],
    stacks: tuple[GithubStack, ...],
) -> tuple[RepoFacts, tuple[GithubStack, ...], bool]:
    state = context.state_store.load()
    selected_prs = {
        tracked.pr_identity.pr_number
        for change in selected
        if (tracked := state.prs.get(change.change_id)) is not None
    }
    affected = tuple(stack for stack in stacks if not selected_prs.isdisjoint(stack.pr_numbers))
    resource_prs = {number for stack in affected for number in stack.pr_numbers}
    tracked_prs = {tracked.pr_identity.pr_number for tracked in state.prs.values()}
    if not any(
        _pr_changed(observed, include_remote_target=False)
        for change in selected
        if (observed := initial.prs.get(change.change_id)) is not None
    ) and (resource_prs & tracked_prs).issubset(selected_prs):
        return initial, (), False
    change_ids = tuple(
        change_id
        for change_id, tracked in state.prs.items()
        if tracked.pr_identity.pr_number in resource_prs
    )
    missing_ids = tuple(change_id for change_id in change_ids if change_id not in initial.prs)
    missing = (
        await observe_prs(
            change_ids=missing_ids,
            context=context,
            github_client=github,
            github_repo_snapshot=initial.github_repo,
            include_remote_targets=False,
            remote_name=remote_name,
        )
        if missing_ids
        else None
    )
    prs = {
        **initial.prs,
        **(missing.prs if missing is not None else {}),
    }
    identities = tuple(
        item.tracked.pr_identity for item in prs.values() if item.tracked is not None
    )
    targets = (
        await github.get_branch_targets(
            branches=tuple(item.head_ref for item in identities),
        )
        if identities
        else {}
    )
    observation = replace(
        initial,
        observed_remote_targets=True,
        prs={
            change_id: replace(
                item,
                remote_pr_branch_target=(
                    targets.get(item.tracked.pr_identity.head_ref)
                    if item.tracked is not None
                    else None
                ),
            )
            for change_id, item in prs.items()
        },
    )
    changed = any(_pr_changed(item) for item in observation.prs.values())
    return observation, stacks if changed else (), changed


def queued_pr_numbers(
    observation: RepoFacts,
    selected: tuple[LocalCommit, ...],
) -> tuple[int, ...]:
    return tuple(
        pr.number
        for change in selected
        if (observed := observation.prs.get(change.change_id)) is not None
        and (pr := observed.pr) is not None
        and pr.state == "open"
        and pr.is_queued
    )


def dependent_path_heads(
    *,
    ancestor_commit_ids: tuple[str, ...],
    context: CommandContext,
    excluded_change_ids: frozenset[str],
) -> dict[str, tuple[LocalCommit, ...]]:
    if not ancestor_commit_ids:
        return {}
    paths = observe_repo_paths(
        jj_client=context.jj_client,
        descendant_of=ancestor_commit_ids,
        state=context.state_store.load(),
    ).paths
    result: dict[str, tuple[LocalCommit, ...]] = {}
    for ancestor in ancestor_commit_ids:
        heads: dict[str, LocalCommit] = {}
        for path in paths:
            if not any(item.commit_id == ancestor for item in path.stack.changes):
                continue
            head = next(
                (
                    change
                    for change in reversed(path.stack.changes)
                    if change.change_id not in excluded_change_ids
                ),
                None,
            )
            if head is not None:
                heads[head.commit_id] = head
        result[ancestor] = tuple(heads.values())
    return result


def _pr_changed(
    observed: PRFacts,
    *,
    include_remote_target: bool = True,
) -> bool:
    pr = observed.pr
    if (tracked := observed.tracked) is None:
        return False
    if pr is None:
        return True
    baseline = tracked.submitted_baseline.commit_id
    return (
        pr.state == "merged"
        or pr.head.sha != baseline
        or (include_remote_target and observed.remote_pr_branch_target != baseline)
        or any(commit.immutable for commit in observed.local_commits)
    )
