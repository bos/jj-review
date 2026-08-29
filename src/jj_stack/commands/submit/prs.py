"""Sync pull request state on GitHub for each prepared change."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence

import jj_stack.ui as ui
from jj_stack.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_stack.errors import CliError, DriftError
from jj_stack.formatting import format_pr_label, format_pr_number
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress
from jj_stack.identifiers import short_change_id
from jj_stack.models.github import GithubPR, GithubPRReview
from jj_stack.models.tracking import (
    PRIdentity,
    SubmittedBaseline,
    TrackedPR,
    TrackingState,
)
from jj_stack.stack.pr_facts import has_competing_open_pr
from jj_stack.ui import Message

from .models import (
    PRDraftAction,
    PreparedSubmitChange,
    PRSyncPlan,
    SubmitMutationRun,
    SubmittedChange,
)


async def _github_request[Result](
    request: Awaitable[Result],
    *,
    error_message: Message,
) -> Result:
    try:
        return await request
    except GithubClientError as error:
        raise CliError(error_message) from error


async def discover_prs_by_branch(
    *,
    github_client: GithubClient,
    branches: tuple[str, ...],
    tracked_prs: Mapping[str, int],
) -> dict[str, GithubPR | None]:
    if not branches:
        return {}

    open_prs_by_branch, tracked_prs_by_number = await asyncio.gather(
        _github_request(
            github_client.get_open_prs_by_head_refs(head_refs=branches),
            error_message="Could not batch open pull request discovery for branches",
        ),
        _github_request(
            github_client.get_prs_by_numbers(pr_numbers=tuple(tracked_prs.values())),
            error_message="Could not batch saved pull request discovery by number",
        ),
    )

    return {
        branch: _select_discovered_pr(
            head_label=f"{github_client.repo.owner}:{branch}",
            open_prs=open_prs_by_branch.get(branch, ()),
            tracked_pr=(
                tracked_prs_by_number.get(tracked_prs[branch]) if branch in tracked_prs else None
            ),
            tracked_pr_number=tracked_prs.get(branch),
        )
        for branch in branches
    }


async def load_re_request_reviewers(
    *,
    github_client: GithubClient,
    prs: tuple[GithubPR, ...],
) -> dict[int, list[str]]:
    reviews = await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=prs,
        run_item=lambda pr: _github_request(
            github_client.list_pr_reviews(pr_number=pr.number),
            error_message=t"Could not load reviews for pull request "
            t"{format_pr_number(pr.number, url=pr.html_url)}",
        ),
    )
    return {
        pr.number: _reviewers_to_re_request(pr_reviews)
        for pr, pr_reviews in zip(prs, reviews, strict=True)
    }


def ensure_pr_syncs_are_safe(
    *,
    discovered_prs: Mapping[str, GithubPR | None],
    existing_only: bool,
    prepared_changes: Sequence[PreparedSubmitChange],
    repo: GithubRepoAddress,
    state: TrackingState,
) -> None:
    """Verify every planned PR sync before any mutation.

    A damaged or divergent link anywhere in the plan must stop `submit` before
    PR branches push or sibling PRs sync. Validating per change inside the
    concurrent sync phase would let a mid-stack link failure surface only after
    those mutations have already happened.
    """

    for prepared_change in prepared_changes:
        change_id = prepared_change.change.change_id
        tracked_pr = state.tracked_pr(change_id)
        pr = discovered_prs[prepared_change.branch]
        if pr is not None and pr.is_queued:
            head_change_id = prepared_changes[-1].change.change_id
            pr_label = format_pr_label(pr.number, url=pr.html_url)
            raise CliError(
                t"{pr_label} for {ui.change_id(change_id)} is in the merge "
                t"queue, so submit made no changes. Any new changes above it remain "
                t"unsubmitted.",
                hint=t"Wait for the queued PRs to merge, then run "
                t"{ui.cmd(f'jj-stack sync {short_change_id(head_change_id)}')} followed by "
                t"{ui.cmd(f'jj-stack submit {short_change_id(head_change_id)}')}.",
            )
        ensure_pr_link_is_consistent(
            branch=prepared_change.branch,
            change_id=change_id,
            discovered_pr=pr,
            expected_remote_target=prepared_change.expected_remote_target,
            repo=repo,
            tracked_pr=tracked_pr,
        )
        if existing_only and (tracked_pr is None or pr is None):
            raise CliError(
                t"Cannot sync {ui.change_id(change_id)} without its existing pull request.",
                hint=t"Repair the PR link with {ui.cmd('relink')} before retrying.",
            )


async def sync_prs(
    *,
    github_client: GithubClient,
    plans: tuple[PRSyncPlan, ...],
    run: SubmitMutationRun,
    on_progress: Callable[[], None] | None = None,
) -> tuple[SubmittedChange, ...]:
    def handle_success(_index: int, _submitted: SubmittedChange) -> None:
        if on_progress is not None:
            on_progress()

    submitted_changes = await run_bounded_tasks(
        concurrency=DEFAULT_BOUNDED_CONCURRENCY,
        items=plans,
        run_item=lambda plan: _sync_pr(
            github_client=github_client,
            plan=plan,
            run=run,
        ),
        on_success=handle_success,
    )
    return tuple(submitted_changes)


async def _sync_pr(
    *,
    github_client: GithubClient,
    plan: PRSyncPlan,
    run: SubmitMutationRun,
) -> SubmittedChange:
    prepared_change = plan.prepared
    branch = prepared_change.branch
    change_id = prepared_change.change.change_id
    pr = plan.discovered_pr
    pr_identity = run.state.pr_identities.get(change_id)
    action = plan.action
    base_update, body_update, title_update = plan.content_updates

    if action == "created":
        if not run.dry_run:
            pr = await _github_request(
                github_client.create_pr(
                    base=plan.base_branch,
                    body=plan.generated_description.body,
                    draft=plan.draft,
                    head=branch,
                    title=plan.generated_description.title,
                ),
                error_message=t"Could not create a pull request for branch {ui.bookmark(branch)}",
            )
    elif (
        any(update is not None for update in (base_update, body_update, title_update))
        and not run.dry_run
    ):
        assert pr is not None
        pr_number = format_pr_number(pr.number, url=pr.html_url)
        pr = await _github_request(
            github_client.update_pr(
                pr_number=pr.number,
                base=base_update,
                body=body_update,
                title=title_update,
            ),
            error_message=t"Could not update pull request {pr_number}",
        )

    if pr is not None and not run.dry_run:
        # Save the PR link as soon as GitHub acknowledges the pull request and the pushed
        # branch. Draft state, labels and reviewers can be observed and rewritten on a
        # rerun, but a pull request submit created and never recorded leaves the change
        # untracked, and every retry then demands an explicit relink.
        run.record_submission(
            baseline=SubmittedBaseline(commit_id=prepared_change.change.commit_id),
            change_id=change_id,
            identity=_submitted_identity(
                branch=branch,
                pr=pr,
                pr_identity=pr_identity,
            ),
        )
        pr = await _apply_draft_action(
            action=plan.draft_action,
            github_client=github_client,
            pr=pr,
        )
        if plan.metadata is not None:
            await _sync_pr_metadata(
                github_client=github_client,
                labels=plan.metadata.labels,
                pr_number=pr.number,
                reviewers=plan.metadata.reviewers,
                team_reviewers=plan.metadata.team_reviewers,
            )

    return SubmittedChange(
        prepared=prepared_change,
        pr_action=action,
        pr_is_draft=(pr.is_draft if pr is not None else None),
        pr_number=(pr.number if pr is not None else None),
        pr_url=(pr.html_url if pr is not None else None),
    )


async def _apply_draft_action(
    *,
    action: PRDraftAction | None,
    github_client: GithubClient,
    pr: GithubPR,
) -> GithubPR:
    if action is None:
        return pr
    pr_number = format_pr_number(pr.number, url=pr.html_url)
    message = (
        t"Could not return pull request {pr_number} to draft for {github_client.repo.full_name}"
        if action == "draft"
        else t"Could not mark draft pull request {pr_number} ready for review for "
        t"{github_client.repo.full_name}"
    )
    if pr.node_id is None:
        raise CliError((message, ": GitHub did not return a node ID."))
    request = (
        github_client.convert_pr_to_draft(pr_id=pr.node_id)
        if action == "draft"
        else github_client.mark_pr_ready_for_review(pr_id=pr.node_id)
    )
    return await _github_request(request, error_message=message)


def _reviewers_to_re_request(
    reviews: Sequence[GithubPRReview],
) -> list[str]:
    latest_reviews_by_user: dict[str, GithubPRReview] = {}
    for review in sorted(reviews, key=lambda item: item.id):
        reviewer = review.user
        if reviewer is None:
            continue
        normalized_state = review.state.upper()
        if normalized_state not in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            continue
        latest_reviews_by_user[reviewer.login] = review

    selected_reviews = sorted(
        (
            review
            for review in latest_reviews_by_user.values()
            if review.state.upper() in {"APPROVED", "CHANGES_REQUESTED"}
        ),
        key=lambda item: item.id,
    )
    return [review.user.login for review in selected_reviews if review.user is not None]


def _select_discovered_pr(
    *,
    head_label: str,
    open_prs: tuple[GithubPR, ...],
    tracked_pr: GithubPR | None,
    tracked_pr_number: int | None,
) -> GithubPR | None:
    ambiguous = len(open_prs) > 1
    if tracked_pr_number is not None and tracked_pr is not None:
        ambiguous = ambiguous or has_competing_open_pr(
            open_head_prs=open_prs,
            pr_number=tracked_pr_number,
        )
    if ambiguous:
        raise DriftError(
            t"GitHub reports multiple pull requests for head branch {ui.bookmark(head_label)}.",
            condition="pr_ambiguous",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if tracked_pr_number is not None:
        return tracked_pr
    return open_prs[0] if open_prs else None


def ensure_pr_link_is_consistent(
    *,
    branch: str,
    change_id: str,
    discovered_pr: GithubPR | None,
    expected_remote_target: str | None,
    repo: GithubRepoAddress | None = None,
    tracked_pr: TrackedPR | None,
    merged_hint: Message | None = None,
) -> None:
    if tracked_pr is None:
        if discovered_pr is not None:
            pr_label = format_pr_label(discovered_pr.number, url=discovered_pr.html_url)
            raise DriftError(
                t"GitHub already reports {pr_label} for untracked branch {ui.bookmark(branch)}.",
                condition="saved_pr_missing",
                hint=t"Adopt that PR explicitly with {ui.cmd('relink')} before submitting.",
            )
        return
    pr_identity = tracked_pr.pr_identity
    if pr_identity.head_ref != branch:
        raise DriftError(
            t"Saved PR tracking for {ui.change_id(change_id)} names branch "
            t"{ui.bookmark(pr_identity.head_ref)}, not {ui.bookmark(branch)}.",
            condition="saved_pr_mismatch",
            hint=t"Run {ui.cmd('relink')} before submitting again.",
        )
    if discovered_pr is None:
        raise DriftError(
            t"Saved pull request link exists for branch {ui.bookmark(branch)}, "
            t"but GitHub no longer reports a PR for that head branch.",
            condition="saved_pr_missing",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    discovered_pr = discovered_pr.normalize_state()
    discovered_number = format_pr_number(discovered_pr.number, url=discovered_pr.html_url)
    discovered_label = format_pr_label(discovered_pr.number, url=discovered_pr.html_url)
    if pr_identity.pr_number != discovered_pr.number:
        saved_label = format_pr_number(pr_identity.pr_number, repo=repo)
        raise DriftError(
            t"Saved pull request {saved_label} does not match the PR "
            t"GitHub reports for branch {ui.bookmark(branch)} "
            t"({discovered_number}).",
            condition="saved_pr_mismatch",
            hint=(
                t"Inspect the PR link with {ui.cmd('view')} and repair it "
                t"with {ui.cmd('relink')} before submitting again."
            ),
        )
    if discovered_pr.state != "open":
        short = short_change_id(change_id)
        hint = (
            merged_hint
            if discovered_pr.state == "merged" and merged_hint is not None
            else t"Run {ui.cmd(f'jj-stack sync {short}')} to update the local stack."
            if discovered_pr.state == "merged"
            else t"Reopen the PR, or run {ui.cmd(f'jj-stack cleanup {short}')} before "
            t"submitting a new PR."
        )
        raise DriftError(
            t"{discovered_label} for {ui.change_id(change_id)} is "
            t"{discovered_pr.state} and cannot be updated.",
            condition="pr_not_open",
            hint=hint,
        )
    if expected_remote_target is None or discovered_pr.head.sha != expected_remote_target:
        raise DriftError(
            t"Pull request {discovered_number} and its remote branch no longer "
            t"identify the same commit.",
            condition="remote_branch_moved",
            hint=t"Inspect it with {ui.cmd('view')} before submitting again.",
        )


async def _sync_pr_metadata(
    *,
    github_client: GithubClient,
    labels: list[str],
    pr_number: int,
    reviewers: list[str],
    team_reviewers: list[str],
) -> None:
    try:
        if reviewers or team_reviewers:
            await github_client.request_reviewers(
                pr_number=pr_number,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        if labels:
            await github_client.add_labels(
                issue_number=pr_number,
                labels=labels,
            )
    except GithubClientError as error:
        pr_label = format_pr_number(pr_number, repo=github_client.repo)
        raise CliError(t"Could not synchronize metadata for pull request {pr_label}") from error


def _submitted_identity(
    *,
    branch: str,
    pr: GithubPR,
    pr_identity: PRIdentity | None,
) -> PRIdentity:
    if pr_identity is None:
        return PRIdentity(
            pr_number=pr.number,
            head_ref=branch,
        )
    return pr_identity
