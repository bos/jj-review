"""Observe and classify repo-wide convergence without mutating it."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass

from jj_stack.bootstrap import CommandContext
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient
from jj_stack.models.github import GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR, TrackingState
from jj_stack.stack.change_state import (
    ChangeState,
    Closed,
    Landed,
    Merged,
    PRIdentityMismatch,
    PRMissing,
    Stop,
    WithPR,
    classify,
    observe_pr_facts,
    unproven_reason,
)
from jj_stack.stack.convergence_models import (
    FinishPR,
    PRFinishPlan,
    SkipPRFinish,
)
from jj_stack.stack.path import RepoStackPath
from jj_stack.stack.pr_branches import prepare_visible_pr_snapshots
from jj_stack.stack.pr_facts import (
    RepoFacts,
    classify_commit_ancestries,
    observe_github_stacks,
    observe_prs,
)
from jj_stack.stack.repo import observe_repo_paths
from jj_stack.stack.trunk_evidence import CommitAncestry
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class GlobalConvergencePlan:
    blocked: tuple[tuple[TrackedPR, Message], ...]
    finishes: tuple[PRFinishPlan, ...]
    sync_change_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GlobalSyncFacts:
    """One repo-wide observation for global classification."""

    ancestries: Mapping[str, CommitAncestry]
    local_copies: Mapping[str, tuple[LocalCommit, ...]]
    paths: tuple[RepoStackPath, ...]
    pr_facts: RepoFacts
    stacks: tuple[GithubStack, ...]


async def observe_global_sync(
    *,
    context: CommandContext,
    github: GithubClient,
    remote_name: str,
    trunk_commit_id: str,
) -> GlobalSyncFacts:
    """Observe tracked pull requests from tracking toward affected local paths."""

    state = context.state_store.load()
    candidates = state.tracked_prs()
    change_ids = tuple(candidate.change_id for candidate in candidates)
    prepare_visible_pr_snapshots(
        jj_client=context.jj_client,
        state=state,
    )
    all_copies, local_copies = context.jj_client.query_commits_by_change_ids_with_off_trunk(
        change_ids
    )
    anchors = tuple(commit.commit_id for commits in local_copies.values() for commit in commits)
    paths = (
        observe_repo_paths(
            jj_client=context.jj_client,
            descendant_of=anchors,
            state=state,
        ).paths
        if anchors
        else ()
    )
    pr_observations, stacks = await asyncio.gather(
        observe_prs(
            change_ids=change_ids,
            context=context,
            github_client=github,
            include_remote_targets=False,
            local_commits_snapshot=all_copies,
            remote_name=remote_name,
        ),
        observe_github_stacks(github=github),
    )
    commit_ids = _observation_commit_ids(pr_observations)
    return GlobalSyncFacts(
        ancestries=classify_commit_ancestries(
            commit_ids=commit_ids,
            context=context,
            trunk_commit_id=trunk_commit_id,
        ),
        local_copies=local_copies,
        paths=paths,
        pr_facts=pr_observations,
        stacks=stacks,
    )


def build_global_convergence_plan(
    *,
    facts: GlobalSyncFacts,
    state: TrackingState,
) -> GlobalConvergencePlan:
    blocked: list[tuple[TrackedPR, Message]] = []
    finishes: list[PRFinishPlan] = []
    heads: list[str] = []
    tracked_prs = frozenset(identity.pr_number for identity in state.pr_identities.values())
    for candidate in state.tracked_prs():
        reason, finish, candidate_heads = _classify_global_candidate(
            candidate=candidate,
            facts=facts,
            tracked_pr_numbers=tracked_prs,
        )
        heads.extend(candidate_heads)
        if reason is not None:
            blocked.append((candidate, reason))
        if finish is not None:
            finishes.append(finish)
    return GlobalConvergencePlan(
        blocked=tuple(blocked),
        finishes=tuple(finishes),
        sync_change_ids=tuple(dict.fromkeys(heads)),
    )


def _classify_global_candidate(
    *,
    candidate: TrackedPR,
    facts: GlobalSyncFacts,
    tracked_pr_numbers: frozenset[int],
) -> tuple[Message | None, PRFinishPlan | None, tuple[str, ...]]:
    ancestry = facts.ancestries[candidate.submitted_baseline.commit_id]
    state = classify(
        observe_pr_facts(facts.pr_facts, candidate.change_id, ancestries=facts.ancestries)
    )
    heads = _candidate_path_heads(candidate, facts=facts)
    rewritten = isinstance(state, Landed) and state.evidence == "rewritten"
    affected = ancestry == "on_trunk" or rewritten
    if affected:
        return _affected_candidate_plan(
            candidate=candidate,
            facts=facts,
            heads=heads,
            state=state,
            tracked_prs=tracked_pr_numbers,
        )
    if isinstance(state, PRMissing):
        return state.reason, None, ()
    if ancestry == "unresolved":
        return "the submitted commit is unavailable locally", None, ()
    if isinstance(state, (PRIdentityMismatch, Closed, Merged)):
        return unproven_reason(state), None, ()
    return None, None, ()


def _affected_candidate_plan(
    *,
    candidate: TrackedPR,
    facts: GlobalSyncFacts,
    heads: tuple[str, ...] | None,
    state: ChangeState,
    tracked_prs: frozenset[int],
) -> tuple[Message | None, PRFinishPlan | None, tuple[str, ...]]:
    if heads is None:
        return "local history is not a supported stack", None, ()
    if heads:
        return None, None, heads
    if not isinstance(state, WithPR):
        if isinstance(state, Stop):
            return state.reason, None, ()
        raise AssertionError("Global sync looks up every saved pull request.")
    if not isinstance(state, Landed):
        return unproven_reason(state), None, ()
    stack_reason, historical = _detached_stack_blocker(
        candidate=candidate,
        facts=facts,
        tracked_pr_numbers=tracked_prs,
    )
    if stack_reason is not None:
        return stack_reason, None, ()
    finish = (
        SkipPRFinish(candidate)
        if state.evidence == "rewritten" or historical or state.pr.state != "open"
        else FinishPR(candidate, state.pr)
    )
    return None, finish, ()


def _candidate_path_heads(
    candidate: TrackedPR, *, facts: GlobalSyncFacts
) -> tuple[str, ...] | None:
    copies = {commit.commit_id for commit in facts.local_copies[candidate.change_id]}
    if not copies:
        return ()
    heads = tuple(
        path.stack.head.change_id
        for path in facts.paths
        if any(change.commit_id in copies for change in path.stack.changes)
    )
    return heads or None


def _detached_stack_blocker(
    *,
    candidate: TrackedPR,
    facts: GlobalSyncFacts,
    tracked_pr_numbers: frozenset[int],
) -> tuple[Message | None, bool]:
    number = candidate.pr_identity.pr_number
    matching = tuple(
        member for stack in facts.stacks for member in stack.prs if member.number == number
    )
    if not matching:
        return None, False
    pr_label = format_pr_label(number, repo=facts.pr_facts.repo)
    if not matching[0].is_historical:
        return t"GitHub still lists {pr_label} as an active member of its stack", False
    blocked = any(
        number in stack.pr_numbers
        and not set(stack.active_pr_numbers).isdisjoint(tracked_pr_numbers)
        for stack in facts.stacks
    )
    return (
        (
            t"{pr_label} is in a GitHub stack that still has active members tracked here"
            if blocked
            else None
        ),
        True,
    )


def _observation_commit_ids(observation: RepoFacts) -> tuple[str, ...]:
    return tuple(
        commit_id
        for item in observation.prs.values()
        for commit_id in (
            item.baseline.commit_id if item.baseline is not None else None,
            item.pr.merge_commit_sha if item.pr is not None else None,
        )
        if commit_id is not None
    )
