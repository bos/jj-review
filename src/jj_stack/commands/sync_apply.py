"""Apply complete sync convergence plans."""

from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.commands._cleanup_actions import close_pr_on_trunk
from jj_stack.commands.cleanup.command import cleanup_tracked_prs
from jj_stack.commands.submit.command import run_submit_async
from jj_stack.commands.submit.models import SubmitOptions
from jj_stack.commands.submit.render import print_submit_result
from jj_stack.errors import CliError, ConflictedStackError
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient
from jj_stack.github.resolution import GithubTarget
from jj_stack.identifiers import short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.jj.client import PRRefUpdate
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import SubmittedBaseline, TrackedPR
from jj_stack.stack.convergence import divergent_change_error
from jj_stack.stack.convergence_models import (
    AdoptedSurvivor,
    ConvergenceActions,
    GithubStackMergePlan,
    GithubStackRebasePlan,
    PRFinishPlan,
    SelectedConvergencePlan,
    SkipPRFinish,
)
from jj_stack.stack.convergence_observation import dependent_path_heads
from jj_stack.stack.observation import observe_change_copies, observe_pr_bookmarks
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class PRFinishResult:
    change_id: str
    candidate: TrackedPR
    outcome: Literal["finished", "already_terminal", "skipped"]
    skip_reason: Message | None = None


async def apply_pr_finishes(
    *,
    plans: tuple[PRFinishPlan, ...],
    dry_run: bool,
    github: GithubClient,
    trunk_branch: str,
) -> tuple[PRFinishResult, ...]:
    results: list[PRFinishResult] = []
    for plan in plans:
        results.append(
            await _apply_pr_finish(
                plan=plan,
                dry_run=dry_run,
                github=github,
                trunk_branch=trunk_branch,
            )
        )
    visible = tuple(result for result in results if result.outcome != "already_terminal")
    if visible:
        console.output(
            "Planned GitHub updates for merged PRs:"
            if dry_run
            else "Applied GitHub updates for merged PRs:"
        )
        marker = "•" if dry_run else "✓"
        for result in visible:
            if result.outcome == "skipped":
                console.output(
                    t"  ! leave {ui.change_id(result.change_id)} unchanged: {result.skip_reason}"
                )
            else:
                pr_label = format_pr_label(
                    result.candidate.pr_identity.pr_number,
                    repo=github.repo,
                )
                console.output(t"  {marker} finish merged {pr_label}")
    return tuple(results)


async def _apply_pr_finish(
    *, plan: PRFinishPlan, dry_run: bool, github: GithubClient, trunk_branch: str
) -> PRFinishResult:
    candidate = plan.candidate
    if isinstance(plan, SkipPRFinish):
        return PRFinishResult(plan.change_id, candidate, "already_terminal")
    if dry_run:
        return PRFinishResult(plan.change_id, candidate, "finished")
    pr_label = format_pr_label(plan.pr.number, url=plan.pr.html_url)
    console.output(t"Finishing {pr_label} for {plan.change_id}...")
    reason = await close_pr_on_trunk(github_client=github, pr=plan.pr, trunk_branch=trunk_branch)
    return (
        PRFinishResult(plan.change_id, candidate, "skipped", reason)
        if reason
        else PRFinishResult(plan.change_id, candidate, "finished")
    )


async def apply_selected_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    github: GithubClient,
    plan: SelectedConvergencePlan,
    target: GithubTarget,
    trunk_branch: str,
    trunk_commit_id: str,
) -> int:
    """Apply one complete selected convergence plan in dependency order."""

    actions = plan.actions
    if isinstance(plan, GithubStackRebasePlan):
        _apply_github_stack_rebase(
            context=context,
            dry_run=dry_run,
            plan=plan,
            remote_name=target.remote.name,
            trunk_commit_id=trunk_commit_id,
        )
        return 0
    results = await apply_pr_finishes(
        plans=tuple(change.finish for change in actions.on_trunk),
        dry_run=dry_run,
        github=github,
        trunk_branch=trunk_branch,
    )
    dependencies = _apply_local_convergence(
        context=context,
        dry_run=dry_run,
        plan=plan,
        remote_name=target.remote.name,
        trunk_commit_id=trunk_commit_id,
    )
    update_result = await _refresh_selected_prs(
        actions=actions,
        context=context,
        dry_run=dry_run,
    )
    if update_result != 0:
        return update_result
    return await _cleanup_reconciled_prs(
        context=context,
        dry_run=dry_run,
        finish_results=results,
        github=github,
        submitted_survivors=actions.submitted_survivors,
        dependencies=dependencies,
        target=target,
    )


