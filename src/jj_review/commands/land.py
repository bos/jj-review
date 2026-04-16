"""Land the consecutive changes above `trunk()` that are ready to land now.

By default, `land` requires each pull request to be open, not draft, approved,
and free of outstanding changes requested. Use `--bypass-readiness` to skip
those checks while still enforcing the normal safety checks.

By default, this command performs the landing. Use `--dry-run` to inspect the
landing plan without mutating jj or GitHub state.

Use `--pull-request` to select the linked local change by pull request number
or URL and land the consecutive ready prefix through that change.

After a successful land, `jj-review` forgets the local `review/...` bookmarks
for the changes that actually landed when those bookmarks still point at the
landed commits. Use `--skip-cleanup` to keep those local review bookmarks.

If later changes remain above that point, run `cleanup --restack` and then
`submit` to keep those remaining changes under review.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.resolution import (
    ParsedGithubRepo,
    resolve_trunk_branch,
)
from jj_review.models.bookmarks import BookmarkState
from jj_review.models.github import GithubPullRequest
from jj_review.models.intent import LandIntent, LoadedIntent
from jj_review.models.review_state import CachedChange
from jj_review.review.intents import (
    describe_intent,
    match_ordered_change_ids,
    retire_superseded_intents,
)
from jj_review.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_review.review.status import (
    PreparedRevision,
    PreparedStatus,
    ReviewStatusRevision,
    StatusResult,
    prepare_status,
    stream_status,
)
from jj_review.state.intents import check_same_kind_intent, save_intent, write_new_intent
from jj_review.ui import Message, plain_text

HELP = "Land the ready changes at the bottom of a stack"

LandActionStatus = Literal["applied", "blocked", "planned"]
type LandActionBody = Message


@dataclass(frozen=True, slots=True)
class LandAction:
    """One planned, applied, or blocked landing action."""

    kind: str
    body: LandActionBody
    status: LandActionStatus

    @property
    def message(self) -> str:
        """Return the plain-text form of this action body."""

        return plain_text(self.body)


@dataclass(frozen=True, slots=True)
class LandResult:
    """Rendered landing result for one selected local stack."""

    actions: tuple[LandAction, ...]
    applied: bool
    bypass_readiness: bool
    blocked: bool
    github_repository: str
    remote_name: str
    selected_revset: str
    trunk_branch: str
    trunk_subject: str
    follow_up: LandActionBody | None


@dataclass(frozen=True, slots=True)
class PreparedLand:
    """Locally prepared land inputs before GitHub planning and execution."""

    cleanup_bookmarks: bool
    dry_run: bool
    bypass_readiness: bool
    config: RepoConfig
    prepared_status: PreparedStatus
    selected_pr_number: int | None


@dataclass(frozen=True, slots=True)
class _LandRevision:
    """One landed change plus its GitHub link."""

    bookmark: str
    change_id: str
    commit_id: str
    pull_request_number: int
    subject: str


@dataclass(frozen=True, slots=True)
class _LandPlan:
    """Resolved landing plan for the selected stack."""

    blocked: bool
    boundary_action: LandAction | None
    landed_revisions: tuple[_LandRevision, ...]
    push_trunk: bool
    trunk_branch: str


@dataclass(frozen=True, slots=True)
class _ReviewBookmarkCleanupPlan:
    """Planned post-land cleanup for one landed local review bookmark."""

    action: LandAction
    bookmark: str
    can_forget: bool
    change_id: str


@dataclass(frozen=True, slots=True)
class _ResumeLandIntent:
    """A stale land intent that still matches the current selected stack."""

    intent: LandIntent
    path: Path
    mode: Literal["exact-path", "tail-after-landed-prefix"]


@dataclass(frozen=True, slots=True)
class _LandExecutionState:
    """Resolved live-run land state after resume checks."""

    execution_plan: _LandPlan
    follow_up: LandActionBody | None
    resume_intent: _ResumeLandIntent | None
    stale_intents: list[LoadedIntent]
    state_dir: Path


class _BookmarkStateReader(Protocol):
    """Subset of the jj client interface needed for trunk bookmark inspection."""

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        """Return local and remote state for the named bookmark."""


class _BookmarkRestorer(Protocol):
    """Subset of the jj client interface needed for local trunk restoration."""

    def forget_bookmarks(self, bookmarks: Sequence[str]) -> None:
        """Forget local bookmarks."""

    def set_bookmark(
        self,
        bookmark: str,
        revision: str,
        *,
        allow_backwards: bool = False,
    ) -> None:
        """Create or move a local bookmark."""


def land(
    *,
    bypass_readiness: bool,
    config_path: Path | None,
    debug: bool,
    dry_run: bool,
    pull_request: str | None,
    repository: Path | None,
    revset: str | None,
    skip_cleanup: bool,
) -> int:
    """CLI entrypoint for `land`."""

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    if pull_request is not None:
        pull_request_number, resolved_revset = _resolve_land_revset_for_pull_request(
            pull_request_reference=pull_request,
            repo_root=context.repo_root,
            revset=revset,
        )
        console.note(
            t"Using PR #{pull_request_number} -> {ui.revset(resolved_revset)}"
        )
    else:
        pull_request_number = None
        resolved_revset = resolve_selected_revset(
            command_label="land",
            default_revset="@-",
            require_explicit=False,
            revset=revset,
        )
    prepared_land = prepare_land(
        cleanup_bookmarks=not skip_cleanup,
        dry_run=dry_run,
        bypass_readiness=bypass_readiness,
        change_overrides=context.config.change,
        config=context.config.repo,
        repo_root=context.repo_root,
        revset=resolved_revset,
        selected_pr_number=pull_request_number,
    )
    result = stream_land(prepared_land=prepared_land)
    _print_land_result(result)
    return 1 if result.blocked else 0


def _resolve_land_revset_for_pull_request(
    *,
    pull_request_reference: str,
    repo_root: Path,
    revset: str | None,
) -> tuple[int, str]:
    return resolve_linked_change_for_pull_request(
        action_name="land",
        pull_request_reference=pull_request_reference,
        repo_root=repo_root,
        revset=revset,
    )


def _print_land_result(result: LandResult) -> None:
    console.output(t"Trunk: {result.trunk_subject} -> {ui.bookmark(result.trunk_branch)}")
    if result.actions:
        if result.applied:
            header = "Applied land actions:"
        elif result.blocked:
            header = "Land blocked:"
        else:
            header = "Planned land actions:"
        console.output(header)
        for action in result.actions:
            prefix, prefix_style, body_style = _land_action_presentation(action.status)
            console.output(
                ui.prefixed_line(
                    f"{prefix} ",
                    (ui.semantic_text(action.kind, "prefix"), ": ", action.body),
                    prefix_labels=prefix_style,
                    message_labels=body_style,
                )
            )
    if result.follow_up is not None:
        console.output(result.follow_up)


def _land_action_presentation(
    status: LandActionStatus,
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
    return ("  ?", None, None)


def prepare_land(
    *,
    cleanup_bookmarks: bool,
    dry_run: bool,
    bypass_readiness: bool,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    repo_root: Path,
    revset: str | None,
    selected_pr_number: int | None,
) -> PreparedLand:
    """Resolve local landing inputs before GitHub planning and execution."""

    prepared_status = prepare_status(
        change_overrides=change_overrides,
        config=config,
        fetch_remote_state=True,
        repo_root=repo_root,
        revset=revset,
    )
    prepared = prepared_status.prepared
    if prepared.remote is None:
        message = prepared.remote_error or t"Could not determine which Git remote to use."
        raise CliError(message)
    if prepared_status.github_repository is None:
        message = prepared_status.github_repository_error or t"Could not resolve GitHub target."
        raise CliError(message)

    if not dry_run:
        prepared.state_store.require_writable()
    return PreparedLand(
        cleanup_bookmarks=cleanup_bookmarks,
        dry_run=dry_run,
        bypass_readiness=bypass_readiness,
        config=config,
        prepared_status=prepared_status,
        selected_pr_number=selected_pr_number,
    )


def stream_land(*, prepared_land: PreparedLand) -> LandResult:
    """Inspect GitHub state for the prepared path and optionally execute `land`."""

    prepared_status = prepared_land.prepared_status
    github_repository = getattr(prepared_status, "github_repository", None)
    progress_total = (
        len(prepared_status.prepared.status_revisions) if github_repository is not None else 0
    )
    with console.progress(description="Inspecting GitHub", total=progress_total) as progress:
        status_result = stream_status(
            on_revision=lambda _revision, _github_available: progress.advance(),
            prepared_status=prepared_status,
        )
    return asyncio.run(
        _stream_land_async(
            prepared_land=prepared_land,
            status_result=status_result,
        )
    )


async def _stream_land_async(
    *,
    prepared_land: PreparedLand,
    status_result: StatusResult,
) -> LandResult:
    prepared_status = prepared_land.prepared_status
    prepared = prepared_status.prepared
    if status_result.github_error is not None:
        raise CliError(
            t"Could not inspect GitHub pull request state for {ui.cmd('land')}: "
            t"{status_result.github_error}"
        )

    github_repository = prepared_status.github_repository
    remote = prepared.remote
    if github_repository is None or remote is None:
        raise AssertionError("Prepared land requires resolved GitHub and remote targets.")

    async with build_github_client(base_url=github_repository.api_base_url) as github_client:
        try:
            github_repository_state = await github_client.get_repository(
                github_repository.owner,
                github_repository.repo,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not load GitHub repository {github_repository.full_name}: {error}"
            ) from error
        trunk_branch = resolve_trunk_branch(
            bookmark_states=prepared.client.list_bookmark_states(),
            github_repository_state=github_repository_state,
            remote_name=remote.name,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        _ensure_trunk_branch_matches_selected_trunk(
            client=prepared.client,
            remote_name=remote.name,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        plan = _build_land_plan(
            bypass_readiness=prepared_land.bypass_readiness,
            prepared_status=prepared_status,
            status_result=status_result,
            trunk_branch=trunk_branch,
        )
        follow_up = _follow_up_message(
            landed_change_count=len(plan.landed_revisions),
            selected_revset=status_result.selected_revset,
            total_change_count=len(prepared_status.prepared.status_revisions),
        )
        bookmark_cleanup_plans = _plan_review_bookmark_cleanup_for_revisions(
            client=prepared.client,
            cleanup_bookmarks=prepared_land.cleanup_bookmarks,
            landed_revisions=plan.landed_revisions,
        )
        if prepared_land.dry_run:
            return LandResult(
                actions=_planned_land_actions(
                    plan=plan,
                    bookmark_cleanup_plans=bookmark_cleanup_plans,
                ),
                applied=False,
                bypass_readiness=prepared_land.bypass_readiness,
                blocked=plan.blocked,
                follow_up=None if plan.blocked else follow_up,
                github_repository=github_repository.full_name,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=status_result.trunk_subject,
            )

        try:
            execution_state = _prepare_land_execution_state(
                follow_up=follow_up,
                github_repository=github_repository,
                plan=plan,
                prepared_land=prepared_land,
                prepared_status=prepared_status,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=status_result.trunk_subject,
            )
        except _CompletedLandResume as resume:
            return resume.result
        execution_plan = execution_state.execution_plan
        follow_up = execution_state.follow_up
        if execution_plan.blocked:
            return LandResult(
                actions=_planned_land_actions(plan=execution_plan),
                applied=False,
                bypass_readiness=prepared_land.bypass_readiness,
                blocked=True,
                follow_up=None,
                github_repository=github_repository.full_name,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=status_result.trunk_subject,
            )

        state = prepared.state_store.load()
        state_changes = dict(state.changes)
        land_intent = (
            execution_state.resume_intent.intent
            if execution_state.resume_intent is not None
            else _build_land_intent(
                bypass_readiness=prepared_land.bypass_readiness,
                cleanup_bookmarks=prepared_land.cleanup_bookmarks,
                landed_revisions=execution_plan.landed_revisions,
                prepared_status=prepared_status,
                selected_pr_number=prepared_land.selected_pr_number,
                trunk_branch=trunk_branch,
            )
        )
        intent_path = (
            execution_state.resume_intent.path
            if execution_state.resume_intent is not None
            else write_new_intent(execution_state.state_dir, land_intent)
        )

        actions: list[LandAction] = []
        succeeded = False
        bookmark_cleanup_by_change_id = {
            cleanup_plan.change_id: cleanup_plan for cleanup_plan in bookmark_cleanup_plans
        }
        original_trunk_target = prepared.client.get_bookmark_state(trunk_branch).local_target
        try:
            if execution_plan.push_trunk:
                try:
                    prepared.client.set_bookmark(
                        trunk_branch,
                        execution_plan.landed_revisions[-1].commit_id,
                    )
                    prepared.client.push_bookmarks(
                        remote=remote.name,
                        bookmarks=(trunk_branch,),
                    )
                except BaseException:
                    _restore_local_trunk_bookmark(
                        client=prepared.client,
                        original_target=original_trunk_target,
                        trunk_branch=trunk_branch,
                    )
                    raise
                actions.append(
                    LandAction(
                        kind="trunk",
                        body=t"push {ui.bookmark(trunk_branch)} to "
                        t"{execution_plan.landed_revisions[-1].subject} "
                        t"{ui.change_id(execution_plan.landed_revisions[-1].change_id)}",
                        status="applied",
                    )
                )
            for landed_revision in execution_plan.landed_revisions:
                console.output(
                    t"Finalizing PR #{landed_revision.pull_request_number} for "
                    t"{landed_revision.subject} "
                    t"{ui.change_id(landed_revision.change_id)}..."
                )
                final_pull_request = await _finalize_landed_pull_request(
                    cached_change=state_changes.get(landed_revision.change_id),
                    github_client=github_client,
                    github_repository=github_repository,
                    landed_revision=landed_revision,
                    trunk_branch=trunk_branch,
                )
                actions.append(
                    LandAction(
                        kind="pull request",
                        body=t"finalize PR #{landed_revision.pull_request_number} for "
                        t"{landed_revision.subject} "
                        t"{ui.change_id(landed_revision.change_id)}",
                        status="applied",
                    )
                )
                state_changes[landed_revision.change_id] = _updated_landed_change(
                    bookmark=landed_revision.bookmark,
                    cached_change=state_changes.get(landed_revision.change_id),
                    commit_id=landed_revision.commit_id,
                    pull_request=final_pull_request,
                )
                prepared.state_store.save(
                    state.model_copy(update={"changes": dict(state_changes)})
                )
                cleanup_plan = bookmark_cleanup_by_change_id.get(landed_revision.change_id)
                if cleanup_plan is not None:
                    if cleanup_plan.can_forget:
                        prepared.client.forget_bookmarks((cleanup_plan.bookmark,))
                        actions.append(
                            LandAction(
                        kind="local bookmark",
                        body=t"forget {ui.bookmark(cleanup_plan.bookmark)} "
                        t"for {ui.change_id(landed_revision.change_id)}",
                        status="applied",
                            )
                        )
                    else:
                        actions.append(cleanup_plan.action)
                land_intent = land_intent.model_copy(
                    update={
                        "completed_change_ids": tuple(
                            dict.fromkeys(
                                (*land_intent.completed_change_ids, landed_revision.change_id)
                            )
                        )
                    }
                )
                save_intent(intent_path, land_intent)
            succeeded = True
            return LandResult(
                actions=tuple(actions),
                applied=True,
                bypass_readiness=prepared_land.bypass_readiness,
                blocked=False,
                follow_up=follow_up,
                github_repository=github_repository.full_name,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=status_result.trunk_subject,
            )
        finally:
            if succeeded:
                retire_superseded_intents(execution_state.stale_intents, land_intent)
                intent_path.unlink(missing_ok=True)


def _prepare_land_execution_state(
    *,
    follow_up: LandActionBody | None,
    github_repository: ParsedGithubRepo,
    plan: _LandPlan,
    prepared_land: PreparedLand,
    prepared_status: PreparedStatus,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_subject: str,
) -> _LandExecutionState:
    """Resolve resume state before live execution."""

    state_dir = prepared_status.prepared.state_store.require_writable()

    current_landed_change_ids = tuple(revision.change_id for revision in plan.landed_revisions)
    stale_intents = check_same_kind_intent(
        state_dir,
        _build_land_intent(
            bypass_readiness=prepared_land.bypass_readiness,
            cleanup_bookmarks=prepared_land.cleanup_bookmarks,
            landed_revisions=plan.landed_revisions,
            prepared_status=prepared_status,
            selected_pr_number=prepared_land.selected_pr_number,
            trunk_branch=trunk_branch,
        ),
    )
    resume_intent = _find_resume_land_intent(
        bypass_readiness=prepared_land.bypass_readiness,
        cleanup_bookmarks=prepared_land.cleanup_bookmarks,
        current_landed_change_ids=current_landed_change_ids,
        prepared_status=prepared_status,
        selected_pr_number=prepared_land.selected_pr_number,
        stale_intents=stale_intents,
        trunk_branch=trunk_branch,
    )
    _report_stale_land_intents(
        current_landed_change_ids=current_landed_change_ids,
        prepared_status=prepared_status,
        resume_intent=resume_intent,
        stale_intents=stale_intents,
    )

    execution_plan = plan
    trunk_transition_already_succeeded = (
        resume_intent is not None
        and _remote_trunk_matches_commit(
            client=prepared_status.prepared.client,
            remote_name=remote_name,
            trunk_branch=trunk_branch,
            commit_id=resume_intent.intent.landed_commit_id,
        )
    )
    if trunk_transition_already_succeeded and resume_intent is not None:
        execution_plan = _resume_land_plan(
            intent=resume_intent.intent,
            trunk_branch=trunk_branch,
        )
        follow_up = _follow_up_message(
            landed_change_count=len(resume_intent.intent.landed_change_ids),
            selected_revset=selected_revset,
            total_change_count=len(resume_intent.intent.ordered_change_ids),
        )

    if not execution_plan.landed_revisions and not execution_plan.push_trunk:
        if resume_intent is not None:
            retire_superseded_intents(stale_intents, resume_intent.intent)
            resume_intent.path.unlink(missing_ok=True)
        raise _CompletedLandResume(
            LandResult(
                actions=(
                    LandAction(
                        kind="resume",
                        body="previous landing already completed; cleared stale intent",
                        status="applied",
                    ),
                ),
                applied=True,
                bypass_readiness=prepared_land.bypass_readiness,
                blocked=False,
                follow_up=follow_up,
                github_repository=github_repository.full_name,
                remote_name=remote_name,
                selected_revset=selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=trunk_subject,
            )
        )

    if not execution_plan.push_trunk and not execution_plan.landed_revisions:
        raise AssertionError("Resume execution without remaining work must be handled above.")
    return _LandExecutionState(
        execution_plan=execution_plan,
        follow_up=follow_up,
        resume_intent=resume_intent,
        stale_intents=stale_intents,
        state_dir=state_dir,
    )


class _CompletedLandResume(Exception):
    """Internal sentinel used when a resumed land already finished previously."""

    def __init__(self, result: LandResult) -> None:
        super().__init__("completed land resume")
        self.result = result


def _report_stale_land_intents(
    *,
    current_landed_change_ids: tuple[str, ...],
    prepared_status: PreparedStatus,
    resume_intent: _ResumeLandIntent | None,
    stale_intents: list[LoadedIntent],
) -> None:
    """Print resumable land intent diagnostics for live execution."""

    for loaded in stale_intents:
        if not isinstance(loaded.intent, LandIntent):
            continue
        if resume_intent is not None and loaded.path == resume_intent.path:
            if resume_intent.mode == "tail-after-landed-prefix":
                console.note(
                    t"Resuming interrupted {describe_intent(loaded.intent)} after the "
                    t"trunk transition already succeeded"
                )
            else:
                console.note(t"Resuming interrupted {describe_intent(loaded.intent)}")
            continue
        match = match_ordered_change_ids(
            loaded.intent.ordered_change_ids,
            tuple(
                prepared_revision.revision.change_id
                for prepared_revision in prepared_status.prepared.status_revisions
            ),
        )
        if match == "overlap":
            console.warning(
                t"this land overlaps an incomplete earlier operation "
                t"({describe_intent(loaded.intent)})"
            )
        else:
            console.note(t"incomplete operation outstanding: {describe_intent(loaded.intent)}")


def _build_land_plan(
    *,
    bypass_readiness: bool,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
    trunk_branch: str,
) -> _LandPlan:
    path_revisions = _resolve_land_path_revisions(
        prepared_status=prepared_status,
        status_result=status_result,
    )
    landed_revisions, boundary_action = _collect_landable_prefix(
        bypass_readiness=bypass_readiness,
        path_revisions=path_revisions,
    )

    if not landed_revisions and boundary_action is None:
        boundary_action = LandAction(
            kind="boundary",
            body="No changes on the selected stack are ready to land.",
            status="blocked",
        )
    return _LandPlan(
        blocked=not landed_revisions,
        boundary_action=boundary_action,
        landed_revisions=tuple(landed_revisions),
        push_trunk=True,
        trunk_branch=trunk_branch,
    )


def _resolve_land_path_revisions(
    *,
    prepared_status: PreparedStatus,
    status_result: StatusResult,
) -> tuple[tuple[PreparedRevision, ReviewStatusRevision], ...]:
    revisions_by_change_id = {
        revision.change_id: revision for revision in status_result.revisions
    }
    path_revisions: list[tuple[PreparedRevision, ReviewStatusRevision]] = []
    for prepared_revision in prepared_status.prepared.status_revisions:
        change_id = prepared_revision.revision.change_id
        revision = revisions_by_change_id.get(change_id)
        if revision is None:
            raise AssertionError(
                f"Prepared land revision {change_id} is missing from the status result."
            )
        path_revisions.append((prepared_revision, revision))
    return tuple(path_revisions)


def _collect_landable_prefix(
    *,
    bypass_readiness: bool,
    path_revisions: tuple[tuple[PreparedRevision, ReviewStatusRevision], ...],
) -> tuple[tuple[_LandRevision, ...], LandAction | None]:
    landed_revisions: list[_LandRevision] = []
    for prepared_revision, revision in path_revisions:
        boundary_message = _land_boundary_message(
            bypass_readiness=bypass_readiness,
            prepared_revision=prepared_revision,
            revision=revision,
        )
        if boundary_message is not None:
            return tuple(landed_revisions), LandAction(
                kind="boundary",
                body=boundary_message,
                status="blocked" if not landed_revisions else "planned",
            )
        pull_request_lookup = revision.pull_request_lookup
        if pull_request_lookup is None or pull_request_lookup.pull_request is None:
            raise AssertionError("Landable revisions require resolved pull requests.")
        landed_revisions.append(
            _LandRevision(
                bookmark=revision.bookmark,
                change_id=revision.change_id,
                commit_id=prepared_revision.revision.commit_id,
                pull_request_number=pull_request_lookup.pull_request.number,
                subject=revision.subject,
            )
        )
    return tuple(landed_revisions), None


def _land_boundary_message(
    *,
    bypass_readiness: bool,
    prepared_revision: PreparedRevision,
    revision: ReviewStatusRevision,
) -> LandActionBody | None:
    if revision.link_state == "unlinked":
        return (
            t"stop before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"this change is unlinked from review tracking; run {ui.cmd('relink')} first"
        )
    if revision.local_divergent:
        return (
            t"stop before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"multiple visible revisions still share that change ID"
        )
    remote_state = revision.remote_state
    if remote_state is None or remote_state.target != prepared_revision.revision.commit_id:
        return (
            t"stop before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"the pushed branch does not match the current local commit; rerun "
            t"{ui.cmd('submit')} first"
        )
    pull_request_lookup = revision.pull_request_lookup
    if pull_request_lookup is None:
        return (
            t"stop before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"GitHub pull request state is unavailable"
        )
    if pull_request_lookup.state == "open":
        pull_request = pull_request_lookup.pull_request
        if pull_request is None:
            raise AssertionError("Open land boundary requires a pull request payload.")
        if pull_request_lookup.review_decision_error is not None:
            detail = pull_request_lookup.review_decision_error
            return (
                t"stop before {revision.subject} {ui.change_id(revision.change_id)} "
                t"because {detail}"
            )
        if pull_request.is_draft:
            if bypass_readiness:
                return None
            return (
                t"stop before {revision.subject} {ui.change_id(revision.change_id)} "
                t"because PR #{pull_request.number} is still a draft"
            )
        if pull_request_lookup.review_decision == "changes_requested":
            if bypass_readiness:
                return None
            return (
                t"stop before {revision.subject} {ui.change_id(revision.change_id)} "
                t"because PR #{pull_request.number} has changes requested"
            )
        if pull_request_lookup.review_decision != "approved":
            if bypass_readiness:
                return None
            return (
                t"stop before {revision.subject} {ui.change_id(revision.change_id)} "
                t"because PR #{pull_request.number} is not approved"
            )
        return None
    if pull_request_lookup.state == "missing":
        return (
            t"stop before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"GitHub no longer reports a pull request for its branch; run "
            t"{ui.cmd('status --fetch')} or {ui.cmd('relink')} first"
        )
    if pull_request_lookup.state == "ambiguous":
        detail = pull_request_lookup.message or "GitHub reports an ambiguous PR link"
        return (
            t"stop before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"{detail} Run {ui.cmd('status --fetch')} and repair the PR link with "
            t"{ui.cmd('relink')}."
        )
    if pull_request_lookup.state == "error":
        detail = pull_request_lookup.message or "GitHub lookup failed"
        return (
            t"stop before {revision.subject} {ui.change_id(revision.change_id)} because {detail}"
        )
    pull_request = pull_request_lookup.pull_request
    if pull_request is None:
        raise AssertionError("Closed land boundary requires a pull request payload.")
    if pull_request.state == "merged":
        return (
            t"stop before {revision.subject} {ui.change_id(revision.change_id)} because "
            t"PR #{pull_request.number} is already merged; run "
            t"{ui.cmd('cleanup --restack')} first"
        )
    return (
        t"stop before {revision.subject} {ui.change_id(revision.change_id)} because "
        t"PR #{pull_request.number} is closed without merge"
    )


def _planned_land_actions(
    *,
    plan: _LandPlan,
    bookmark_cleanup_plans: tuple[_ReviewBookmarkCleanupPlan, ...] = (),
) -> tuple[LandAction, ...]:
    if plan.blocked:
        return () if plan.boundary_action is None else (plan.boundary_action,)

    actions: list[LandAction] = []
    bookmark_cleanup_by_change_id = {
        cleanup_plan.change_id: cleanup_plan.action for cleanup_plan in bookmark_cleanup_plans
    }
    if plan.push_trunk and plan.landed_revisions:
        actions.append(
            LandAction(
                kind="trunk",
                body=t"push {ui.bookmark(plan.trunk_branch)} to "
                t"{plan.landed_revisions[-1].subject} "
                t"{ui.change_id(plan.landed_revisions[-1].change_id)}",
                status="planned",
            )
        )
        for landed_revision in plan.landed_revisions:
            actions.append(
                LandAction(
                    kind="pull request",
                    body=t"finalize PR #{landed_revision.pull_request_number} for "
                    t"{landed_revision.subject} "
                    t"{ui.change_id(landed_revision.change_id)}",
                    status="planned",
                )
            )
            cleanup_action = bookmark_cleanup_by_change_id.get(landed_revision.change_id)
            if cleanup_action is not None:
                actions.append(cleanup_action)
    if plan.boundary_action is not None:
        actions.append(plan.boundary_action)
    return tuple(actions)


def _find_resume_land_intent(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    current_landed_change_ids: tuple[str, ...],
    prepared_status: PreparedStatus,
    selected_pr_number: int | None,
    stale_intents: Sequence[LoadedIntent],
    trunk_branch: str,
) -> _ResumeLandIntent | None:
    current_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    current_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    tail_match: _ResumeLandIntent | None = None
    for loaded in stale_intents:
        if not isinstance(loaded.intent, LandIntent):
            continue
        intent = loaded.intent
        if intent.display_revset != prepared_status.selected_revset:
            continue
        if intent.bypass_readiness != bypass_readiness:
            continue
        if intent.cleanup_bookmarks != cleanup_bookmarks:
            continue
        if intent.selected_pr_number != selected_pr_number or intent.trunk_branch != trunk_branch:
            continue
        if (
            intent.ordered_change_ids == current_change_ids
            and intent.ordered_commit_ids == current_commit_ids
            and intent.landed_change_ids == current_landed_change_ids
        ):
            return _ResumeLandIntent(
                intent=intent,
                path=loaded.path,
                mode="exact-path",
            )
        prefix_length = len(intent.landed_change_ids)
        if intent.ordered_change_ids[:prefix_length] != intent.landed_change_ids:
            continue
        if (
            intent.ordered_change_ids[prefix_length:] == current_change_ids
            and intent.ordered_commit_ids[prefix_length:] == current_commit_ids
        ):
            tail_match = _ResumeLandIntent(
                intent=intent,
                path=loaded.path,
                mode="tail-after-landed-prefix",
            )
    return tail_match


def _remote_trunk_matches_commit(
    *,
    client: _BookmarkStateReader,
    remote_name: str,
    trunk_branch: str,
    commit_id: str,
) -> bool:
    bookmark_state = client.get_bookmark_state(trunk_branch)
    local_target = bookmark_state.local_target
    if local_target is not None and local_target != commit_id:
        return False
    remote_state = bookmark_state.remote_target(remote_name)
    return remote_state is not None and remote_state.target == commit_id


def _resume_land_plan(*, intent: LandIntent, trunk_branch: str) -> _LandPlan:
    completed_change_ids = set(intent.completed_change_ids)
    landed_revisions: list[_LandRevision] = []
    for change_id in intent.landed_change_ids:
        if change_id in completed_change_ids:
            continue
        try:
            landed_revisions.append(
                _LandRevision(
                    bookmark=intent.landed_bookmarks[change_id],
                    change_id=change_id,
                    commit_id=intent.landed_commit_ids[change_id],
                    pull_request_number=intent.landed_pull_request_numbers[change_id],
                    subject=intent.landed_subjects[change_id],
                )
            )
        except KeyError as error:
            raise CliError(
                t"Interrupted land intent for {intent.label} is incomplete. "
                t"Re-run {ui.cmd('land')} to refresh the plan."
            ) from error
    return _LandPlan(
        blocked=False,
        boundary_action=None,
        landed_revisions=tuple(landed_revisions),
        push_trunk=False,
        trunk_branch=trunk_branch,
    )


def _restore_local_trunk_bookmark(
    *,
    client: _BookmarkRestorer,
    original_target: str | None,
    trunk_branch: str,
) -> None:
    if original_target is None:
        client.forget_bookmarks((trunk_branch,))
        return
    client.set_bookmark(trunk_branch, original_target, allow_backwards=True)


def _plan_review_bookmark_cleanup(
    *,
    bookmark: str,
    bookmark_state: BookmarkState,
    change_id: str,
    commit_id: str,
) -> _ReviewBookmarkCleanupPlan | None:
    """Validate whether `land` can forget one landed local review bookmark."""

    if not bookmark.startswith("review/"):
        return None
    if not bookmark_state.local_targets:
        return None
    if len(bookmark_state.local_targets) > 1:
        return _ReviewBookmarkCleanupPlan(
            action=LandAction(
                kind="local bookmark",
                body=t"cannot forget {ui.bookmark(bookmark)} because it is conflicted",
                status="blocked",
            ),
            bookmark=bookmark,
            can_forget=False,
            change_id=change_id,
        )
    local_target = bookmark_state.local_target
    if local_target is None:
        return None
    if local_target != commit_id:
        return _ReviewBookmarkCleanupPlan(
            action=LandAction(
                kind="local bookmark",
                body=(
                    t"cannot forget {ui.bookmark(bookmark)} because it already points "
                    t"to a different revision"
                ),
                status="blocked",
            ),
            bookmark=bookmark,
            can_forget=False,
            change_id=change_id,
        )
    return _ReviewBookmarkCleanupPlan(
        action=LandAction(
            kind="local bookmark",
            body=t"forget {ui.bookmark(bookmark)}",
            status="planned",
        ),
        bookmark=bookmark,
        can_forget=True,
        change_id=change_id,
    )


def _plan_review_bookmark_cleanup_for_revisions(
    *,
    client: _BookmarkStateReader,
    cleanup_bookmarks: bool,
    landed_revisions: tuple[_LandRevision, ...],
) -> tuple[_ReviewBookmarkCleanupPlan, ...]:
    """Plan which landed local review bookmarks `land` should forget."""

    if not cleanup_bookmarks:
        return ()
    cleanup_plans: list[_ReviewBookmarkCleanupPlan] = []
    for landed_revision in landed_revisions:
        cleanup_plan = _plan_review_bookmark_cleanup(
            bookmark=landed_revision.bookmark,
            bookmark_state=client.get_bookmark_state(landed_revision.bookmark),
            change_id=landed_revision.change_id,
            commit_id=landed_revision.commit_id,
        )
        if cleanup_plan is not None:
            cleanup_plans.append(cleanup_plan)
    return tuple(cleanup_plans)


def _follow_up_message(
    *,
    landed_change_count: int,
    selected_revset: str,
    total_change_count: int,
) -> LandActionBody | None:
    if landed_change_count == 0 or landed_change_count >= total_change_count:
        return None
    return (
        t"Next step: remaining descendants still sit above the changes that were landed. "
        t"Run {ui.cmd('cleanup --restack')} {ui.revset(selected_revset)} and then "
        t"{ui.cmd('submit')} {ui.revset(selected_revset)}."
    )


def _ensure_trunk_branch_matches_selected_trunk(
    *,
    client: _BookmarkStateReader,
    remote_name: str,
    trunk_branch: str,
    trunk_commit_id: str,
) -> None:
    bookmark_state = client.get_bookmark_state(trunk_branch)
    if len(bookmark_state.local_targets) > 1:
        raise CliError(
            t"Local trunk bookmark {ui.bookmark(trunk_branch)} is conflicted. "
            t"Resolve it before landing."
        )
    local_target = bookmark_state.local_target
    if local_target is not None and local_target != trunk_commit_id:
        raise CliError(
            t"Local trunk bookmark {ui.bookmark(trunk_branch)} no longer matches "
            t"{ui.revset('trunk()')}. Refresh or restore the local trunk state before "
            t"retrying."
        )

    remote_state = bookmark_state.remote_target(remote_name)
    if remote_state is None:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available. Fetch and retry."
        )
    if len(remote_state.targets) > 1:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is "
            t"conflicted. Resolve it before landing."
        )
    if remote_state.target is None:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} is not "
            t"available. Fetch and retry."
        )
    if remote_state.target != trunk_commit_id:
        raise CliError(
            t"Remote trunk bookmark {ui.bookmark(f'{trunk_branch}@{remote_name}')} moved since "
            t"the selected path was resolved. Fetch, restack if needed, and retry."
        )


async def _finalize_landed_pull_request(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    landed_revision: _LandRevision,
    trunk_branch: str,
) -> GithubPullRequest:
    try:
        pull_request = await github_client.get_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=landed_revision.pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not load PR #{landed_revision.pull_request_number} during land: {error}"
        ) from error
    pull_request = _normalize_pull_request_state(pull_request)
    if pull_request.state == "open" and pull_request.base.ref != trunk_branch:
        try:
            pull_request = await github_client.update_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
                base=trunk_branch,
                body=pull_request.body or "",
                title=pull_request.title,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not retarget PR #{pull_request.number} to "
                t"{ui.bookmark(trunk_branch)}: {error}"
            ) from error
        pull_request = _normalize_pull_request_state(pull_request)
    if pull_request.state == "open":
        try:
            await github_client.close_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
            )
            pull_request = await github_client.get_pull_request(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request.number,
            )
        except GithubClientError as error:
            raise CliError(
                t"Could not close PR #{pull_request.number} after landing: {error}"
            ) from error
        pull_request = _normalize_pull_request_state(pull_request)
    if cached_change is not None and cached_change.stack_comment_id is not None:
        try:
            await github_client.delete_issue_comment(
                github_repository.owner,
                github_repository.repo,
                comment_id=cached_change.stack_comment_id,
            )
        except GithubClientError as error:
            if error.status_code != 404:
                raise CliError(
                    t"Could not delete stack summary comment "
                    t"#{cached_change.stack_comment_id}: {error}"
                ) from error
    return pull_request


def _updated_landed_change(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    commit_id: str,
    pull_request: GithubPullRequest,
) -> CachedChange:
    pr_state = pull_request.state
    if pull_request.merged_at is not None:
        pr_state = "merged"
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            last_submitted_commit_id=commit_id,
            pr_number=pull_request.number,
            pr_state=pr_state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "last_submitted_commit_id": commit_id,
            "pr_number": pull_request.number,
            "pr_review_decision": None,
            "pr_state": pr_state,
            "pr_url": pull_request.html_url,
            "stack_comment_id": None,
        }
    )


def _build_land_intent(
    *,
    bypass_readiness: bool,
    cleanup_bookmarks: bool,
    landed_revisions: tuple[_LandRevision, ...],
    prepared_status: PreparedStatus,
    selected_pr_number: int | None,
    trunk_branch: str,
) -> LandIntent:
    ordered_change_ids = tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    ordered_commit_ids = tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )
    landed_change_ids = tuple(revision.change_id for revision in landed_revisions)
    landed_commit_id = (
        landed_revisions[-1].commit_id
        if landed_revisions
        else prepared_status.prepared.stack.trunk.commit_id
    )
    return LandIntent(
        kind="land",
        pid=os.getpid(),
        label=f"land on {prepared_status.selected_revset}",
        bypass_readiness=bypass_readiness,
        cleanup_bookmarks=cleanup_bookmarks,
        display_revset=prepared_status.selected_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        landed_change_ids=landed_change_ids,
        landed_bookmarks={revision.change_id: revision.bookmark for revision in landed_revisions},
        landed_commit_ids={
            revision.change_id: revision.commit_id for revision in landed_revisions
        },
        landed_pull_request_numbers={
            revision.change_id: revision.pull_request_number for revision in landed_revisions
        },
        landed_subjects={revision.change_id: revision.subject for revision in landed_revisions},
        completed_change_ids=(),
        trunk_branch=trunk_branch,
        trunk_commit_id=prepared_status.prepared.stack.trunk.commit_id,
        landed_commit_id=landed_commit_id,
        selected_pr_number=selected_pr_number,
        started_at=datetime.now(UTC).isoformat(),
    )


def _normalize_pull_request_state(pull_request):
    if pull_request.state != "closed" or pull_request.merged_at is None:
        return pull_request
    return pull_request.model_copy(update={"state": "merged"})
