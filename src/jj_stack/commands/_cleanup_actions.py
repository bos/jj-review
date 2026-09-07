"""Shared PR checks and cleanup helpers."""

from __future__ import annotations

from collections.abc import Callable

import jj_stack.console as console
import jj_stack.ui as ui
from jj_stack.commands.cleanup.shared import CleanupAction, CleanupActionStatus
from jj_stack.errors import CliError
from jj_stack.formatting import format_pr_label
from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.overview_comments import (
    STACK_OVERVIEW_COMMENT_LABEL,
    delete_stack_overview_comment,
)
from jj_stack.identifiers import CommitId
from jj_stack.jj.client import JjClient, PRRefUpdate
from jj_stack.models.github import GithubIssueComment, GithubPR, GithubStack
from jj_stack.models.tracking import TrackedPR
from jj_stack.stack.change_state import (
    ChangeState,
    CompetingOpenPR,
    PRAmbiguous,
    PRIdentityMismatch,
    PRMissing,
    Unobserved,
    classify,
    live_pr,
)
from jj_stack.stack.pr_facts import RepoFacts
from jj_stack.ui import Message


def check_tracked_pr(
    *,
    allowed_states: frozenset[str],
    candidate: TrackedPR,
    change_id: str,
    observation: RepoFacts,
    preview_detached_dependents: frozenset[int] = frozenset(),
    require_no_dependents: bool = False,
) -> tuple[ChangeState | None, CleanupAction | None]:
    """Classify one saved PR and check that it is still the saved one in an allowed state."""

    pr_identity = candidate.pr_identity
    observed = observation.prs[change_id]
    if observed.tracked != candidate:
        return None, CleanupAction(
            kind="tracking",
            body=t"tracking for {ui.change_id(change_id)} changed while this command ran; "
            t"rerun the same command",
            status="blocked",
        )
    state = classify(observation.prs[change_id])
    if isinstance(state, (PRMissing, PRAmbiguous, PRIdentityMismatch)):
        return None, CleanupAction(
            kind="pull request",
            body=t"{state.reason}; {state.repair}",
            status="blocked",
        )
    pr = live_pr(state)
    if pr is None:
        raise AssertionError("A tracked pull request lookup must report the pull request.")
    pr_label = format_pr_label(pr.number, url=pr.html_url)
    if pr.state not in allowed_states:
        return state, CleanupAction(
            kind="pull request",
            body=t"cannot update {pr_label}: GitHub now reports it as {pr.state!r}",
            status="blocked",
        )
    if not require_no_dependents:
        return state, None
    prs_by_base = observation.prs_by_base
    assert not isinstance(prs_by_base, Unobserved)
    observed_dependents = prs_by_base.get(pr_identity.head_ref, ())
    dependents = tuple(
        item
        for item in observed_dependents
        if item.number not in preview_detached_dependents
        # GitHub can never reopen a closed PR whose head branch is gone, so its base is free.
        and (item.state == "open" or item.head_branch_exists)
    )
    # A full 100-result page may hide another dependent, so it also fails closed.
    blockers = dependents[:1] or observed_dependents[99:100]
    if not blockers:
        return state, None
    dependent = blockers[0]
    dependent_label = format_pr_label(dependent.number, url=dependent.html_url)
    recovery = (
        t"retarget {dependent_label} to its new base"
        if dependent.state == "open"
        else t"reopen and retarget {dependent_label} to its new base, or delete its head branch "
        t"if you no longer need to reopen it"
    )
    return state, CleanupAction(
        kind="remote branch",
        body=t"keep {pr_label}'s branch and saved link because "
        t"{dependent_label} still uses {ui.bookmark(pr_identity.head_ref)} "
        t"as its base; deleting the base branch would prevent reopening {dependent_label}. "
        t"To continue, {recovery}, then rerun {ui.cmd('jj-stack cleanup')}",
        status="blocked",
    )


async def close_pr_on_trunk(
    *,
    github_client: GithubClient,
    pr: GithubPR,
    trunk_branch: str,
) -> Message | None:
    """Retarget one open PR to trunk, then close it, so GitHub can still reopen it later.

    GitHub refuses to retarget a closed PR and to reopen one whose base branch is gone.
    Returns why the PR is still open, or None once GitHub reports it closed.
    """

    pr_label = format_pr_label(pr.number, url=pr.html_url)
    try:
        if pr.base.ref != trunk_branch:
            pr = await github_client.update_pr(pr_number=pr.number, base=trunk_branch)
            if pr.state == "open" and pr.base.ref != trunk_branch:
                return (
                    t"cannot close {pr_label} because GitHub did not retarget it to "
                    t"{ui.bookmark(trunk_branch)}"
                )
        if pr.state == "open":
            await github_client.close_pr(pr_number=pr.number)
    except GithubClientError as error:
        return t"cannot close {pr_label}: {error}"
    return None


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
    status: CleanupActionStatus,
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
    status: CleanupActionStatus,
) -> tuple[str, tuple[str, ...], tuple[str, ...] | None]:
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
    return (
        "  -",
        ("hint heading",),
        None,
    )


def plan_pr_cleanup(
    *,
    allowed_states: frozenset[str],
    candidate: TrackedPR,
    change_id: str,
    observation: RepoFacts,
    preview_detached_dependents: frozenset[int] = frozenset(),
) -> tuple[PRRefUpdate | None, CleanupAction | None]:
    """Check cleanup eligibility and lease the PR branch deletion to its observed target."""

    state, blocker = check_tracked_pr(
        allowed_states=allowed_states,
        candidate=candidate,
        change_id=change_id,
        observation=observation,
        preview_detached_dependents=preview_detached_dependents,
        require_no_dependents=True,
    )
    if blocker is not None or state is None:
        return None, blocker
    pr_identity = candidate.pr_identity
    branch = pr_identity.head_ref
    if isinstance(state, CompetingOpenPR):
        return (
            None,
            CleanupAction(
                kind="remote branch",
                body=t"cannot delete {ui.bookmark(branch)}: {state.reason}; {state.repair}",
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
            None,
            CleanupAction(
                kind="remote branch",
                body=t"cannot determine which Git remote belongs to {pr_label}; run "
                t"{ui.cmd('jj-stack doctor')} to check the repo setup",
                status="blocked",
            ),
        )
    remote_target = observation.prs[change_id].remote_target
    if isinstance(remote_target, Unobserved):
        raise AssertionError("Branch cleanup requires an observed remote target.")
    update = (
        None
        if remote_target is None
        else PRRefUpdate(
            branch=branch,
            expected_target=CommitId(remote_target),
            desired_target=None,
        )
    )
    return update, None


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
                f"GitHub stack #{stack.number} still groups this pull request. "
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
