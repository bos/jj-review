from __future__ import annotations

from dataclasses import dataclass

import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.identifiers import short_change_id
from jj_stack.models.github import GithubPR, GithubStack, GithubStackPR
from jj_stack.models.stack import LocalCommit
from jj_stack.models.tracking import TrackedPR, TrackingState
from jj_stack.stack.convergence_models import (
    AdoptedSurvivor,
    ConvergenceActions,
    FinishPR,
    GithubStackMergePlan,
    GithubStackRebasePlan,
    OnTrunkChange,
    OrdinaryConvergencePlan,
    PRFinishPlan,
    SelectedConvergencePlan,
    SkipPRFinish,
)
from jj_stack.stack.github_stack_safety import selected_github_stack
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.stack.status import PreparedStatus
from jj_stack.stack.trunk_evidence import (
    CommitAncestry,
    TrunkEvidenceKind,
    classify_proven_kind,
)


class CheckedOutMergedChangeError(CliError):
    def __init__(self, message, *, workspaces: tuple[str, ...]) -> None:
        super().__init__(message)
        self.workspaces = workspaces


@dataclass(frozen=True, slots=True)
class _NoGithubStack:
    pass


@dataclass(frozen=True, slots=True)
class _GithubStackMerge:
    history: tuple[OnTrunkChange, ...]
    adopted: tuple[AdoptedSurvivor, ...]
    merge_result_commit_id: str | None


@dataclass(frozen=True, slots=True)
class _GithubStackRebase:
    adopted: tuple[AdoptedSurvivor, ...]


type _GithubStackEffect = _NoGithubStack | _GithubStackMerge | _GithubStackRebase


def build_selected_convergence_plan(
    *,
    ancestries: dict[str, CommitAncestry],
    context: CommandContext,
    github_stacks: tuple[GithubStack, ...],
    observation: RepoFacts,
    prepared_status: PreparedStatus,
    trunk_branch: str,
) -> SelectedConvergencePlan:
    selected = prepared_status.prepared.stack.changes
    state = prepared_status.prepared.state
    effect = _classify_github_stack(
        ancestries=ancestries,
        github_stacks=github_stacks,
        observation=observation,
        selected=selected,
        state=state,
        trunk_branch=trunk_branch,
    )
    history = effect.history if isinstance(effect, _GithubStackMerge) else ()
    adopted = effect.adopted if not isinstance(effect, _NoGithubStack) else ()
    history_ids = {item.candidate.change_id for item in history}
    active_ids = {item.candidate.change_id for item in adopted}
    on_trunk = list(history)
    survivors: list[LocalCommit] = []
    for change in (item for item in selected if item.change_id not in history_ids):
        candidate = state.tracked_pr(change.change_id)
        evidence_kind = (
            None
            if candidate is None or change.change_id in active_ids
            else _trunk_evidence_kind_for(
                ancestries=ancestries,
                candidate=candidate,
                observation=observation,
            )
        )
        if candidate is None or evidence_kind is None:
            survivors.append(change)
            continue
        if survivors:
            raise CliError(
                t"Cannot sync submitted {ui.change_id(change.change_id)} because these "
                t"unmerged local changes are its parents: "
                t"{ui.join(lambda item: ui.change_id(item.change_id), tuple(survivors))}. "
                t"The submitted change is already on trunk, so sync cannot decide "
                t"whether those local changes belong before or after it.\n"
                t"Submitted commit: "
                t"{ui.semantic_text(candidate.submitted_baseline.commit_id, 'commit_id')}\n"
                t"Local copy commit: {ui.semantic_text(change.commit_id, 'commit_id')}\n"
                t"Trunk commit: "
                t"{
                    ui.semantic_text(prepared_status.prepared.stack.trunk.commit_id, 'commit_id')
                }",
                hint=t"Inspect the local and fetched histories with "
                t"{
                    ui.cmd(f"jj log -r 'trunk() | (trunk()..{selected[-1].commit_id})'")
                }, and put the unmerged changes where you want them with {ui.cmd('jj')}. Then "
                t"check the remaining pull requests with {ui.cmd('jj-stack view')}, and run "
                t"{ui.cmd('jj-stack sync <head-change-id>')} for a stack that still has "
                t"submitted changes, or {ui.cmd('jj-stack cleanup')} if none remains.",
            )
        on_trunk.append(
            OnTrunkChange(
                candidate=candidate,
                evidence_kind=evidence_kind,
                finish=_finish_plan(candidate, observation, evidence_kind == "exact"),
                change=change,
            )
        )

    _require_no_unpublished_edits(tuple(on_trunk))
    _require_no_checked_out_merged_changes(tuple(on_trunk))
    submitted = _submitted_survivors(
        survivors=tuple(survivors),
        state=state,
        observation=observation,
    )
    local_head = selected[-1]
    working_copy_children = tuple(
        commit
        for commit in context.jj_client.query_descendant_commits((local_head.commit_id,))
        if commit.is_working_copy and commit.empty and commit.parents == (local_head.commit_id,)
    )
    actions = ConvergenceActions(
        on_trunk=tuple(on_trunk),
        submitted_survivors=submitted,
        survivors=tuple(survivors),
        working_copy_children=working_copy_children,
    )
    _require_no_divergent_survivors(actions, adopted=adopted)
    if isinstance(effect, _GithubStackRebase):
        return GithubStackRebasePlan(actions=actions, adopted_survivors=adopted)
    if isinstance(effect, _GithubStackMerge):
        return GithubStackMergePlan(
            actions=actions,
            adopted_survivors=adopted,
            # Without a reported merge result, the trunk tip is the only commit left to expect;
            # the import still verifies the chain against it.
            expected_parent_commit_id=effect.merge_result_commit_id
            or prepared_status.prepared.stack.trunk.commit_id,
        )
    return OrdinaryConvergencePlan(actions=actions)


