"""Land the consecutive changes above `trunk()` that are ready to land now.

By default, `land` requires each pull request in that prefix to be open, not
draft, approved, and free of outstanding changes requested. Use
`--bypass-readiness` to ignore those readiness gates while still enforcing the
normal safety checks.

By default, this command performs the landing. Use `--dry-run` to inspect the
landing plan without mutating jj or GitHub state.

If later changes remain above that point, run `cleanup --restack` and then
`submit` to keep those remaining changes under review.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from jj_review.bootstrap import bootstrap_context
from jj_review.command_ui import resolve_selected_revset
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.github.client import GithubClient, GithubClientError
from jj_review.github_resolution import (
    ResolvedGithubRepository,
    _build_github_client,
    resolve_trunk_branch,
)
from jj_review.intent import (
    check_same_kind_intent,
    delete_intent,
    match_ordered_change_ids,
    replace_intent,
    retire_superseded_intents,
    write_intent,
)
from jj_review.models.bookmarks import BookmarkState
from jj_review.models.cache import CachedChange
from jj_review.models.github import GithubPullRequest
from jj_review.models.intent import LandIntent, LoadedIntent
from jj_review.pull_request_references import (
    parse_pull_request_number,
    parse_pull_request_url,
)
from jj_review.review_inspection import (
    PreparedStatus,
    ReviewStatusRevision,
    StatusResult,
    _PreparedRevision,
    prepare_status,
    stream_status,
)

HELP = "Land the ready prefix of a stack"

LandActionStatus = Literal["applied", "blocked", "planned"]


@dataclass(frozen=True, slots=True)
class LandAction:
    """One planned, applied, or blocked landing action."""

    kind: str
    message: str
    status: LandActionStatus


@dataclass(frozen=True, slots=True)
class LandResult:
    """Rendered landing result for one selected local stack."""

    actions: tuple[LandAction, ...]
    applied: bool
    bypass_readiness: bool
    blocked: bool
    expect_pr_number: int | None
    follow_up: str | None
    github_repository: str
    remote_name: str
    selected_revset: str
    trunk_branch: str
    trunk_subject: str


@dataclass(frozen=True, slots=True)
class PreparedLand:
    """Locally prepared land inputs before GitHub planning and apply."""

    apply: bool
    bypass_readiness: bool
    config: RepoConfig
    expect_pr_number: int | None
    prepared_status: PreparedStatus
    state_dir: Path | None


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
class _ResumeLandIntent:
    """A stale land intent that still matches the current selected stack."""

    intent: LandIntent
    path: Path
    mode: Literal["exact-path", "tail-after-landed-prefix"]


@dataclass(frozen=True, slots=True)
class _LandExecutionState:
    """Resolved apply-mode land state after resume checks."""

    execution_plan: _LandPlan
    follow_up: str | None
    resume_intent: _ResumeLandIntent | None
    stale_intents: list[LoadedIntent]
    state_dir: Path


class _BookmarkStateReader(Protocol):
    """Subset of the jj client interface needed for trunk bookmark inspection."""

    def get_bookmark_state(self, bookmark: str) -> BookmarkState:
        """Return local and remote state for the named bookmark."""


class _BookmarkRestorer(Protocol):
    """Subset of the jj client interface needed for local trunk restoration."""

    def forget_bookmark(self, bookmark: str) -> None:
        """Forget a local bookmark."""

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
    expect_pr: str | None,
    repository: Path | None,
    revset: str | None,
) -> int:
    """CLI entrypoint for `land`."""

    apply = not dry_run
    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    prepared_land = prepare_land(
        apply=apply,
        bypass_readiness=bypass_readiness,
        change_overrides=context.config.change,
        config=context.config.repo,
        expect_pr_reference=expect_pr,
        repo_root=context.repo_root,
        revset=resolve_selected_revset(
            command_label="land",
            default_revset="@-",
            require_explicit=False,
            revset=revset,
        ),
    )
    result = stream_land(prepared_land=prepared_land)
    print(f"Selected revset: {result.selected_revset}")
    print(f"Selected remote: {result.remote_name}")
    print(f"GitHub: {result.github_repository}")
    print(f"Trunk: {result.trunk_subject} -> {result.trunk_branch}")
    if result.actions:
        if result.applied:
            header = "Applied land actions:"
        elif result.blocked:
            header = "Land blocked:"
        else:
            header = "Planned land actions:"
        print(header)
        for action in result.actions:
            print(f"- [{action.status}] {action.kind}: {action.message}")
    if result.follow_up is not None:
        print(result.follow_up)
    return 1 if result.blocked else 0


def _build_land_result(
    *,
    actions: tuple[LandAction, ...],
    applied: bool,
    blocked: bool,
    follow_up: str | None,
    github_repository: ResolvedGithubRepository,
    prepared_land: PreparedLand,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_subject: str,
) -> LandResult:
    """Render one land result from the shared execution context."""

    return LandResult(
        actions=actions,
        applied=applied,
        bypass_readiness=prepared_land.bypass_readiness,
        blocked=blocked,
        expect_pr_number=prepared_land.expect_pr_number,
        follow_up=follow_up,
        github_repository=github_repository.full_name,
        remote_name=remote_name,
        selected_revset=selected_revset,
        trunk_branch=trunk_branch,
        trunk_subject=trunk_subject,
    )


def prepare_land(
    *,
    apply: bool,
    bypass_readiness: bool,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    expect_pr_reference: str | None,
    repo_root: Path,
    revset: str | None,
) -> PreparedLand:
    """Resolve local landing inputs before GitHub planning and apply."""

    prepared_status = prepare_status(
        change_overrides=change_overrides,
        config=config,
        fetch_remote_state=True,
        repo_root=repo_root,
        revset=revset,
    )
    prepared = prepared_status.prepared
    if prepared.remote is None:
        message = prepared.remote_error or "Could not determine which Git remote to use."
        raise CliError(message)
    if prepared_status.github_repository is None:
        message = prepared_status.github_repository_error or "Could not resolve GitHub target."
        raise CliError(message)

    expect_pr_number = None
    if expect_pr_reference is not None:
        expect_pr_number = _parse_pull_request_reference(
            reference=expect_pr_reference,
            github_repository=prepared_status.github_repository,
        )

    state_dir = (
        prepared.state_store.require_writable()
        if apply
        else prepared.state_store.state_dir
    )
    return PreparedLand(
        apply=apply,
        bypass_readiness=bypass_readiness,
        config=config,
        expect_pr_number=expect_pr_number,
        prepared_status=prepared_status,
        state_dir=state_dir,
    )


def stream_land(*, prepared_land: PreparedLand) -> LandResult:
    """Inspect GitHub state for the prepared path and optionally apply `land`."""

    status_result = stream_status(prepared_status=prepared_land.prepared_status)
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
            "Could not inspect GitHub pull request state for `land`: "
            f"{status_result.github_error}"
        )

    github_repository = prepared_status.github_repository
    remote = prepared.remote
    if github_repository is None or remote is None:
        raise AssertionError("Prepared land requires resolved GitHub and remote targets.")

    async with _build_github_client(base_url=github_repository.api_base_url) as github_client:
        github_repository_state = await _get_github_repository(
            github_client=github_client,
            github_repository=github_repository,
        )
        trunk_branch = resolve_trunk_branch(
            client=prepared.client,
            config=prepared_land.config,
            github_repository_state=github_repository_state,
            remote=remote,
            stack=prepared.stack,
        )
        _ensure_trunk_branch_matches_selected_trunk(
            client=prepared.client,
            remote_name=remote.name,
            trunk_branch=trunk_branch,
            trunk_commit_id=prepared.stack.trunk.commit_id,
        )
        plan = _build_land_plan(
            bypass_readiness=prepared_land.bypass_readiness,
            expect_pr_number=prepared_land.expect_pr_number,
            prepared_status=prepared_status,
            status_result=status_result,
            trunk_branch=trunk_branch,
        )
        follow_up = _follow_up_message(
            landed_change_count=len(plan.landed_revisions),
            selected_revset=status_result.selected_revset,
            total_change_count=len(prepared_status.prepared.status_revisions),
        )
        if not prepared_land.apply:
            return _build_land_result(
                actions=_planned_land_actions(plan=plan),
                applied=False,
                blocked=plan.blocked,
                follow_up=None if plan.blocked else follow_up,
                github_repository=github_repository,
                prepared_land=prepared_land,
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
            return _build_land_result(
                actions=_planned_land_actions(plan=execution_plan),
                applied=False,
                blocked=True,
                follow_up=None,
                github_repository=github_repository,
                prepared_land=prepared_land,
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
                expect_pr_number=prepared_land.expect_pr_number,
                landed_revisions=execution_plan.landed_revisions,
                prepared_status=prepared_status,
                trunk_branch=trunk_branch,
            )
        )
        intent_path = (
            execution_state.resume_intent.path
            if execution_state.resume_intent is not None
            else write_intent(execution_state.state_dir, land_intent)
        )

        actions: list[LandAction] = []
        succeeded = False
        original_trunk_target = prepared.client.get_bookmark_state(trunk_branch).local_target
        try:
            if execution_plan.push_trunk:
                try:
                    prepared.client.set_bookmark(
                        trunk_branch,
                        execution_plan.landed_revisions[-1].commit_id,
                    )
                    prepared.client.push_bookmark(remote=remote.name, bookmark=trunk_branch)
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
                        message=(
                            f"push {trunk_branch} to "
                            f"{execution_plan.landed_revisions[-1].subject} "
                            f"[{_short_change_id(execution_plan.landed_revisions[-1].change_id)}]"
                        ),
                        status="applied",
                    )
                )
            for landed_revision in execution_plan.landed_revisions:
                print(
                    f"Finalizing PR #{landed_revision.pull_request_number} for "
                    f"{landed_revision.subject} "
                    f"[{_short_change_id(landed_revision.change_id)}]..."
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
                        message=(
                            f"finalize PR #{landed_revision.pull_request_number} for "
                            f"{landed_revision.subject} "
                            f"[{_short_change_id(landed_revision.change_id)}]"
                        ),
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
                land_intent = dataclass_replace(
                    land_intent,
                    completed_change_ids=tuple(
                        dict.fromkeys(
                            (*land_intent.completed_change_ids, landed_revision.change_id)
                        )
                    ),
                )
                replace_intent(intent_path, land_intent)
            succeeded = True
            return _build_land_result(
                actions=tuple(actions),
                applied=True,
                blocked=False,
                follow_up=follow_up,
                github_repository=github_repository,
                prepared_land=prepared_land,
                remote_name=remote.name,
                selected_revset=status_result.selected_revset,
                trunk_branch=trunk_branch,
                trunk_subject=status_result.trunk_subject,
            )
        finally:
            if succeeded:
                retire_superseded_intents(execution_state.stale_intents, land_intent)
                delete_intent(intent_path)
def _prepare_land_execution_state(
    *,
    follow_up: str | None,
    github_repository: ResolvedGithubRepository,
    plan: _LandPlan,
    prepared_land: PreparedLand,
    prepared_status: PreparedStatus,
    remote_name: str,
    selected_revset: str,
    trunk_branch: str,
    trunk_subject: str,
) -> _LandExecutionState:
    """Resolve resume state before apply-mode execution."""

    state_dir = prepared_land.state_dir
    if state_dir is None:
        raise AssertionError("Apply mode requires a writable state directory.")

    current_landed_change_ids = tuple(
        revision.change_id for revision in plan.landed_revisions
    )
    stale_intents = check_same_kind_intent(state_dir, _build_land_intent(
        bypass_readiness=prepared_land.bypass_readiness,
        expect_pr_number=prepared_land.expect_pr_number,
        landed_revisions=plan.landed_revisions,
        prepared_status=prepared_status,
        trunk_branch=trunk_branch,
    ))
    resume_intent = _find_resume_land_intent(
        bypass_readiness=prepared_land.bypass_readiness,
        current_landed_change_ids=current_landed_change_ids,
        expect_pr_number=prepared_land.expect_pr_number,
        prepared_status=prepared_status,
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
            delete_intent(resume_intent.path)
        raise _CompletedLandResume(
            _build_land_result(
                actions=(
                    LandAction(
                        kind="resume",
                        message="previous landing already completed; cleared stale intent",
                        status="applied",
                    ),
                ),
                applied=True,
                blocked=False,
                follow_up=follow_up,
                github_repository=github_repository,
                prepared_land=prepared_land,
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
    """Print resumable land intent diagnostics for apply-mode execution."""

    for loaded in stale_intents:
        if not isinstance(loaded.intent, LandIntent):
            continue
        if resume_intent is not None and loaded.path == resume_intent.path:
            if resume_intent.mode == "tail-after-landed-prefix":
                print(
                    f"Resuming interrupted {loaded.intent.label} after the trunk "
                    "transition already succeeded"
                )
            else:
                print(f"Resuming interrupted {loaded.intent.label}")
            continue
        match = match_ordered_change_ids(
            loaded.intent.ordered_change_ids,
            _ordered_change_ids(prepared_status),
        )
        if match == "exact" and loaded.intent.landed_change_ids == current_landed_change_ids:
            print(f"Resuming interrupted {loaded.intent.label}")
        elif match == "overlap":
            print(
                f"Warning: this land overlaps an incomplete earlier operation "
                f"({loaded.intent.label})"
            )
        else:
            print(f"Note: incomplete operation outstanding: {loaded.intent.label}")


async def _get_github_repository(
    *,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
):
    try:
        return await github_client.get_repository(
            github_repository.owner,
            github_repository.repo,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not load GitHub repository {github_repository.full_name}: {error}"
        ) from error


def _build_land_plan(
    *,
    bypass_readiness: bool,
    expect_pr_number: int | None,
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

    if expect_pr_number is not None:
        actual_pr_number = (
            landed_revisions[-1].pull_request_number if landed_revisions else None
        )
        if actual_pr_number != expect_pr_number:
            return _LandPlan(
                blocked=True,
                boundary_action=LandAction(
                    kind="guardrail",
                    message=(
                        f"`--expect-pr {expect_pr_number}` did not match the selected changes "
                        f"that can be landed now on {trunk_branch}."
                    ),
                    status="blocked",
                ),
                landed_revisions=tuple(landed_revisions),
                push_trunk=True,
                trunk_branch=trunk_branch,
            )

    if not landed_revisions and boundary_action is None:
        boundary_action = LandAction(
            kind="boundary",
            message="No changes on the selected stack are ready to land.",
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
) -> tuple[tuple[_PreparedRevision, ReviewStatusRevision], ...]:
    revisions_by_change_id = {
        revision.change_id: revision for revision in status_result.revisions
    }
    path_revisions: list[tuple[_PreparedRevision, ReviewStatusRevision]] = []
    for prepared_revision in prepared_status.prepared.status_revisions:
        change_id = prepared_revision.revision.change_id
        revision = revisions_by_change_id.get(change_id)
        if revision is None:
            raise AssertionError(
                f"Prepared land revision {change_id!r} is missing from the status result."
            )
        path_revisions.append((prepared_revision, revision))
    return tuple(path_revisions)


def _collect_landable_prefix(
    *,
    bypass_readiness: bool,
    path_revisions: tuple[tuple[_PreparedRevision, ReviewStatusRevision], ...],
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
                message=boundary_message,
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
    prepared_revision: _PreparedRevision,
    revision: ReviewStatusRevision,
) -> str | None:
    if revision.link_state == "unlinked":
        return (
            f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
            "this change is unlinked from review tracking; run `relink` first"
        )
    if revision.local_divergent:
        return (
            f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
            "multiple visible revisions still share that change ID"
        )
    remote_state = revision.remote_state
    if remote_state is None or remote_state.target != prepared_revision.revision.commit_id:
        return (
            f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
            "the pushed branch does not match the current local commit; rerun `submit` "
            "first"
        )
    pull_request_lookup = revision.pull_request_lookup
    if pull_request_lookup is None:
        return (
            f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
            "GitHub pull request state is unavailable"
        )
    if pull_request_lookup.state == "open":
        pull_request = pull_request_lookup.pull_request
        if pull_request is None:
            raise AssertionError("Open land boundary requires a pull request payload.")
        if pull_request_lookup.review_decision_error is not None:
            detail = pull_request_lookup.review_decision_error
            return (
                f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] "
                f"because {detail}"
            )
        if pull_request.is_draft:
            if bypass_readiness:
                return None
            return (
                f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] "
                f"because PR #{pull_request.number} is still a draft"
            )
        if pull_request_lookup.review_decision == "changes_requested":
            if bypass_readiness:
                return None
            return (
                f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] "
                f"because PR #{pull_request.number} has changes requested"
            )
        if pull_request_lookup.review_decision != "approved":
            if bypass_readiness:
                return None
            return (
                f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] "
                f"because PR #{pull_request.number} is not approved"
            )
        return None
    if pull_request_lookup.state == "missing":
        return (
            f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
            "GitHub no longer reports a pull request for its branch; run `status --fetch` "
            "or `relink` first"
        )
    if pull_request_lookup.state == "ambiguous":
        detail = pull_request_lookup.message or "GitHub reports an ambiguous PR link"
        return (
            f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
            f"{detail} Run `status --fetch` and repair the PR link with `relink`."
        )
    if pull_request_lookup.state == "error":
        detail = pull_request_lookup.message or "GitHub lookup failed"
        return (
            f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
            f"{detail}"
        )
    pull_request = pull_request_lookup.pull_request
    if pull_request is None:
        raise AssertionError("Closed land boundary requires a pull request payload.")
    if pull_request.state == "merged":
        return (
            f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
            f"PR #{pull_request.number} is already merged; run `cleanup --restack` first"
        )
    return (
        f"stop before {revision.subject} [{_short_change_id(revision.change_id)}] because "
        f"PR #{pull_request.number} is closed without merge"
    )


def _planned_land_actions(*, plan: _LandPlan) -> tuple[LandAction, ...]:
    if plan.blocked:
        return () if plan.boundary_action is None else (plan.boundary_action,)

    actions: list[LandAction] = []
    if plan.push_trunk and plan.landed_revisions:
        actions.append(
            LandAction(
                kind="trunk",
                message=(
                    f"push {plan.trunk_branch} to {plan.landed_revisions[-1].subject} "
                    f"[{_short_change_id(plan.landed_revisions[-1].change_id)}]"
                ),
                status="planned",
            )
        )
        for landed_revision in plan.landed_revisions:
            actions.append(
                LandAction(
                    kind="pull request",
                    message=(
                        f"finalize PR #{landed_revision.pull_request_number} for "
                        f"{landed_revision.subject} "
                        f"[{_short_change_id(landed_revision.change_id)}]"
                    ),
                    status="planned",
                )
            )
    if plan.boundary_action is not None:
        actions.append(plan.boundary_action)
    return tuple(actions)


def _ordered_change_ids(prepared_status: PreparedStatus) -> tuple[str, ...]:
    return tuple(
        prepared_revision.revision.change_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )


def _ordered_commit_ids(prepared_status: PreparedStatus) -> tuple[str, ...]:
    return tuple(
        prepared_revision.revision.commit_id
        for prepared_revision in prepared_status.prepared.status_revisions
    )


def _find_resume_land_intent(
    *,
    bypass_readiness: bool,
    current_landed_change_ids: tuple[str, ...],
    expect_pr_number: int | None,
    prepared_status: PreparedStatus,
    stale_intents: Sequence[LoadedIntent],
    trunk_branch: str,
) -> _ResumeLandIntent | None:
    current_change_ids = _ordered_change_ids(prepared_status)
    current_commit_ids = _ordered_commit_ids(prepared_status)
    tail_match: _ResumeLandIntent | None = None
    for loaded in stale_intents:
        if not isinstance(loaded.intent, LandIntent):
            continue
        intent = loaded.intent
        if intent.display_revset != prepared_status.selected_revset:
            continue
        if intent.bypass_readiness != bypass_readiness:
            continue
        if intent.expected_pr_number != expect_pr_number or intent.trunk_branch != trunk_branch:
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
                f"Interrupted land intent for {intent.label!r} is incomplete. "
                "Re-run `land` to refresh the plan."
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
        client.forget_bookmark(trunk_branch)
        return
    client.set_bookmark(trunk_branch, original_target, allow_backwards=True)


def _follow_up_message(
    *,
    landed_change_count: int,
    selected_revset: str,
    total_change_count: int,
) -> str | None:
    if landed_change_count == 0 or landed_change_count >= total_change_count:
        return None
    return (
        "Next step: remaining descendants still sit above the changes that were landed. "
        f"Run `cleanup --restack {selected_revset}` and then `submit {selected_revset}`."
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
            f"Local trunk bookmark {trunk_branch!r} is conflicted. Resolve it before landing."
        )
    local_target = bookmark_state.local_target
    if local_target is not None and local_target != trunk_commit_id:
        raise CliError(
            f"Local trunk bookmark {trunk_branch!r} no longer matches `trunk()`. Refresh or "
            "restore the local trunk state before retrying."
        )

    remote_state = bookmark_state.remote_target(remote_name)
    if remote_state is None:
        raise CliError(
            f"Remote trunk bookmark {trunk_branch!r}@{remote_name} is not available. Fetch and "
            "retry."
        )
    if len(remote_state.targets) > 1:
        raise CliError(
            f"Remote trunk bookmark {trunk_branch!r}@{remote_name} is conflicted. Resolve it "
            "before landing."
        )
    if remote_state.target is None:
        raise CliError(
            f"Remote trunk bookmark {trunk_branch!r}@{remote_name} is not available. Fetch and "
            "retry."
        )
    if remote_state.target != trunk_commit_id:
        raise CliError(
            f"Remote trunk bookmark {trunk_branch!r}@{remote_name} moved since the selected "
            "path was resolved. Fetch, restack if needed, and retry."
        )


async def _finalize_landed_pull_request(
    *,
    cached_change: CachedChange | None,
    github_client: GithubClient,
    github_repository: ResolvedGithubRepository,
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
            f"Could not load PR #{landed_revision.pull_request_number} during land: {error}"
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
                f"Could not retarget PR #{pull_request.number} to {trunk_branch!r}: {error}"
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
                f"Could not close PR #{pull_request.number} after landing: {error}"
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
                    f"Could not delete stack summary comment "
                    f"#{cached_change.stack_comment_id}: {error}"
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
    expect_pr_number: int | None,
    landed_revisions: tuple[_LandRevision, ...],
    prepared_status: PreparedStatus,
    trunk_branch: str,
) -> LandIntent:
    ordered_change_ids = _ordered_change_ids(prepared_status)
    ordered_commit_ids = _ordered_commit_ids(prepared_status)
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
        display_revset=prepared_status.selected_revset,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        landed_change_ids=landed_change_ids,
        landed_bookmarks={
            revision.change_id: revision.bookmark for revision in landed_revisions
        },
        landed_commit_ids={
            revision.change_id: revision.commit_id for revision in landed_revisions
        },
        landed_pull_request_numbers={
            revision.change_id: revision.pull_request_number for revision in landed_revisions
        },
        landed_subjects={
            revision.change_id: revision.subject for revision in landed_revisions
        },
        completed_change_ids=(),
        trunk_branch=trunk_branch,
        trunk_commit_id=prepared_status.prepared.stack.trunk.commit_id,
        landed_commit_id=landed_commit_id,
        expected_pr_number=expect_pr_number,
        started_at=datetime.now(UTC).isoformat(),
    )


def _normalize_pull_request_state(pull_request):
    if pull_request.state != "closed" or pull_request.merged_at is None:
        return pull_request
    return pull_request.model_copy(update={"state": "merged"})


def _short_change_id(change_id: str) -> str:
    return change_id[:8]


def _parse_pull_request_reference(
    *,
    reference: str,
    github_repository: ResolvedGithubRepository,
) -> int:
    parsed = parse_pull_request_number(reference)
    if parsed is not None:
        return parsed
    pull_request_url = parse_pull_request_url(reference)
    if (
        pull_request_url is None
        or pull_request_url.host != github_repository.host
        or pull_request_url.owner != github_repository.owner
        or pull_request_url.repo != github_repository.repo
    ):
        raise CliError(
            f"`--expect-pr` must be a pull request number or a URL for "
            f"{github_repository.full_name}."
        )
    return pull_request_url.number
