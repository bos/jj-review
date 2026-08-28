"""Shared PR checks and cleanup helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.commands.cleanup.shared import CleanupAction
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_LABEL,
    delete_stack_overview_comment,
)
from jj_stack.jj.client import JjClient, PRRefUpdate
from jj_stack.models.github import GithubIssueComment, GithubPR, GithubStack
from jj_stack.models.tracking import TrackedPR
from jj_stack.pr_branch_namespace import pr_branch_matches_change
from jj_stack.stack.pr_facts import RepoFacts, has_competing_open_pr
from jj_stack.ui import Message

ActionPresentationStatus = Literal["applied", "blocked", "planned", "skipped"]


def check_tracked_pr(
    *,
    allowed_states: frozenset[str],
    candidate: TrackedPR,
    observation: RepoFacts,
    preview_detached_dependents: frozenset[int] = frozenset(),
    require_no_open_dependents: bool = False,
) -> tuple[GithubPR | None, CleanupAction | None]:
    """Check one exact unchanged PR against shared facts."""

    change_id = candidate.change_id
    pr_identity = candidate.pr_identity
    submitted_baseline = candidate.submitted_baseline
    observed = observation.prs[change_id]
    pr_number = pr_identity.pr_number
    pr = observed.pr
    pr_label = format_pr_label(pr_number, repo=observation.repo)
    kind = "pull request"
    reason: Message | None = None
    if (observed.identity, observed.baseline) != (pr_identity, submitted_baseline):
        kind = "tracking"
        reason = (
            t"tracking for {ui.change_id(change_id)} changed while this command ran; "
            t"rerun the same command"
        )
    elif pr is None:
        reason = (
            t"{pr_label} is no longer on GitHub; attach a replacement with "
            t"{ui.cmd('jj-stack relink')}, or drop the tracking with "
            t"{ui.cmd('jj-stack unstack --local')}"
        )
    else:
        pr = pr.normalize_state()
        pr_label = format_pr_label(pr.number, url=pr.html_url)
    if reason is None:
        assert pr is not None
        if not pr_identity.matches_pr(pr):
            reason = (
                t"cannot inspect saved {pr_label} because its live PR no longer "
                t"matches {ui.bookmark(pr_identity.head_ref)}"
            )
        elif not candidate.matches_snapshot(pr):
            reason = (
                t"cannot mutate saved {pr_label} because its head no longer "
                t"matches the saved submitted commit"
            )
        elif pr.state not in allowed_states:
            reason = (
                t"cannot mutate saved {pr_label} because GitHub now reports state {pr.state!r}"
            )
    check_dependents = (reason is None, require_no_open_dependents) == (True, True)
    if check_dependents:
        open_prs_by_base = observation.open_prs_by_base
        assert open_prs_by_base is not None
        observed_dependents = open_prs_by_base.get(pr_identity.head_ref, ())
        dependents = tuple(
            filter(
                lambda item: item.number not in preview_detached_dependents,
                observed_dependents,
            )
        )
        # A full 100-result page may hide another dependent, so it also fails closed.
        blockers = dependents[:1] or observed_dependents[99:100]
        if blockers:
            kind = "remote branch"
            dependent = blockers[0]
            dependent_label = format_pr_label(dependent.number, url=dependent.html_url)
            reason = (
                t"preserve {pr_label}'s branch and tracking because open "
                t"{dependent_label} still uses {ui.bookmark(pr_identity.head_ref)} "
                t"as its base; close or retarget {dependent_label}, then rerun "
                t"{ui.cmd('cleanup')}"
            )
    return (
        pr,
        None if reason is None else CleanupAction(kind=kind, body=reason, status="blocked"),
    )


async def apply_overview_comment_cleanup(
    *,
    comment: GithubIssueComment | None,
    dry_run: bool,
    github_client: GithubClient,
    pr_number: int,
) -> tuple[tuple[CleanupAction, ...], bool]:
    """Delete one overview comment identified during cleanup planning."""

    if comment is None:
        return (), True
    deleted = True
    if not dry_run:
        try:
            deleted = await delete_stack_overview_comment(
                comment_id=comment.id,
                github_client=github_client,
            )
        except CliError as error:
            return (
                CleanupAction(
                    kind=STACK_OVERVIEW_COMMENT_LABEL,
                    body=str(error),
                    status="blocked",
                ),
            ), False
    pr_label = format_pr_label(pr_number, repo=github_client.repo)
    action_body: Message = t"delete {STACK_OVERVIEW_COMMENT_LABEL} #{comment.id} from {pr_label}"
    if not dry_run and not deleted:
        action_body = (
            t"{STACK_OVERVIEW_COMMENT_LABEL} #{comment.id} already absent from {pr_label}"
        )
    return (
        CleanupAction(
            kind=STACK_OVERVIEW_COMMENT_LABEL,
            body=action_body,
            status="planned" if dry_run else "applied",
        ),
    ), True


def emit_action_row(
    *,
    kind: str,
    status: ActionPresentationStatus,
    body: Message,
) -> None:
    prefix, prefix_style, body_style = _action_presentation(status)
    message = body
    if kind != "tracking":
        message = (ui.semantic_text(kind, "prefix"), ": ", body)
    console.output(
        ui.prefixed_line(
            f"{prefix} ",
            message,
            prefix_labels=prefix_style,
            message_labels=body_style,
        )
    )


def _action_presentation(
    status: ActionPresentationStatus,
) -> tuple[str, tuple[str, ...] | None, tuple[str, ...] | None]:
    if status == "applied":
        return (
            "  ✓",
            ("signature status good",),
            None,
        )
    if status == "planned":
        return (
            "  ~",
            ("hint heading",),
            None,
        )
    if status == "blocked":
        return (
            "  ✗",
            ("error heading",),
            ("warning heading",),
        )
    if status == "skipped":
        return (
            "  -",
            ("hint heading",),
            None,
        )
    return ("  ?", None, None)


def plan_pr_cleanup(
    *,
    allowed_states: frozenset[str],
    candidate: TrackedPR,
    observation: RepoFacts,
    preview_detached_dependents: frozenset[int] = frozenset(),
) -> tuple[GithubPR | None, PRRefUpdate | None, CleanupAction | None]:
    """Check exact cleanup facts and derive at most one leased ref deletion."""

    pr, blocker = check_tracked_pr(
        allowed_states=allowed_states,
        candidate=candidate,
        observation=observation,
        preview_detached_dependents=preview_detached_dependents,
        require_no_open_dependents=True,
    )
    if blocker is not None or pr is None:
        return pr, None, blocker
    change_id = candidate.change_id
    pr_identity = candidate.pr_identity
    submitted_baseline = candidate.submitted_baseline
    observed = observation.prs[change_id]
    if has_competing_open_pr(
        open_head_prs=observed.open_head_prs,
        pr_number=pr_identity.pr_number,
    ):
        return (
            pr,
            None,
            CleanupAction(
                kind="remote branch",
                body=t"cannot delete {ui.bookmark(pr_identity.head_ref)} because another open "
                t"pull request uses it as its head branch",
                status="blocked",
            ),
        )
    configured_repo = observation.configured_repo
    if (
        observation.remote is None
        or configured_repo is None
        or configured_repo != observation.repo
    ):
        pr_label = format_pr_label(pr_identity.pr_number, repo=observation.repo)
        return (
            pr,
            None,
            CleanupAction(
                kind="remote branch",
                body=t"cannot resolve the configured remote for saved {pr_label}",
                status="blocked",
            ),
        )
    branch = pr_identity.head_ref
    if not pr_branch_matches_change(branch, change_id):
        return (
            pr,
            None,
            CleanupAction(
                kind="tracking",
                body=t"cannot clean up {ui.bookmark(branch)} because it does not match "
                t"change {ui.change_id(change_id)}",
                status="blocked",
            ),
        )
    remote_target = observation.prs[change_id].remote_pr_branch_target
    if remote_target is not None and remote_target != submitted_baseline.commit_id:
        return (
            pr,
            None,
            CleanupAction(
                kind="remote branch",
                body=t"cannot delete {ui.bookmark(branch)} because it "
                t"already points to a different commit",
                status="blocked",
            ),
        )
    update = (
        None
        if remote_target is None
        else PRRefUpdate(
            branch=branch,
            expected_target=submitted_baseline.commit_id,
            desired_target=None,
        )
    )
    return pr, update, None


def github_stack_cleanup_blockers(
    *,
    pr_numbers: tuple[int, ...],
    stacks: tuple[GithubStack, ...] | CliError,
) -> dict[int, CleanupAction]:
    """Fail closed for every selected PR a stack with an active member still groups.

    A merged member's branch is the base of the member above it, so a stack that still holds
    an active member needs every branch it groups, not only the active ones.
    """

    if isinstance(stacks, CliError):
        return dict.fromkeys(
            pr_numbers,
            CleanupAction(kind="remote branch", body=str(stacks), status="blocked"),
        )
    selected = set(pr_numbers)
    blockers: dict[int, CleanupAction] = {}
    for stack in stacks:
        if selected.isdisjoint(stack.active_pr_numbers):
            continue
        action = CleanupAction(
            kind="remote branch",
            body=(
                f"GitHub stack #{stack.number} blocks this jj-stack operation. "
                f"Run jj-stack unstack --stack {stack.number} and retry."
            ),
            status="blocked",
        )
        blockers.update({number: action for number in stack.pr_numbers if number in selected})
    return blockers


def apply_remote_branch_cleanup(
    *,
    dry_run: bool,
    jj_client: JjClient,
    record_action: Callable[[CleanupAction], None],
    remote_name: str,
    update: PRRefUpdate | None,
) -> None:
    """Execute one prechecked remote branch deletion with an exact lease.

    A rejected lease raises, so there is no failure for callers to branch on.
    """

    if update is not None:
        if not dry_run:
            jj_client.mutate_remote_pr_branch_refs(
                remote=remote_name,
                updates=(update,),
            )
        record_action(
            CleanupAction(
                kind="remote branch",
                body=t"delete {ui.bookmark(f'{update.branch}@{remote_name}')}",
                status="planned" if dry_run else "applied",
            )
        )