def _submitted_survivors(
    *,
    survivors: tuple[LocalCommit, ...],
    state: TrackingState,
    observation: RepoFacts,
) -> tuple[LocalCommit, ...]:
    submitted: list[LocalCommit] = []
    saw_unsubmitted = False
    for change in survivors:
        candidate = state.tracked_pr(change.change_id)
        if candidate is None:
            saw_unsubmitted = True
            continue
        if saw_unsubmitted:
            raise CliError(
                t"Cannot sync because submitted {ui.change_id(change.change_id)} appears "
                t"above an unsubmitted change.",
                hint="Submit the intervening change or select a stack that ends below it.",
            )
        pr = observation.prs[change.change_id].pr
        identity = candidate.pr_identity
        if pr is None or not identity.matches_pr(pr):
            raise CliError(
                t"The pull request no longer matches the saved link for "
                t"{ui.change_id(candidate.change_id)}.",
                hint=t"Relink the intended PR with {ui.cmd('jj-stack relink')}, or forget "
                t"the incorrect link with {ui.cmd('jj-stack unstack --local')} before "
                t"submitting again.",
            )
        lifecycle = pr.normalize_state().state
        if lifecycle != "open":
            pr_label = format_pr_label(pr.number, url=pr.html_url)
            raise CliError(
                t"{pr_label} for {ui.change_id(candidate.change_id)} is "
                t"{lifecycle}, so sync cannot update that PR.",
                hint=t"Reopen it on GitHub, or run {ui.cmd('jj-stack cleanup')} before "
                t"submitting again.",
            )
        submitted.append(change)
    return tuple(submitted)


def _trunk_evidence_kind_for(
    *,
    ancestries: dict[str, CommitAncestry],
    candidate: TrackedPR,
    observation: RepoFacts,
) -> TrunkEvidenceKind | None:
    observed = observation.prs[candidate.change_id]
    if observed.identity != candidate.pr_identity:
        raise CliError(
            t"The saved pull request link for {ui.change_id(candidate.change_id)} changed.",
            hint=t"Inspect it with {ui.cmd('jj-stack view')}, then relink the intended "
            t"PR with {ui.cmd('jj-stack relink')}.",
        )
    pr = observed.pr
    if pr is None:
        pr_label = format_pr_label(candidate.pr_identity.pr_number, repo=observation.repo)
        raise CliError(
            t"GitHub no longer reports {pr_label}.",
            hint=t"Confirm it with {ui.cmd('jj-stack view')}, then link an open "
            t"replacement with {ui.cmd('jj-stack relink')}, or forget the missing link with "
            t"{ui.cmd('jj-stack unstack --local')} before submitting again.",
        )
    evidence_kind, reason = classify_proven_kind(
        ancestries=ancestries,
        candidate=candidate,
        pr=pr,
    )
    if evidence_kind is None and pr.normalize_state().state in {"closed", "merged"}:
        raise CliError(
            t"Cannot remove {ui.change_id(candidate.change_id)}: {reason}.",
            hint="Make GitHub's reported merge commit reachable from trunk, then rerun sync.",
        )
    return evidence_kind


def _require_no_divergent_survivors(
    actions: ConvergenceActions,
    *,
    adopted: tuple[AdoptedSurvivor, ...],
) -> None:
    expected_remote_copies = {item.candidate.change_id for item in adopted}
    for change in actions.survivors:
        if change.divergent and change.change_id not in expected_remote_copies:
            raise divergent_change_error(change.change_id)