def _apply_local_convergence(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: SelectedConvergencePlan,
    remote_name: str,
    trunk_commit_id: str,
) -> dict[str, tuple[LocalCommit, ...]]:
    actions = plan.actions
    rewritten = plan.adopted_survivors if isinstance(plan, GithubStackMergePlan) else ()
    # GitHub's rewrite of a survivor is its baseline, moved. Adopt those commits only while every
    # survivor is still at its baseline; otherwise rebase them all and let the refresh republish.
    adopt = _all_at_baseline(rewritten)
    adopted_ids = {item.change_id for item in rewritten} if adopt else set()
    rebased = (
        (
            *(item for item in actions.survivors if item.change_id not in adopted_ids),
            *actions.working_copy_children,
        )
        if actions.on_trunk
        else ()
    )
    if dry_run:
        return _observe_removal_dependencies(context=context, actions=actions)
    if isinstance(plan, GithubStackMergePlan) and adopt and rewritten:
        top = rewritten[-1]
        replaced = tuple(
            item.local_change.commit_id
            for item in rewritten
            if item.local_change.commit_id != item.remote_commit_id
        )
        destination = top.remote_commit_id
        attachment = context.jj_client.import_remote_pr_branch_ref(
            remote=remote_name,
            branch=top.candidate.pr_identity.head_ref,
            expected_target=destination,
            expected_change_id=top.change_id,
            expected_chain=tuple(
                (
                    item.candidate.pr_identity.head_ref,
                    item.remote_commit_id,
                    item.change_id,
                )
                for item in rewritten
            ),
            expected_parent_commit_id=plan.expected_parent_commit_id,
        )
    else:
        replaced = ()
        destination = trunk_commit_id
        attachment = nullcontext()
    with attachment:
        if rebased:
            change_ids, rewrite_args = _single_visible_change_ids(context, rebased)
            context.jj_client.rebase_changes(
                change_ids=change_ids, destination=destination, cli_args=rewrite_args
            )
        else:
            rewrite_args, _snapshots = observe_pr_bookmarks(
                jj_client=context.jj_client, state=context.state_store.load()
            )
        if replaced:
            context.jj_client.abandon_changes(replaced, cli_args=rewrite_args)
        dependencies = _observe_removal_dependencies(context=context, actions=actions)
        abandoned = tuple(
            change.change.commit_id
            for change in actions.on_trunk
            if change.change is not None
            and not change.change.immutable
            and not dependencies.get(change.change_id)
        )
        if abandoned:
            context.jj_client.abandon_changes(abandoned, cli_args=rewrite_args)
        if rewritten:
            context.state_store.relink_prs(
                replacements={
                    item.change_id: TrackedPR(
                        pr_identity=item.candidate.pr_identity,
                        submitted_baseline=SubmittedBaseline(commit_id=item.remote_commit_id),
                    )
                    for item in rewritten
                },
            )
    return dependencies


