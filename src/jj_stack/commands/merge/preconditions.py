"""Merge preconditions checked against fresh PR facts."""

from __future__ import annotations

import jj_stack.ui as ui
from jj_stack.commands.merge.models import MergeChange, MergePrecondition
from jj_stack.formatting import format_pr_number
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import short_change_id
from jj_stack.stack.pr_facts import PRFacts, RepoFacts
from jj_stack.ui import Message


def merge_precondition_error(
    *,
    expected_repo: GithubRepoAddress,
    expected_trunk_branch: str,
    observation: RepoFacts,
    remote_name: str,
    changes: tuple[MergeChange, ...],
    inactive_allowed: frozenset[str] = frozenset(),
) -> MergePrecondition | None:
    """Explain why fresh facts do not permit the next mutation."""

    remote = observation.remote
    if remote is None or remote.name != remote_name:
        return MergePrecondition(f"Git remote {remote_name} is no longer configured")
    if observation.configured_repo != expected_repo:
        return MergePrecondition(
            "the configured Git remote no longer names the planned GitHub repo"
        )
    github_repo = observation.github_repo
    assert github_repo is not None
    if github_repo.full_name.casefold() != expected_repo.full_name.casefold():
        return MergePrecondition("GitHub no longer reports the planned repo")
    if github_repo.default_branch not in (None, "", expected_trunk_branch):
        return MergePrecondition(
            "GitHub no longer reports the planned trunk branch as its default"
        )
    for change in changes:
        error = _merge_change_precondition_error(
            observed=observation.prs[change.change_id],
            planned=change,
            inactive_allowed=change.change_id in inactive_allowed,
        )
        if error is not None:
            return error
    return None


def explain_precondition(
    precondition: MergePrecondition,
    *,
    change_id: str,
    sync_target: str,
) -> Message:
    """Restate a precondition reason so it names the command that resolves it.

    Planning and execution both stop on these reasons, so they share one wording rather than each
    deciding what to tell the user.
    """

    reason = precondition.reason
    submit = ui.cmd(f"jj-stack submit {short_change_id(change_id)}")
    if precondition.recovery == "resolve":
        return t"it has unresolved conflicts; resolve them with jj, then run {submit}"
    if precondition.recovery == "reconcile":
        return (
            t"it has more than one visible commit; resolve the divergence, starting with "
            t"{ui.cmd('jj log -r')} {ui.revset(f'change_id({short_change_id(change_id)})')}, "
            t"then run {submit}"
        )
    if precondition.recovery == "view":
        return (
            t"it is no longer visible locally; find where it went with {ui.cmd('jj-stack view')}"
        )
    if precondition.recovery == "submit":
        return (
            t"the local change, the commit last submitted for it, and its PR branch do not "
            t"all name the same commit; run {submit}"
        )
    if precondition.recovery == "sync":
        return (
            t"{reason}, so this stack still holds a local copy of work already on trunk; run "
            t"{ui.cmd(f'jj-stack sync {sync_target}')}"
        )
    return t"{reason}; inspect it and rerun {ui.cmd('jj-stack merge')}"


def _merge_change_precondition_error(
    *,
    observed: PRFacts,
    planned: MergeChange,
    inactive_allowed: bool,
) -> MergePrecondition | None:
    """Explain why the pull request, or the local copy behind it, does not match the plan.

    GitHub's report of the pull request comes first: a merged pull request is a stop by itself,
    wherever its branch and the local copy have ended up since. Only a candidate that can still
    merge, or a completed merge being finished, goes on to the commit comparison.
    """

    pr = observed.pr
    label = short_change_id(planned.change_id)
    if pr is None:
        return MergePrecondition(f"GitHub no longer reports the saved pull request for {label}")
    pr = pr.normalize_state()
    if not planned.identity.matches_pr(pr):
        return MergePrecondition(f"the pull request linked to {label} changed")
    pr_number = format_pr_number(pr.number, url=pr.html_url)
    if pr.state != "open" and not inactive_allowed:
        return MergePrecondition(
            t"pull request {pr_number} is {pr.state}",
            recovery="sync" if pr.state == "merged" else "inspect",
        )
    if pr.is_draft and not inactive_allowed:
        return MergePrecondition(t"pull request {pr_number} is now a draft")
    # The head commit is deliberately not compared here. GitHub is given the expected head with
    # the merge request and rejects a stale one atomically, which a check made beforehand cannot
    # do; the PR branch is still compared against the submitted baseline below.
    return _local_precondition_error(observed=observed, planned=planned)


def _local_precondition_error(
    *,
    observed: PRFacts,
    planned: MergeChange,
) -> MergePrecondition | None:
    """Explain why the local change and its PR branch do not match the plan."""

    identity = observed.identity
    local_commits = observed.local_commits
    label = short_change_id(planned.change_id)
    if identity != planned.identity or identity is None:
        return MergePrecondition(f"saved PR tracking for {label} changed")
    if not local_commits:
        return MergePrecondition(f"{label} is no longer visible locally", recovery="view")
    # Stack discovery normally rejects a divergent change first; this covers one that diverged
    # after the plan was built.
    if len(local_commits) > 1 or local_commits[0].divergent:
        return MergePrecondition(
            f"{label} has more than one visible commit",
            recovery="reconcile",
        )
    local = local_commits[0]
    # Conflicts come before the commit comparison: a rebase that conflicts also changes the
    # commit, and resolving is what has to happen first either way.
    if local.conflict:
        return MergePrecondition(f"{label} has unresolved conflicts", recovery="resolve")
    if (
        observed.baseline is None
        or observed.baseline.commit_id != planned.commit_id
        or local.commit_id != planned.commit_id
        or observed.remote_pr_branch_target != planned.commit_id
    ):
        return MergePrecondition(
            f"the last submitted commit for {label} changed",
            recovery="submit",
        )
    return None