def divergent_change_error(change_id: str) -> CliError:
    return CliError(
        t"Cannot rebase remaining {ui.change_id(change_id)} because it has multiple visible "
        t"commits.",
        hint=t"Resolve the divergence with {ui.cmd('jj')}, then rerun sync for this stack.",
    )


def _classify_github_stack(
    *,
    ancestries: dict[str, CommitAncestry],
    github_stacks: tuple[GithubStack, ...],
    observation: RepoFacts,
    selected: tuple[LocalCommit, ...],
    state: TrackingState,
    trunk_branch: str,
) -> _GithubStackEffect:
    selected_by_id = {change.change_id: change for change in selected}
    by_pr = {candidate.pr_identity.pr_number: candidate for candidate in state.tracked_prs()}
    selected_prs = tuple(
        candidate.pr_identity.pr_number
        for change in selected
        if (candidate := state.tracked_pr(change.change_id)) is not None
    )
    stack = selected_github_stack(observation.repo, selected_prs, github_stacks)
    if stack is None:
        return _NoGithubStack()
    # A selected PR outside the stack, such as a child submitted with --base, is not compared.
    members = tuple(number for number in selected_prs if number in stack.pr_numbers)
    if members != tuple(number for number in stack.pr_numbers if number in members):
        raise CliError(
            t"The selected pull requests are not in the order GitHub stack #{stack.number} "
            t"records.",
            hint=t"Bring them back into line with {ui.cmd('jj-stack submit')}, or remove the "
            t"GitHub stack with {ui.cmd(f'jj-stack unstack --stack {stack.number}')} and "
            t"resubmit.",
        )
    merge_mode = _is_stack_merge(stack=stack, by_pr=by_pr)
    history: list[OnTrunkChange] = []
    adopted: list[AdoptedSurvivor] = []
    expected_base = trunk_branch
    merge_result: str | None = None
    for member in stack.prs:
        candidate = by_pr.get(member.number)
        if candidate is None:
            continue
        pr = _validated_member(candidate, member, observation)
        if member.is_historical:
            change = selected_by_id.get(candidate.change_id)
            mutable_copies = tuple(
                item
                for item in observation.prs[candidate.change_id].local_commits
                if not item.immutable
            )
            if change is None and len(mutable_copies) > 1:
                raise CliError(
                    t"Merged change {ui.change_id(candidate.change_id)} from this stack has "
                    t"more than one mutable local copy.",
                    hint=t"Resolve the divergent change with {ui.cmd('jj')}, then rerun sync.",
                )
            kind, reason = classify_proven_kind(
                ancestries=ancestries,
                candidate=candidate,
                pr=pr,
            )
            if kind is None:
                pr_label = format_pr_label(pr.number, url=pr.html_url)
                raise CliError(
                    t"Cannot remove the saved link for merged {pr_label}: {reason}.",
                    hint="Make GitHub's merge result reachable from trunk, then rerun sync.",
                )
            merge_result = pr.merge_commit_sha
            history.append(
                OnTrunkChange(
                    candidate,
                    kind,
                    SkipPRFinish(candidate),
                    change or (mutable_copies[0] if mutable_copies else None),
                )
            )
            continue
        local = selected_by_id[candidate.change_id]
        _validate_active_member(
            candidate=candidate,
            expected_base=expected_base,
            merge_mode=merge_mode,
            member=member,
            observation=observation,
            pr=pr,
            selected_change=local,
            stack=stack,
        )
        adopted.append(AdoptedSurvivor(candidate, local, member.head.sha))
        expected_base = candidate.pr_identity.head_ref
    result = tuple(adopted)
    if not merge_mode:
        if any(
            item.remote_commit_id == item.candidate.submitted_baseline.commit_id
            for item in result
        ):
            raise _unproven_rewrite_error(stack)
        return _GithubStackRebase(result)
    return _GithubStackMerge(tuple(history), result, merge_result)


def _is_stack_merge(*, stack: GithubStack, by_pr: dict[int, TrackedPR]) -> bool:
    merge_mode = any(member.number in by_pr for member in stack.historical_prs)
    if stack.historical_prs and not merge_mode:
        raise _unproven_rewrite_error(stack)
    return merge_mode


def _validated_member(
    candidate: TrackedPR,
    member: GithubStackPR,
    observation: RepoFacts,
) -> GithubPR:
    observed = observation.prs.get(candidate.change_id)
    pr = observed.pr if observed is not None else None
    identity = candidate.pr_identity
    pr_label = format_pr_label(member.number, repo=observation.repo)
    if (
        observed is None
        or observed.identity != identity
        or pr is None
        or not identity.matches_pr(pr)
        or pr.head.ref != member.head.ref
    ):
        raise CliError(
            t"{pr_label} no longer matches the saved pull request link for "
            t"{ui.change_id(candidate.change_id)}.",
            hint=t"Relink it with {ui.cmd('jj-stack relink')}, or forget the incorrect link "
            t"with {ui.cmd('jj-stack unstack --local')} before submitting again.",
        )
    return pr


