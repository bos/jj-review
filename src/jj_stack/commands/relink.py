"""Reconnect a GitHub pull request to a selected local change.

Use it when `jj-stack` has lost or mixed up the link between a pull request and its local change,
for example after tracking data was removed or a pull request was submitted from another
checkout. The pull request must be open, and its PR branch must be at the current commit of the
selected change; otherwise `relink` refuses and shows what is on the branch. Pass
`--replace-remote` to link anyway and let the next submit overwrite the branch.

`relink` changes only `jj-stack`'s local tracking data. It does not push or change anything on
GitHub.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.bootstrap import CommandContext, bootstrap_context
from jj_stack.errors import CliError, UsageError
from jj_stack.formatting import format_pr_label, format_pr_number
from jj_stack.github.client import GithubClient, build_github_client
from jj_stack.github.pr_refs import load_pr, parse_repo_pr_reference, require_managed_pr_head
from jj_stack.github.resolution import (
    GithubRepoAddress,
    require_github_repo,
    select_submit_remote,
)
from jj_stack.identifiers import short_change_id
from jj_stack.jj.cli_args import JjCliArgs
from jj_stack.models.github import GithubPR
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackedPR, TrackingState
from jj_stack.pr_branch_namespace import pr_branch_matches_change
from jj_stack.stack.change_state import (
    BranchDisagrees,
    BranchMissing,
    ChangeObservation,
    PRHeadMoved,
    classify,
    stop_error,
)
from jj_stack.stack.pr_facts import duplicate_pr_claim_change_ids
from jj_stack.stack.selected import require_submittable_changes, select_stack_path
from jj_stack.state.operation_lock import acquire_operation_lock

HELP = "Reconnect an existing pull request to a local change"


@dataclass(frozen=True, slots=True)
class RelinkResult:
    """Explicit PR relink result for one local change."""

    branch: str
    change_id: str
    pr_number: int
    pr_url: str
    subject: str


def relink(
    *,
    cli_args: JjCliArgs,
    debug: bool,
    pr: str,
    repo: Path | None,
    replace_remote: bool,
    revset: str | None,
) -> int:
    """CLI entrypoint for `relink`."""

    context = bootstrap_context(repo=repo, cli_args=cli_args, debug=debug)
    with acquire_operation_lock(context.state_store.require_writable(), command="relink"):
        result = asyncio.run(
            _run_relink_async(
                context=context,
                pr_reference=pr,
                replace_remote=replace_remote,
                revset=revset,
            )
        )
    pr_label = format_pr_label(result.pr_number, url=result.pr_url)
    console.output(
        t"Relinked {pr_label} for {result.subject} "
        t"({ui.change_id(result.change_id)}) -> {ui.bookmark(result.branch)}"
    )
    return 0


async def _run_relink_async(
    *,
    context: CommandContext,
    pr_reference: str,
    replace_remote: bool,
    revset: str | None,
) -> RelinkResult:
    client = context.jj_client
    state = context.state_store.load()
    if revset is None:
        raise UsageError(t"{ui.cmd('relink')} requires an explicit change selection.")
    stack = select_stack_path(
        jj_client=client,
        revset=revset,
        state=state,
    ).stack
    require_submittable_changes(stack.changes)
    if not stack.changes:
        raise CliError("The selected stack has no changes to link to a pull request.")
    change = stack.head
    remote = select_submit_remote(client.list_git_remotes())
    repo = require_github_repo(remote)
    pr_number = parse_repo_pr_reference(
        reference=pr_reference,
        github_repo=repo,
        invalid_reference_message=(
            f"{pr_reference} is not a pull request number or URL for {repo.full_name}."
        ),
        wrong_repo_message=(f"{pr_reference} does not belong to {repo.full_name}."),
    )
    async with build_github_client(repo=repo) as github_client:
        pr, head_sha = await _load_exact_relink_pr(
            github_client=github_client,
            pr_number=pr_number,
            repo=repo,
        )
        branch = pr.head.ref
        remote_target = (await github_client.get_branch_targets(branches=(branch,))).get(branch)
    pr_number_label = format_pr_number(pr_number, url=pr.html_url)
    identity = PRIdentity(pr_number=pr_number, head_ref=branch)
    tracked_pr = state.prs.get(change.change_id)
    retry = f"jj-stack relink {pr_number} {short_change_id(change.change_id)}"
    # Classify the link as if it were already saved: the pull request must still agree with
    # its branch, and its head must be this change's commit or the commit last submitted.
    link_state = classify(
        ChangeObservation(
            change_id=change.change_id,
            tracked=TrackedPR(
                pr_identity=identity,
                submitted_baseline=tracked_pr.submitted_baseline
                if tracked_pr is not None
                else SubmittedBaseline(commit_id=change.commit_id),
            ),
            branch=branch,
            remote_name=remote.name,
            local=(change,),
            selected=change,
            pr=pr,
            remote_target=remote_target,
        )
    )
    if isinstance(link_state, (BranchMissing, BranchDisagrees)):
        raise stop_error(link_state, rerun=retry)
    remote_head = client.read_remote_git_commit(remote=remote.name, commit_id=head_sha)
    remote_change_id = remote_head.change_id
    if (
        remote_change_id is not None
        and remote_change_id != change.change_id
        and pr_branch_matches_change(branch, remote_change_id)
    ):
        raise CliError(
            t"Pull request {pr_number_label} belongs to change "
            t"{ui.change_id(remote_change_id)}, not selected change "
            t"{ui.change_id(change.change_id)}.",
            hint=t"If {ui.change_id(remote_change_id)} still exists locally, run "
            t"{ui.cmd(f'jj-stack relink {pr_number} {short_change_id(remote_change_id)}')} "
            t"instead. If you rewrote history and {ui.change_id(change.change_id)} replaced "
            t"it, recover the original change with "
            t"{ui.cmd(f'jj-stack checkout --pull-request {pr_number}')}, then move the "
            t"intended content onto it; relink cannot assign an existing pull request to "
            t"a replacement change ID.",
        )
    if not pr_branch_matches_change(branch, change.change_id):
        raise CliError(
            t"Pull request {pr_number_label} head {ui.bookmark(branch)} was not created for "
            t"change {ui.change_id(change.change_id)}; jj-stack PR branch names end with the "
            t"change's ID."
        )
    if isinstance(link_state, PRHeadMoved) and not replace_remote:
        moved = stop_error(link_state, rerun=retry)
        raise CliError(
            (moved.message, t" The branch holds {remote_head.author}: {remote_head.subject}."),
            hint=moved.hint,
        )
    _ensure_relinkable_cached_link(
        change_id=change.change_id,
        identity=identity,
        pr_url=pr.html_url,
        state=state,
    )
    context.state_store.relink_pr(
        change.change_id,
        identity=identity,
        baseline=SubmittedBaseline(commit_id=head_sha),
    )
    return RelinkResult(
        branch=branch,
        change_id=change.change_id,
        pr_number=pr_number,
        pr_url=pr.html_url,
        subject=change.subject,
    )


async def _load_exact_relink_pr(
    *,
    github_client: GithubClient,
    pr_number: int,
    repo: GithubRepoAddress,
) -> tuple[GithubPR, str]:
    pr = await load_pr(github_client=github_client, pr_number=pr_number)
    pr_number_label = format_pr_number(pr.number, url=pr.html_url)
    if pr.state != "open":
        raise CliError(
            t"Pull request {pr_number_label} is not open; cannot relink {pr.state} PRs.",
            hint=t"Reopen it on GitHub to keep reviewing it, or forget its saved link with "
            t"{ui.cmd('jj-stack unstack --local')} and submit again.",
        )
    return pr, require_managed_pr_head(pr=pr, repo=repo)


def _ensure_relinkable_cached_link(
    *,
    change_id: str,
    identity: PRIdentity,
    pr_url: str,
    state: TrackingState,
) -> None:
    identities = {key: tracked.pr_identity for key, tracked in state.prs.items()}
    identities[change_id] = identity
    if change_id in duplicate_pr_claim_change_ids(identities):
        pr_label = format_pr_label(identity.pr_number, url=pr_url)
        raise CliError(
            t"{pr_label} or branch {ui.bookmark(identity.head_ref)} is already "
            t"linked to another local change.",
            hint=t"Run {ui.cmd('jj-stack list')} to find that change, then forget its saved link "
            t"with {ui.cmd('jj-stack unstack --local')} or clean it up with "
            t"{ui.cmd('jj-stack cleanup')}.",
        )
