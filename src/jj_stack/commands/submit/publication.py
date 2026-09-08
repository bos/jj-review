"""Plan and publish pull request updates for submit and sync."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace

import jj_stack.console as console
from jj_stack.bootstrap import CommandContext
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY
from jj_stack.github.client import GithubClient
from jj_stack.identifiers import CommitId
from jj_stack.jj.client import PRRefUpdate
from jj_stack.models.github import GithubPR, GithubStack
from jj_stack.stack.github_stack_safety import dissolve_github_stack

from . import auto_close
from .auto_close import retarget_pr_bases_before_branch_push
from .comments import sync_submit_comments
from .github_stack import (
    GithubStackPlan,
    apply_github_stack_plan,
    omitted_active_stack_prs,
    plan_github_stack,
)
from .inputs import confirm_orphaned_pr_snapshots
from .models import (
    GeneratedDescription,
    PreparedSubmitChange,
    PRMetadataAction,
    PRSyncPlan,
    PublicationInputs,
    SubmitMutationRun,
    SubmitResult,
    SubmittedChange,
)
from .prs import sync_prs


def plan_pr_updates(
    *,
    bottom_base_branch: str,
    drafts: dict[str, bool],
    generated_descriptions: dict[str, GeneratedDescription],
    metadata: PRMetadataAction,
    explicit_metadata: bool = False,
    prepared_changes: tuple[PreparedSubmitChange, ...],
    prior_reviewers: Mapping[int, list[str]],
) -> tuple[PRSyncPlan, ...]:
    labels, reviewers, team_reviewers = metadata
    base_branches = (
        bottom_base_branch,
        *(change.branch for change in prepared_changes[:-1]),
    )
    plans: list[PRSyncPlan] = []
    for prepared, base_branch in zip(prepared_changes, base_branches, strict=True):
        pr = prepared.pr
        plan = PRSyncPlan(
            base_branch=base_branch,
            draft=drafts[prepared.change.change_id],
            generated_description=generated_descriptions[prepared.change.change_id],
            metadata=None,
            prepared=prepared,
        )
        prior = prior_reviewers.get(pr.number, ()) if pr else ()
        merged_reviewers = list(dict.fromkeys((*reviewers, *prior)))
        full_metadata = plan.action != "unchanged" or explicit_metadata
        if full_metadata or merged_reviewers != reviewers:
            plan = replace(
                plan,
                metadata=PRMetadataAction(
                    labels=labels if full_metadata else [],
                    reviewers=merged_reviewers,
                    team_reviewers=team_reviewers if full_metadata else [],
                ),
            )
        plans.append(plan)
    return tuple(plans)


async def publish_prepared(
    *,
    context: CommandContext,
    github_client: GithubClient,
    prepared_inputs: PublicationInputs,
    pr_plans: tuple[PRSyncPlan, ...],
    remote_targets: dict[str, CommitId],
    observed_stacks: tuple[GithubStack, ...],
    trunk_branch: str,
    trunk_targets: dict[str, CommitId],
    dry_run: bool,
) -> SubmitResult:
    client = prepared_inputs.client
    state = prepared_inputs.state
    if not dry_run:
        context.state_store.require_writable()
    mutation_run = SubmitMutationRun(
        state=state,
        state_store=context.state_store,
    )
    prepared_changes = tuple(plan.prepared for plan in pr_plans)
    pushes_pr_branches = any(change.remote_action == "pushed" for change in prepared_changes)
    planned_branches = {change.branch for change in prepared_changes}
    observed_base_refs = tuple(
        dict.fromkeys(
            pr.base.ref
            for plan in pr_plans
            if (pr := plan.prepared.pr) is not None
            and pr.state == "open"
            and pr.base.ref not in planned_branches
            and pr.base.ref not in trunk_targets
            and pr.base.ref not in remote_targets
        )
    )
    observed_base_targets = await github_client.get_branch_targets(
        branches=observed_base_refs,
    )
    retarget_prs = (
        auto_close.predict_prs_auto_closed_by_push(
            jj_client=client,
            plans=pr_plans,
            prepared_changes=prepared_changes,
            remote_targets={**trunk_targets, **remote_targets, **observed_base_targets},
        )
        if pushes_pr_branches
        else ()
    )
    desired_pr_numbers = tuple(
        plan.prepared.pr.number if plan.prepared.pr is not None else None for plan in pr_plans
    )
    omitted_stack_prs = (
        omitted_active_stack_prs(
            desired=desired_pr_numbers,
            observed_stacks=observed_stacks,
        )
        if not prepared_inputs.is_maximal_path
        else ()
    )
    orphaned_pr_snapshots = confirm_orphaned_pr_snapshots(
        candidates=omitted_stack_prs,
        jj_client=client,
        state=state,
    )
    github_stack_plan = plan_github_stack(
        desired=desired_pr_numbers,
        is_maximal_path=prepared_inputs.is_maximal_path,
        observed_stacks=observed_stacks,
        orphaned_pr_snapshots=orphaned_pr_snapshots,
        pr_numbers_requiring_base_update={
            pr.number
            for plan in pr_plans
            if (pr := plan.prepared.pr) is not None
            and (pr.base.ref != plan.base_branch or pr in retarget_prs)
        },
        repo=github_client.repo,
    )
    stacks_to_dissolve = (
        github_stack_plan.affected_stacks if github_stack_plan.action == "replace" else ()
    )
    pr_branch_ref_updates = tuple(
        PRRefUpdate(
            branch=prepared.branch,
            expected_target=prepared.expected_remote_target,
            desired_target=prepared.change.commit_id,
        )
        for prepared in prepared_changes
    )

    if dry_run:
        submitted_changes = tuple(
            SubmittedChange(prepared=plan.prepared, pr_action=plan.action, pr=plan.prepared.pr)
            for plan in pr_plans
        )
    else:
        submitted_changes = await _apply_planned_submit(
            github_client=github_client,
            github_stack_plan=github_stack_plan,
            prepared_inputs=prepared_inputs,
            pr_plans=pr_plans,
            pr_branch_ref_updates=pr_branch_ref_updates,
            retarget_prs=retarget_prs,
            run=mutation_run,
            stacks_to_dissolve=stacks_to_dissolve,
            trunk_branch=trunk_branch,
        )
    return SubmitResult(
        client=client,
        dry_run=dry_run,
        changes=submitted_changes,
        github_stack_actions=mutation_run.github_stack_actions,
        trunk=prepared_inputs.stack.trunk,
    )


async def _apply_planned_submit(
    *,
    github_client: GithubClient,
    github_stack_plan: GithubStackPlan,
    prepared_inputs: PublicationInputs,
    pr_plans: tuple[PRSyncPlan, ...],
    pr_branch_ref_updates: tuple[PRRefUpdate, ...],
    retarget_prs: tuple[GithubPR, ...],
    run: SubmitMutationRun,
    stacks_to_dissolve: tuple[GithubStack, ...],
    trunk_branch: str,
) -> tuple[SubmittedChange[GithubPR], ...]:
    for github_stack in stacks_to_dissolve:
        await dissolve_github_stack(github_client=github_client, stack=github_stack)
    # GitHub has no transaction spanning PR branches, pull requests, and stack
    # membership. An external stack edit can race this mutation, and submit accepts that
    # narrow window rather than pretending another non-atomic observation closes it.
    if retarget_prs:
        await retarget_pr_bases_before_branch_push(
            github_client=github_client,
            prs=retarget_prs,
            trunk_branch=trunk_branch,
        )
    with console.spinner(description="Pushing PR branches"):
        prepared_inputs.client.mutate_remote_pr_branch_refs(
            remote=prepared_inputs.remote.name,
            updates=pr_branch_ref_updates,
        )
    with console.progress(
        description="Syncing pull requests",
        total=len(pr_plans),
    ) as progress:
        submitted = await sync_prs(
            github_client=github_client,
            on_progress=progress.advance,
            plans=pr_plans,
            run=run,
        )
    pr_numbers = tuple(change.pr.number for change in submitted)
    grouped = await apply_github_stack_plan(
        github_client=github_client,
        plan=github_stack_plan,
        pr_numbers=pr_numbers,
    )
    actions = [f"dissolved GitHub stack #{stack.number}" for stack in stacks_to_dissolve]
    if grouped is not None:
        verb = "extended" if github_stack_plan.action == "append" else "created"
        actions.append(f"{verb} GitHub stack #{grouped.number}")
    run.github_stack_actions = tuple(actions)
    submitted_force_pushes_by_pr = {
        pr_number: (expected_target, change.prepared.change.commit_id)
        for change, pr_number in zip(submitted, pr_numbers, strict=True)
        if change.pr_action != "created"
        and change.prepared.remote_action == "pushed"
        and (expected_target := change.prepared.expected_remote_target) is not None
    }
    await sync_submit_comments(
        base_is_another_pr=pr_plans[0].base_branch != trunk_branch,
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        generated_stack_description=prepared_inputs.generated_stack_description,
        github_client=github_client,
        pr_numbers=pr_numbers,
        submitted_force_pushes_by_pr=submitted_force_pushes_by_pr,
    )
    return submitted