def _apply_github_stack_rebase(
    *,
    context: CommandContext,
    dry_run: bool,
    plan: GithubStackRebasePlan,
    remote_name: str,
    trunk_commit_id: str,
) -> None:
    adopted = plan.adopted_survivors
    top = adopted[-1]
    with context.jj_client.import_remote_pr_branch_ref(
        remote=remote_name,
        branch=top.candidate.pr_identity.head_ref,
        expected_target=top.remote_commit_id,
        expected_chain=tuple(
            (
                item.candidate.pr_identity.head_ref,
                item.remote_commit_id,
                (None, item.change_id),
            )
            for item in adopted
        ),
        expected_parent_commit_id=trunk_commit_id,
    ):
        desired_by_change, operation_id = _verified_local_rebase(
            context=context,
            plan=plan,
            trunk_commit_id=trunk_commit_id,
        )
        if dry_run:
            return
        if operation_id is not None:
            context.jj_client.integrate_operation(operation_id)
        context.jj_client.mutate_remote_pr_branch_refs(
            remote=remote_name,
            updates=tuple(
                PRRefUpdate(
                    branch=item.candidate.pr_identity.head_ref,
                    expected_target=item.remote_commit_id,
                    desired_target=desired_by_change[item.change_id].commit_id,
                )
                for item in adopted
            ),
        )
        context.state_store.relink_prs(
            replacements={
                item.change_id: TrackedPR(
                    pr_identity=item.candidate.pr_identity,
                    submitted_baseline=SubmittedBaseline(
                        commit_id=desired_by_change[item.change_id].commit_id
                    ),
                )
                for item in adopted
            },
        )


def _verified_local_rebase(
    *,
    context: CommandContext,
    plan: GithubStackRebasePlan,
    trunk_commit_id: str,
) -> tuple[dict[str, LocalCommit], str | None]:
    adopted = plan.adopted_survivors
    local = plan.actions.survivors
    desired = local
    operation_id: str | None = None
    if _all_at_baseline(adopted):
        change_ids, rewrite_args = _single_visible_change_ids(
            context, (*local, *plan.actions.working_copy_children)
        )
        operation_id = context.jj_client.prepare_rebase_changes(
            change_ids=change_ids, destination=trunk_commit_id, cli_args=rewrite_args
        )
        grouped = context.jj_client.query_commits_at_operation(
            change_ids=tuple(item.change_id for item in local),
            operation_id=operation_id,
            cli_args=rewrite_args,
        )
        desired = tuple(
            commits[0] for item in local if len(commits := grouped[item.change_id]) == 1
        )
        if len(desired) != len(local):
            raise CliError(
                "A local change did not have exactly one commit after rebasing onto trunk."
            )
    expected_parent = trunk_commit_id
    for change in desired:
        if change.conflict:
            raise CliError(
                t"Rebasing {ui.change_id(change.change_id)} locally produced conflicts.",
                hint=t"Rebase and resolve the stack with {ui.cmd('jj')}, then run "
                t"{ui.cmd('jj-stack submit')}.",
            )
        if change.parents != (expected_parent,):
            raise CliError(
                "The local stack does not match GitHub's rebase onto trunk.",
                hint=t"Inspect the local and GitHub stacks, then restore or resubmit the "
                t"intended pull requests.",
            )
        expected_parent = change.commit_id
    desired_by_change = {item.change_id: item for item in desired}
    tree_pairs = tuple(
        (desired_by_change[item.change_id].commit_id, item.remote_commit_id) for item in adopted
    )
    trees = context.jj_client.git_tree_ids(
        tuple(commit_id for pair in tree_pairs for commit_id in pair)
    )
    if any(trees[local_id] != trees[remote_id] for local_id, remote_id in tree_pairs):
        raise CliError(
            "GitHub's rewritten stack does not have the same contents as the local rebase.",
            hint=t"Inspect the changed PR branches on GitHub before choosing which version "
            t"to keep.",
        )
    return desired_by_change, operation_id


def _all_at_baseline(items: tuple[AdoptedSurvivor, ...]) -> bool:
    return all(
        item.local_change.commit_id == item.candidate.submitted_baseline.commit_id
        for item in items
    )


def _single_visible_change_ids(
    context: CommandContext, changes: tuple[LocalCommit, ...]
) -> tuple[tuple[str, ...], JjCliArgs]:
    """Require one visible commit per change right before rewriting it.

    Planning observed these changes before the GitHub round-trips; one that became divergent
    since then must not be rewritten at all.
    """

    change_ids = tuple(change.change_id for change in changes)
    observed = observe_change_copies(
        jj_client=context.jj_client, state=context.state_store.load(), change_ids=change_ids
    )
    visible = observed.copies(change_ids)
    for change_id in change_ids:
        if len(visible[change_id]) != 1:
            raise divergent_change_error(change_id)
    return change_ids, observed.cli_args


