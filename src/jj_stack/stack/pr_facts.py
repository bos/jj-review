"""Fresh PR facts used to check mutations."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

import jj_stack.github.resolution as github_resolution
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.identifiers import CommitId
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR, GithubRepo, GithubStack
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import (
    PRIdentity,
    TrackedPR,
)
from jj_stack.stack.observation import observe_change_copies
from jj_stack.stack.trunk_evidence import CommitAncestry


@dataclass(frozen=True, slots=True)
class PRFacts:
    """Fresh facts for one change ID; no field decides whether a mutation is safe."""

    tracked: TrackedPR | None
    open_head_prs: tuple[GithubPR, ...]
    local_commits: tuple[LocalCommit, ...]
    pr: GithubPR | None
    remote_pr_branch_target: CommitId | None


@dataclass(frozen=True, slots=True)
class RepoFacts:
    """Fresh pull request facts shared by mutation policies."""

    configured_repo: github_resolution.GithubRepoAddress | None
    github_repo: GithubRepo
    prs_by_base: Mapping[str, tuple[GithubPR, ...]] | None
    remote: GitRemote | None
    repo: github_resolution.GithubRepoAddress
    prs: Mapping[str, PRFacts]
    # Whether the optional facts were requested; only then does an absent value mean absent.
    observed_open_head_prs: bool = False
    observed_remote_targets: bool = False


def duplicate_pr_claim_change_ids(
    identities: Mapping[str, PRIdentity],
) -> frozenset[str]:
    """Return every change participating in a duplicate PR or head claim."""

    values = identities.values()
    pr_claims = Counter(item.pr_number for item in values)
    head_claims = Counter(item.head_ref for item in values)
    return frozenset(
        change_id
        for change_id, item in identities.items()
        if pr_claims[item.pr_number] > 1 or head_claims[item.head_ref] > 1
    )


async def observe_prs(
    *,
    change_ids: tuple[str, ...],
    context: CommandContext,
    github_client: GithubClient,
    remote_name: str,
    include_dependents: bool = False,
    include_open_head_prs: bool = False,
    include_remote_targets: bool = True,
    github_repo_snapshot: GithubRepo | None = None,
    local_commits_snapshot: Mapping[str, tuple[LocalCommit, ...]] | None = None,
) -> RepoFacts:
    """Reload pull request facts, optionally deferring exact remote-ref observation."""

    remotes = context.jj_client.list_git_remotes()
    remote = next((item for item in remotes if item.name == remote_name), None)
    state = context.state_store.load()
    tracked_prs = {change_id: state.prs.get(change_id) for change_id in dict.fromkeys(change_ids)}
    repo = github_client.repo
    known_identities = tuple(
        tracked.pr_identity for tracked in tracked_prs.values() if tracked is not None
    )
    head_refs = tuple(dict.fromkeys(identity.head_ref for identity in known_identities))
    pr_numbers = tuple(dict.fromkeys(identity.pr_number for identity in known_identities))
    if local_commits_snapshot is None:

        def observe_local_commits() -> dict[str, tuple[LocalCommit, ...]]:
            return observe_change_copies(
                jj_client=context.jj_client, state=state, change_ids=tuple(tracked_prs)
            ).copies(tuple(tracked_prs))

        local_task = asyncio.create_task(asyncio.to_thread(observe_local_commits))
    else:
        local_task = asyncio.create_task(asyncio.sleep(0, result=local_commits_snapshot))
    if include_open_head_prs:
        open_heads_request = github_client.get_open_prs_by_head_refs(head_refs=head_refs)
    else:
        open_heads_request = asyncio.sleep(0, result={})
    if include_remote_targets and remote is not None and head_refs:
        remote_targets_request = github_client.get_branch_targets(branches=head_refs)
    else:
        remote_targets_request = asyncio.sleep(0, result={})
    try:
        results = await asyncio.gather(
            github_client.get_prs_by_numbers(pr_numbers=pr_numbers),
            open_heads_request,
            (
                github_client.get_prs_by_base_refs(base_refs=head_refs)
                if include_dependents
                else asyncio.sleep(0, result=None)
            ),
            (
                github_client.get_repo()
                if github_repo_snapshot is None
                else asyncio.sleep(0, result=github_repo_snapshot)
            ),
            remote_targets_request,
        )
        local_commits = await asyncio.shield(local_task)
    except BaseException:
        await asyncio.gather(asyncio.shield(local_task), return_exceptions=True)
        raise
    numbered, by_head, by_base, github_repo, remote_targets = results
    prs = {
        change_id: PRFacts(
            tracked=tracked,
            open_head_prs=(by_head.get(identity.head_ref, ()) if identity is not None else ()),
            local_commits=matches,
            pr=(numbered.get(identity.pr_number) if identity is not None else None),
            remote_pr_branch_target=(
                remote_targets.get(identity.head_ref) if identity is not None else None
            ),
        )
        for change_id, tracked in tracked_prs.items()
        for identity in (tracked.pr_identity if tracked is not None else None,)
        for matches in (local_commits.get(change_id, ()),)
    }

    return RepoFacts(
        configured_repo=github_resolution.parse_github_repo(remote) if remote else None,
        github_repo=github_repo,
        prs_by_base=by_base,
        remote=remote,
        repo=repo,
        prs=prs,
        observed_open_head_prs=include_open_head_prs,
        observed_remote_targets=include_remote_targets and remote is not None,
    )


def classify_commit_ancestries(
    *,
    commit_ids: tuple[str | None, ...],
    context: CommandContext,
    trunk_commit_id: str,
) -> dict[str, CommitAncestry]:
    """Classify commits in one scan while keeping unavailable commits distinct."""

    present_commit_ids = tuple(commit_id for commit_id in commit_ids if commit_id is not None)
    memberships = context.jj_client.query_present_commit_ancestor_membership(
        present_commit_ids,
        descendant_commit_id=trunk_commit_id,
    )
    states: dict[bool, CommitAncestry] = {True: "on_trunk", False: "not_on_trunk"}
    return {
        commit_id: states[memberships[commit_id]] if commit_id in memberships else "unresolved"
        for commit_id in dict.fromkeys(present_commit_ids)
    }


def classify_observed_commit_ancestries(
    *,
    context: CommandContext,
    observation: RepoFacts,
    trunk_commit_id: str,
) -> dict[str, CommitAncestry]:
    return classify_commit_ancestries(
        commit_ids=tuple(
            commit_id
            for item in observation.prs.values()
            for commit_id in (
                item.tracked.submitted_baseline.commit_id if item.tracked is not None else None,
                item.pr.merge_commit_sha if item.pr is not None else None,
            )
        ),
        context=context,
        trunk_commit_id=trunk_commit_id,
    )


async def observe_github_stacks(*, github: GithubClient) -> tuple[GithubStack, ...]:
    try:
        return await github.list_stacks()
    except GithubClientError as error:
        raise CliError(
            "Could not inspect GitHub stack membership.",
            hint="Resolve the GitHub error above, then rerun the command.",
        ) from error