def _validate_active_member(
    *,
    candidate: TrackedPR,
    expected_base: str,
    merge_mode: bool,
    member: GithubStackPR,
    observation: RepoFacts,
    pr: GithubPR,
    selected_change: LocalCommit,
    stack: GithubStack,
) -> None:
    observed = observation.prs[candidate.change_id]
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    expected = {selected_change.commit_id, member.head.sha}
    if any(
        not item.immutable and item.commit_id not in expected for item in observed.local_commits
    ):
        raise CliError(
            t"Cannot sync {ui.change_id(candidate.change_id)} because it has more than one "
            t"mutable local copy.",
            hint=t"Resolve the divergence with {ui.cmd('jj')}, then rerun sync for this stack.",
        )
    if selected_change.immutable and selected_change.commit_id != member.head.sha:
        raise CliError(
            t"GitHub still lists {pr_label} as active in stack #{stack.number}, but "
            t"{ui.change_id(candidate.change_id)} is already immutable here, so this repo "
            t"cannot tell what GitHub did with it.",
            hint=t"Check GitHub's result with {ui.cmd('jj-stack view')}, then rerun sync once it "
            t"reports the merge.",
        )
    if pr.head.sha != member.head.sha or observed.remote_pr_branch_target != member.head.sha:
        raise CliError(
            t"{pr_label}, its PR branch, and GitHub stack #{stack.number} do not all name the "
            t"same commit.",
            hint=t"Update the pull request with {ui.cmd('jj-stack submit')}, then rerun sync.",
        )
    if not merge_mode and pr.base.ref != expected_base:
        raise CliError(
            t"{pr_label} no longer has the base expected for this stack.",
            hint=t"Restore the stack on GitHub, or run "
            t"{ui.cmd(f'jj-stack unstack --stack {stack.number}')} and resubmit it.",
        )


def _unproven_rewrite_error(stack: GithubStack) -> CliError:
    return CliError(
        t"GitHub stack #{stack.number} changed, but jj-stack cannot tell how. None of its merged "
        t"pull requests is tracked here, and the whole stack was not rebased.",
        hint=t"Inspect it with {ui.cmd('jj-stack view')}. Restore or resubmit the PR "
        t"branches, then rerun sync.",
    )


def _require_no_unpublished_edits(changes: tuple[OnTrunkChange, ...]) -> None:
    for item in changes:
        local, baseline = item.change, item.candidate.submitted_baseline.commit_id
        if local is None or not local.holds_unpublished_edit(baseline):
            continue
        short = short_change_id(local.change_id)
        raise CliError(
            t"Cannot remove merged {ui.change_id(item.candidate.change_id)} because its local "
            t"commit differs from what was submitted and is not empty, so jj-stack treats it "
            t"as unpublished local work.",
            hint=t"Run {ui.cmd(f"jj rebase -s {short} -d 'trunk()'")} and rerun sync. If "
            t"{ui.cmd(f'jj diff -r {short}')} still shows changes, move anything still needed "
            t"to another change, then drop this copy with {ui.cmd(f'jj abandon {short}')} and "
            t"rerun sync, or keep it and forget its saved link with "
            t"{ui.cmd(f'jj-stack unstack --local {short}')}.",
        )


def _finish_plan(
    candidate: TrackedPR,
    observation: RepoFacts,
    allowed: bool,
) -> PRFinishPlan:
    pr = observation.prs[candidate.change_id].pr
    if not allowed or pr is None or pr.normalize_state().state != "open":
        return SkipPRFinish(candidate)
    return FinishPR(candidate, pr)


def _require_no_checked_out_merged_changes(
    changes: tuple[OnTrunkChange, ...],
) -> None:
    for item in changes:
        change = item.change
        if change is None or not change.is_working_copy:
            continue
        workspaces = change.working_copy_workspaces
        if not workspaces:
            location = "the current workspace"
        elif len(workspaces) == 1:
            location = t"workspace {ui.code(workspaces[0])}"
        else:
            location = t"workspaces {ui.join(ui.code, workspaces)}"
        raise CheckedOutMergedChangeError(
            t"Cannot remove merged {ui.change_id(item.candidate.change_id)} because it is "
            t"checked out in {location}.",
            workspaces=workspaces,
        )