async def _refresh_selected_prs(
    *, actions: ConvergenceActions, context: CommandContext, dry_run: bool
) -> int:
    if not actions.on_trunk:
        return 0
    if actions.survivors and dry_run:
        short = short_change_id(actions.survivors[-1].change_id)
        console.output(
            t"Run {ui.cmd(f'jj-stack sync {short}')} to apply the "
            t"rebase and then compute updates for the remaining existing PRs."
        )
        return 0
    if not actions.submitted_survivors:
        if actions.survivors:
            console.output("No existing pull requests to update; trailing work remains local.")
        return 0
    head_change_id = actions.submitted_survivors[-1].change_id
    try:
        result = await run_submit_async(
            context=context,
            on_prepared=None,
            options=SubmitOptions(
                base_revset=None,
                descriptions=(),
                describe_with=None,
                draft_mode="default",
                dry_run=dry_run,
                edit=False,
                existing_only=True,
                labels=None,
                re_request=False,
                reviewers=None,
                revset=head_change_id,
                team_reviewers=None,
            ),
        )
    except ConflictedStackError as error:
        raise ConflictedStackError(
            error.message,
            hint=t"The local rebase is complete. Resolve the conflicts with {ui.cmd('jj')}, "
            t"then update the remaining pull requests with "
            t"{ui.cmd(f'jj-stack submit {short_change_id(head_change_id)}')}",
        ) from error
    print_submit_result(result)
    return 0


async def _cleanup_reconciled_prs(
    *,
    context: CommandContext,
    dry_run: bool,
    finish_results: tuple[PRFinishResult, ...],
    github: GithubClient,
    submitted_survivors: tuple[LocalCommit, ...],
    dependencies: dict[str, tuple[LocalCommit, ...]],
    target: GithubTarget,
) -> int:
    cleanup_change_ids: list[str] = []
    for result in finish_results:
        if result.outcome == "skipped":
            continue
        if heads := dependencies.get(result.change_id):
            recovery_commands = tuple(
                f"jj-stack sync {short_change_id(head.change_id)}" for head in heads
            )
            recovery = t"run {ui.join(ui.cmd, recovery_commands)}"
            pr_label = format_pr_label(
                result.candidate.pr_identity.pr_number,
                repo=target.repo,
            )
            console.output(
                t"  ! kept {pr_label} and its PR "
                t"branch for {ui.change_id(result.change_id)}: another local stack "
                t"still uses this merged change; {recovery}"
            )
            continue
        cleanup_change_ids.append(result.change_id)
    tracked_prs = context.state_store.load().prs
    cleanup = await cleanup_tracked_prs(
        change_ids=tuple(cleanup_change_ids),
        context=context,
        dry_run=dry_run,
        github_client=github,
        github_target=target,
        planned_detached_dependents=frozenset(
            tracked.pr_identity.pr_number
            for change in submitted_survivors
            if (tracked := tracked_prs.get(change.change_id)) is not None
        ),
        planned_local_removals=frozenset(cleanup_change_ids),
    )
    return 1 if any(action.status == "blocked" for action in cleanup.actions) else 0


def _observe_removal_dependencies(
    *, context: CommandContext, actions: ConvergenceActions
) -> dict[str, tuple[LocalCommit, ...]]:
    anchors = {
        change.change_id: (
            change.change.commit_id
            if change.change is not None
            else change.candidate.submitted_baseline.commit_id
        )
        for change in actions.on_trunk
        if change.evidence_kind == "rewritten"
    }
    observed = dependent_path_heads(
        ancestor_commit_ids=tuple(anchors.values()),
        context=context,
        excluded_change_ids=frozenset(
            (
                *(change.change_id for change in actions.on_trunk),
                *(item.change_id for item in actions.survivors),
            )
        ),
    )
    return {change_id: observed.get(commit_id, ()) for change_id, commit_id in anchors.items()}
