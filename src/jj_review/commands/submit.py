"""Create or update the GitHub pull requests for the selected stack of changes.

This pushes or updates the GitHub branches for that stack, then opens or
refreshes one pull request per change from bottom to top.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from jj_review import console, ui
from jj_review.bootstrap import bootstrap_context
from jj_review.concurrency import DEFAULT_BOUNDED_CONCURRENCY, run_bounded_tasks
from jj_review.config import ChangeConfig, RepoConfig
from jj_review.errors import CliError
from jj_review.formatting import (
    format_pull_request_label,
    render_revision_lines,
    render_revision_with_suffix_lines,
    short_change_id,
)
from jj_review.github.client import GithubClient, GithubClientError, build_github_client
from jj_review.github.resolution import (
    ParsedGithubRepo,
    parse_github_repo,
    remote_bookmarks_pointing_at_commit,
    require_github_repo,
    resolve_trunk_branch,
    select_submit_remote,
)
from jj_review.github.stack_comments import STACK_COMMENT_MARKER, is_stack_summary_comment
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState
from jj_review.models.github import GithubIssueComment, GithubPullRequest
from jj_review.models.intent import LoadedIntent, SubmitIntent
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision, LocalStack
from jj_review.review.bookmarks import (
    BookmarkResolutionResult,
    BookmarkResolver,
    BookmarkSource,
    discover_bookmarks_for_revisions,
    ensure_unique_bookmarks,
)
from jj_review.review.intents import (
    retire_superseded_intents,
)
from jj_review.review.selection import (
    parse_comma_separated_flag_values,
    resolve_selected_revset,
)
from jj_review.review.submit_recovery import (
    SubmitRecoveryIdentity,
    SubmitStatusDecision,
    submit_status_decision,
)
from jj_review.state.intents import (
    check_same_kind_intent,
    scan_intents,
    write_new_intent,
)
from jj_review.state.store import ReviewStateStore
from jj_review.system import pid_is_alive
from jj_review.ui import Message

HELP = "Send a jj stack to GitHub for review"


LocalBookmarkAction = Literal["created", "moved", "unchanged"]
PullRequestAction = Literal["created", "unchanged", "updated"]
SubmitDraftMode = Literal["default", "draft", "draft_all", "publish"]
RemoteBookmarkAction = Literal["pushed", "up to date"]
PushOperation = Literal["batch", "git_update", "up_to_date"]
_GITHUB_INSPECTION_CONCURRENCY = DEFAULT_BOUNDED_CONCURRENCY
_DESCRIBE_WITH_STACK_INPUT_ENV = "JJ_REVIEW_STACK_INPUT_FILE"


@dataclass(frozen=True, slots=True)
class SubmittedRevision:
    """Remote bookmark and GitHub result for one revision in the submitted stack."""

    bookmark: str
    bookmark_source: BookmarkSource
    change_id: str
    commit_id: str
    local_action: LocalBookmarkAction
    pull_request_action: PullRequestAction
    pull_request_is_draft: bool | None
    pull_request_number: int | None
    pull_request_title: str | None
    pull_request_url: str | None
    remote_action: RemoteBookmarkAction
    subject: str


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Remote bookmark and pull request state for the selected stack."""

    client: JjClient
    dry_run: bool
    remote: GitRemote
    revisions: tuple[SubmittedRevision, ...]
    selected_change_id: str
    selected_revset: str
    selected_subject: str
    trunk_change_id: str
    trunk_branch: str
    trunk_revision: LocalRevision
    trunk_subject: str


@dataclass(frozen=True, slots=True)
class PullRequestSyncResult:
    """Result of creating, reusing, or updating one pull request."""

    action: PullRequestAction
    cached_change: CachedChange | None
    pull_request: GithubPullRequest | None


@dataclass(frozen=True, slots=True)
class GeneratedDescription:
    """Generated title/body pair for a pull request or stack summary."""

    body: str
    title: str


@dataclass(frozen=True, slots=True)
class PreparedSubmitRevision:
    """Local submit state gathered before remote and GitHub mutation."""

    bookmark: str
    bookmark_source: BookmarkSource
    change_id: str
    expected_remote_target: str | None
    local_action: LocalBookmarkAction
    push_operation: PushOperation
    remote_action: RemoteBookmarkAction
    revision: LocalRevision


@dataclass(frozen=True, slots=True)
class SubmittedPullRequestSync:
    """One completed PR sync plus its saved-data update."""

    cached_change: CachedChange | None
    submitted_revision: SubmittedRevision


@dataclass(frozen=True, slots=True)
class PendingPullRequestSync:
    """One queued PR sync task."""

    base_branch: str
    discovered_pull_request: GithubPullRequest | None
    generated_description: GeneratedDescription
    prepared_revision: PreparedSubmitRevision


@dataclass(frozen=True, slots=True)
class PendingStackCommentSync:
    """One queued stack-comment sync task."""

    cached_change: CachedChange
    change_id: str
    comment_body: str | None
    pull_request_number: int


@dataclass(frozen=True, slots=True)
class _PreparedSubmitInputs:
    """Local submit inputs prepared before GitHub mutations begin."""

    bookmark_result: BookmarkResolutionResult
    client: JjClient
    generated_pull_request_descriptions: dict[str, GeneratedDescription]
    generated_stack_description: GeneratedDescription | None
    remote: GitRemote
    stack: LocalStack
    state: ReviewState


@dataclass(frozen=True, slots=True)
class _SubmitIntentState:
    """Prepared submit intent bookkeeping for resumable runs."""

    intent: SubmitIntent
    intent_path: Path | None
    stale_intents: list[LoadedIntent]


class BookmarkStateReader(Protocol):
    """Subset of the jj client interface needed for trunk-branch fallback."""

    def list_bookmark_states(self) -> dict[str, BookmarkState]:
        """Return bookmark state keyed by bookmark name."""


class PrivateCommitFinder(Protocol):
    """Subset of the jj client interface needed for git.private-commits checks."""

    def find_private_commits(
        self,
        revisions: tuple[LocalRevision, ...],
    ) -> tuple[LocalRevision, ...]:
        """Return the revisions blocked by the repo's private-commit policy."""


class RemoteBookmarkSyncer(Protocol):
    """Subset of the jj client interface needed for remote bookmark updates."""

    def push_bookmarks(self, *, remote: str, bookmarks: tuple[str, ...]) -> None:
        """Push a batch of bookmarks to the selected remote."""

    def update_untracked_remote_bookmark(
        self,
        *,
        remote: str,
        bookmark: str,
        desired_target: str,
        expected_remote_target: str,
    ) -> None:
        """Update an existing untracked remote bookmark without importing it first."""


class InterruptedRemoteBookmarkRepairer(Protocol):
    """Subset of the jj client interface needed for stale remote bookmark repair."""

    def fetch_remote(self, *, remote: str) -> None:
        """Refresh remembered remote bookmark state for the selected remote."""

    def list_bookmark_states(
        self,
        bookmarks: tuple[str, ...] | None = None,
    ) -> dict[str, BookmarkState]:
        """Return local and remote state for the requested bookmark names."""

    def track_bookmark(self, *, remote: str, bookmark: str) -> None:
        """Track an existing remote bookmark locally."""


def submit(
    *,
    config_path: Path | None,
    debug: bool,
    describe_with: str | None,
    draft: bool,
    draft_all: bool,
    dry_run: bool,
    labels: Sequence[str] | None,
    publish: bool,
    repository: Path | None,
    reviewers: Sequence[str] | None,
    revset: str | None,
    team_reviewers: Sequence[str] | None,
) -> int:
    """CLI entrypoint for `submit`."""

    context = bootstrap_context(
        repository=repository,
        config_path=config_path,
        debug=debug,
    )
    selected_revset = resolve_selected_revset(
        command_label="submit",
        default_revset="@-",
        require_explicit=False,
        revset=revset,
    )
    label_list = parse_comma_separated_flag_values(labels)
    reviewer_list = parse_comma_separated_flag_values(reviewers)
    team_reviewer_list = parse_comma_separated_flag_values(team_reviewers)
    emitted_prepared = False

    def emit_prepared(
        selected_revset: str,
        selected_change_id: str,
        selected_subject: str,
        has_revisions: bool,
    ) -> None:
        del has_revisions, selected_revset
        nonlocal emitted_prepared
        if revset is None:
            console.output(
                _render_selected_line(
                    selected_change_id=selected_change_id,
                    selected_subject=selected_subject,
                )
            )
        emitted_prepared = True

    state_store = ReviewStateStore.for_repo(context.repo_root)
    result = asyncio.run(
        _run_submit_async(
            change_overrides=context.config.change,
            config=context.config.repo,
            describe_with=describe_with,
            draft_mode=(
                "draft_all" if draft_all else "draft" if draft else "publish" if publish else "default"
            ),
            dry_run=dry_run,
            labels=label_list,
            on_prepared=emit_prepared,
            on_trunk_resolved=lambda *_args: None,
            repo_root=context.repo_root,
            revset=selected_revset,
            reviewers=reviewer_list,
            state_store=state_store,
            team_reviewers=team_reviewer_list,
        )
    )
    if not emitted_prepared:
        if revset is None:
            console.output(
                _render_selected_line(
                    selected_change_id=result.selected_change_id,
                    selected_subject=result.selected_subject,
                )
            )
    client = getattr(result, "client", None)
    if not result.revisions:
        for line in _render_submit_trunk_lines(
            client=client,
            result=result,
        ):
            if client is None:
                console.output(line)
            else:
                console.output(line, soft_wrap=True)
        console.note(
            t"No reviewable commits between the selected revision and {ui.revset('trunk()')}.",
            soft_wrap=True,
        )
        return 0

    if result.dry_run:
        console.note("Dry run: no local, remote, or GitHub changes applied.", soft_wrap=True)
        console.output("Planned changes:")
    else:
        console.output("Submitted changes:")
    for revision in reversed(result.revisions):
        for line in _render_submit_revision_lines(
            client=client,
            revision=revision,
        ):
            if client is None:
                console.output(line)
            else:
                console.output(line, soft_wrap=True)
    for line in _render_submit_trunk_lines(
        client=client,
        result=result,
    ):
        if client is None:
            console.output(line)
        else:
            console.output(line, soft_wrap=True)
    if not result.dry_run:
        top_pull_request_url = result.revisions[-1].pull_request_url
        if top_pull_request_url is not None:
            console.output(ui.prefixed_line("Top of stack: ", top_pull_request_url))
    return 0


def _render_submit_pr_suffix(
    *,
    action: str,
    is_draft: bool | None,
    pull_request_number: int | None,
) -> str:
    if pull_request_number is None:
        if action == "created":
            return "new PR"
        if action == "updated":
            return "PR updated"
        return "PR unchanged"
    label = format_pull_request_label(
        pull_request_number,
        is_draft=bool(is_draft),
    )
    if action == "created":
        return label
    return f"{label} {action}"


def _render_selected_line(
    *,
    selected_change_id: str,
    selected_subject: str,
) -> ui.PrefixedLine:
    return ui.prefixed_line(
        "Selected: ",
        t"{selected_subject} ({ui.change_id(selected_change_id)})",
    )


def _render_submit_revision_lines(
    *,
    client: JjClient | None,
    revision,
) -> tuple[object, ...]:
    summary = _render_submit_revision_summary(revision)
    if client is None:
        return (
            ui.prefixed_line(
                "- ",
                t"{revision.subject} ({ui.change_id(revision.change_id)}): {summary}",
            ),
        )
    return render_revision_with_suffix_lines(
        client=client,
        revision=revision,
        bookmark=revision.bookmark,
        suffix=summary,
    )


def _render_submit_revision_summary(revision) -> str:
    parts: list[str] = []
    if revision.pull_request_action != "created":
        if revision.remote_action == "up to date":
            parts.append("already pushed")
        else:
            parts.append("pushed")
    parts.append(
        _render_submit_pr_suffix(
            action=revision.pull_request_action,
            is_draft=getattr(revision, "pull_request_is_draft", None),
            pull_request_number=revision.pull_request_number,
        )
    )
    return ", ".join(parts)


def _render_submit_trunk_lines(
    *,
    client: JjClient | None,
    result,
) -> tuple[object, ...]:
    if client is None:
        return (
            ui.prefixed_line(
                "Trunk: ",
                t"{result.trunk_subject} ({ui.change_id(result.trunk_change_id)}) "
                t"-> {ui.bookmark(result.trunk_branch)}",
            ),
        )
    return render_revision_lines(
        client=client,
        revision=result.trunk_revision,
    )


def _build_submit_result(
    *,
    client: JjClient,
    dry_run: bool,
    remote: GitRemote,
    revisions: tuple[SubmittedRevision, ...],
    stack: LocalStack,
    trunk_branch: str,
) -> SubmitResult:
    """Render one submit result from the shared stack context."""

    return SubmitResult(
        client=client,
        dry_run=dry_run,
        remote=remote,
        revisions=revisions,
        selected_change_id=stack.head.change_id,
        selected_revset=stack.selected_revset,
        selected_subject=stack.head.subject,
        trunk_change_id=stack.trunk.change_id,
        trunk_branch=trunk_branch,
        trunk_revision=stack.trunk,
        trunk_subject=stack.trunk.subject,
    )


def _prepare_submit_inputs(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    describe_with: str | None,
    dry_run: bool,
    on_prepared: Callable[[str, str, str, bool], None] | None,
    repo_root: Path,
    revset: str | None,
    state_store: ReviewStateStore,
) -> _PreparedSubmitInputs:
    """Load local submit state before any GitHub mutation begins."""

    client = JjClient(repo_root)
    remote = select_submit_remote(client.list_git_remotes())
    if not dry_run:
        _repair_interrupted_untracked_remote_bookmarks(
            client=client,
            remote=remote,
            state_dir=state_store.require_writable(),
        )
    stack = client.discover_review_stack(revset)
    if on_prepared is not None:
        on_prepared(
            stack.selected_revset,
            stack.head.change_id,
            stack.head.subject,
            bool(stack.revisions),
        )
    state = state_store.load()
    discovered_bookmarks = discover_bookmarks_for_revisions(
        bookmark_states=client.list_bookmark_states(),
        remote_name=remote.name,
        revisions=stack.revisions,
    )
    bookmark_result = BookmarkResolver(
        state,
        change_overrides,
        discovered_bookmarks=discovered_bookmarks,
    ).pin_revisions(stack.revisions)
    ensure_unique_bookmarks(bookmark_result.resolutions)
    _preflight_private_commits(client, stack.revisions)
    (
        generated_pull_request_descriptions,
        generated_stack_description,
    ) = _resolve_generated_descriptions(
        describe_with=describe_with,
        repo_root=repo_root,
        selected_revset=stack.selected_revset,
        revisions=stack.revisions,
    )
    return _PreparedSubmitInputs(
        bookmark_result=bookmark_result,
        client=client,
        generated_pull_request_descriptions=generated_pull_request_descriptions,
        generated_stack_description=generated_stack_description,
        remote=remote,
        stack=stack,
        state=state,
    )


def _start_submit_intent(
    *,
    bookmark_result: BookmarkResolutionResult,
    dry_run: bool,
    github_repository,
    remote_name: str,
    stack: LocalStack,
    state_store: ReviewStateStore,
) -> _SubmitIntentState:
    """Prepare submit intent state before any remote mutation begins."""

    ordered_change_ids = tuple(revision.change_id for revision in stack.revisions)
    ordered_commit_ids = tuple(revision.commit_id for revision in stack.revisions)
    intent = SubmitIntent(
        kind="submit",
        pid=os.getpid(),
        label=(
            f"submit for {short_change_id(stack.head.change_id)} (from {stack.selected_revset})"
        ),
        display_revset=stack.selected_revset,
        ordered_commit_ids=ordered_commit_ids,
        head_change_id=(
            stack.revisions[-1].change_id if stack.revisions else stack.trunk.change_id
        ),
        remote_name=remote_name,
        github_host=github_repository.host,
        github_owner=github_repository.owner,
        github_repo=github_repository.repo,
        ordered_change_ids=ordered_change_ids,
        bookmarks={
            revision.change_id: resolution.bookmark
            for revision, resolution in zip(
                stack.revisions,
                bookmark_result.resolutions,
                strict=True,
            )
        },
        started_at=datetime.now(UTC).isoformat(),
    )
    if dry_run:
        stale_intents = _list_stale_submit_intents_without_waiting(
            state_store=state_store,
            intent=intent,
        )
        _report_stale_submit_intents(
            current_intent=intent,
            ordered_change_ids=ordered_change_ids,
            ordered_commit_ids=ordered_commit_ids,
            stale_intents=stale_intents,
        )
        return _SubmitIntentState(intent=intent, intent_path=None, stale_intents=stale_intents)

    state_dir = state_store.require_writable()
    stale_intents = check_same_kind_intent(state_dir, intent)
    _report_stale_submit_intents(
        current_intent=intent,
        ordered_change_ids=ordered_change_ids,
        ordered_commit_ids=ordered_commit_ids,
        stale_intents=stale_intents,
    )
    return _SubmitIntentState(
        intent=intent,
        intent_path=write_new_intent(state_dir, intent),
        stale_intents=stale_intents,
    )


def _report_stale_submit_intents(
    *,
    current_intent: SubmitIntent,
    ordered_change_ids: tuple[str, ...],
    ordered_commit_ids: tuple[str, ...],
    stale_intents: list[LoadedIntent],
) -> None:
    """Render resumable submit intent diagnostics for the operator."""

    for loaded in stale_intents:
        if not isinstance(loaded.intent, SubmitIntent):
            continue
        decision = submit_status_decision(
            intent=loaded.intent,
            current_change_ids=ordered_change_ids,
            current_commit_ids=ordered_commit_ids,
            current_identity=SubmitRecoveryIdentity.from_intent(current_intent),
        )
        description = _render_submit_intent_description(loaded.intent)
        if decision is SubmitStatusDecision.CONTINUE:
            console.note(t"Continuing interrupted {description}", soft_wrap=True)
        elif decision is SubmitStatusDecision.CURRENT_STACK:
            console.note(
                t"Note: interrupted {description} does not match the current stack "
                t"exactly. This submit will use the current stack.",
                soft_wrap=True,
            )
        elif decision is SubmitStatusDecision.INSPECT:
            console.note(
                t"Note: interrupted {description} matches the current stack, "
                t"but its recorded submit target does not. This submit will use "
                t"the current stack.",
                soft_wrap=True,
            )
        else:
            console.note(
                t"Note: incomplete operation outstanding: {description}",
                soft_wrap=True,
            )


def _render_submit_intent_description(intent: SubmitIntent) -> ui.Message:
    return t"{ui.cmd('submit')} for {ui.change_id(intent.head_change_id)} " \
        t"(from {ui.revset(intent.display_revset)})"


def _prepare_submit_revisions(
    *,
    bookmark_result: BookmarkResolutionResult,
    client: JjClient,
    dry_run: bool,
    remote: GitRemote,
    stack: LocalStack,
) -> tuple[PreparedSubmitRevision, ...]:
    """Resolve bookmark mutations and push strategy for each stack revision."""

    prepared_revisions: list[PreparedSubmitRevision] = []
    for resolution, revision in zip(
        bookmark_result.resolutions,
        stack.revisions,
        strict=True,
    ):
        _ensure_change_is_not_unlinked(
            cached_change=bookmark_result.state.changes.get(revision.change_id),
            change_id=revision.change_id,
        )
        bookmark_state = client.get_bookmark_state(resolution.bookmark)
        local_action = _resolve_local_action(
            resolution.bookmark,
            bookmark_state.local_targets,
            revision.commit_id,
        )
        remote_state = bookmark_state.remote_target(remote.name)
        _ensure_remote_can_be_updated(
            bookmark=resolution.bookmark,
            bookmark_source=resolution.source,
            bookmark_state=bookmark_state,
            change_id=revision.change_id,
            desired_target=revision.commit_id,
            remote=remote.name,
            remote_state=remote_state,
            state=bookmark_result.state,
        )

        if local_action != "unchanged" and not dry_run:
            client.set_bookmark(resolution.bookmark, revision.commit_id)

        expected_remote_target: str | None = None
        if remote_state is not None and remote_state.target == revision.commit_id:
            push_operation: PushOperation = "up_to_date"
            remote_action: RemoteBookmarkAction = "up to date"
        elif (
            remote_state is not None
            and not remote_state.is_tracked
            and len(remote_state.targets) == 1
            and remote_state.target != revision.commit_id
        ):
            if remote_state is None:
                raise AssertionError("Checked remote bookmark state must exist.")
            expected_remote_target = remote_state.target
            if expected_remote_target is None:
                raise AssertionError("Checked remote target must be unambiguous.")
            push_operation = "git_update"
            remote_action = "pushed"
        else:
            push_operation = "batch"
            remote_action = "pushed"

        prepared_revisions.append(
            PreparedSubmitRevision(
                bookmark=resolution.bookmark,
                bookmark_source=resolution.source,
                change_id=revision.change_id,
                expected_remote_target=expected_remote_target,
                local_action=local_action,
                push_operation=push_operation,
                remote_action=remote_action,
                revision=revision,
            )
        )
    return tuple(prepared_revisions)


async def _run_submit_async(
    *,
    change_overrides: dict[str, ChangeConfig],
    config: RepoConfig,
    describe_with: str | None,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    labels: list[str] | None,
    on_prepared: Callable[[str, str, str, bool], None] | None,
    on_trunk_resolved: Callable[[str, str, str, bool], None] | None,
    repo_root: Path,
    revset: str | None,
    reviewers: list[str] | None,
    state_store: ReviewStateStore,
    team_reviewers: list[str] | None,
) -> SubmitResult:
    prepared_inputs = _prepare_submit_inputs(
        change_overrides=change_overrides,
        config=config,
        describe_with=describe_with,
        dry_run=dry_run,
        on_prepared=on_prepared,
        repo_root=repo_root,
        revset=revset,
        state_store=state_store,
    )
    client = prepared_inputs.client
    remote = prepared_inputs.remote
    stack = prepared_inputs.stack
    bookmark_result = prepared_inputs.bookmark_result
    state = prepared_inputs.state

    if not stack.revisions:
        if bookmark_result.changed and not dry_run:
            state_store.save(bookmark_result.state)
        trunk_branch = stack.trunk.subject
        remote_bookmarks = remote_bookmarks_pointing_at_commit(
            bookmark_states=client.list_bookmark_states(),
            remote_name=remote.name,
            commit_id=stack.trunk.commit_id,
        )
        if len(remote_bookmarks) == 1:
            trunk_branch = remote_bookmarks[0]
        if on_trunk_resolved is not None:
            on_trunk_resolved(
                stack.trunk.subject,
                stack.trunk.change_id,
                trunk_branch,
                False,
            )
        return _build_submit_result(
            client=client,
            dry_run=dry_run,
            remote=remote,
            revisions=(),
            stack=stack,
            trunk_branch=trunk_branch,
        )

    github_repository = require_github_repo(remote)
    resolved_labels = config.labels if labels is None else labels
    resolved_reviewers = config.reviewers if reviewers is None else reviewers
    resolved_team_reviewers = config.team_reviewers if team_reviewers is None else team_reviewers
    state_changes = dict(bookmark_result.state.changes)
    intent_state = _start_submit_intent(
        bookmark_result=bookmark_result,
        dry_run=dry_run,
        github_repository=github_repository,
        remote_name=remote.name,
        stack=stack,
        state_store=state_store,
    )

    succeeded = False
    submitted_revisions: tuple[SubmittedRevision, ...] = ()
    try:
        async with build_github_client(base_url=github_repository.api_base_url) as github_client:
            try:
                github_repository_state = await github_client.get_repository(
                    github_repository.owner,
                    github_repository.repo,
                )
            except GithubClientError as error:
                raise CliError(
                    f"Could not load GitHub repository {github_repository.full_name}: {error}"
                ) from error
            trunk_branch = resolve_trunk_branch(
                bookmark_states=client.list_bookmark_states(),
                github_repository_state=github_repository_state,
                remote_name=remote.name,
                trunk_commit_id=stack.trunk.commit_id,
            )
            discovered_pull_requests = await _discover_pull_requests_by_bookmark(
                github_client=github_client,
                github_repository=github_repository,
                bookmarks=tuple(
                    resolution.bookmark for resolution in bookmark_result.resolutions
                ),
            )
            if on_trunk_resolved is not None:
                on_trunk_resolved(
                    stack.trunk.subject,
                    stack.trunk.change_id,
                    trunk_branch,
                    True,
                )

            prepared_revisions = _prepare_submit_revisions(
                bookmark_result=bookmark_result,
                client=client,
                dry_run=dry_run,
                remote=remote,
                stack=stack,
            )

            _sync_remote_bookmarks(
                client=client,
                dry_run=dry_run,
                prepared_revisions=prepared_revisions,
                remote=remote,
            )
            with console.progress(
                description="Syncing pull requests",
                total=len(prepared_revisions),
            ) as progress:
                submitted_revisions = await _sync_pull_requests(
                    draft_mode=draft_mode,
                    dry_run=dry_run,
                    github_client=github_client,
                    github_repository=github_repository,
                    prepared_revisions=prepared_revisions,
                    discovered_pull_requests=discovered_pull_requests,
                    labels=resolved_labels,
                    on_progress=progress.advance,
                    reviewers=resolved_reviewers,
                    state=bookmark_result.state,
                    state_changes=state_changes,
                    state_store=state_store,
                    team_reviewers=resolved_team_reviewers,
                    trunk_branch=trunk_branch,
                    generated_descriptions=prepared_inputs.generated_pull_request_descriptions,
                )

            with console.progress(
                description="Syncing stack comments",
                total=sum(
                    1
                    for revision in submitted_revisions
                    if revision.pull_request_number is not None
                ),
            ) as progress:
                await _sync_stack_comments(
                    dry_run=dry_run,
                    generated_stack_description=prepared_inputs.generated_stack_description,
                    github_client=github_client,
                    github_repository=github_repository,
                    on_progress=progress.advance,
                    revisions=submitted_revisions,
                    state=bookmark_result.state,
                    state_changes=state_changes,
                    state_store=state_store,
                    trunk_branch=trunk_branch,
                )

        if not dry_run:
            next_state = bookmark_result.state.model_copy(update={"changes": state_changes})
            if bookmark_result.changed or next_state != state:
                state_store.save(next_state)

        succeeded = True
        return _build_submit_result(
            client=client,
            dry_run=dry_run,
            remote=remote,
            revisions=submitted_revisions,
            stack=stack,
            trunk_branch=trunk_branch,
        )
    finally:
        if succeeded and intent_state.intent_path is not None:
            retire_superseded_intents(intent_state.stale_intents, intent_state.intent)
            intent_state.intent_path.unlink(missing_ok=True)


def _list_stale_submit_intents_without_waiting(
    *,
    state_store: ReviewStateStore,
    intent: SubmitIntent,
) -> list[LoadedIntent]:
    return [
        loaded
        for loaded in state_store.list_intents()
        if loaded.intent.kind == intent.kind and not pid_is_alive(loaded.intent.pid)
    ]


def _repair_interrupted_untracked_remote_bookmarks(
    *,
    client: InterruptedRemoteBookmarkRepairer,
    remote: GitRemote,
    state_dir: Path,
) -> None:
    current_github_repository = parse_github_repo(remote)
    if current_github_repository is None:
        return

    stale_submit_intents: list[SubmitIntent] = []
    for loaded in scan_intents(state_dir):
        intent = loaded.intent
        if not isinstance(intent, SubmitIntent):
            continue
        if pid_is_alive(intent.pid):
            continue
        if intent.remote_name != remote.name:
            continue
        if (
            intent.github_host,
            intent.github_owner,
            intent.github_repo,
        ) != (
            current_github_repository.host,
            current_github_repository.owner,
            current_github_repository.repo,
        ):
            continue
        stale_submit_intents.append(intent)

    if not stale_submit_intents:
        return

    bookmarks = tuple(
        sorted(
            {
                bookmark
                for loaded in stale_submit_intents
                for bookmark in loaded.bookmarks.values()
            }
        )
    )
    if not bookmarks:
        return

    client.fetch_remote(remote=remote.name)
    bookmark_states = client.list_bookmark_states(bookmarks)
    for bookmark in bookmarks:
        bookmark_state = bookmark_states.get(bookmark)
        if bookmark_state is None:
            continue
        remote_state = bookmark_state.remote_target(remote.name)
        if remote_state is None or remote_state.is_tracked:
            continue
        local_target = bookmark_state.local_target
        if local_target is None or remote_state.target != local_target:
            continue
        client.track_bookmark(remote=remote.name, bookmark=bookmark)


def _resolve_local_action(
    bookmark: str,
    local_targets: tuple[str, ...],
    desired_target: str,
) -> LocalBookmarkAction:
    if len(local_targets) > 1:
        raise CliError(
            t"Bookmark {ui.bookmark(bookmark)} has {len(local_targets)} conflicting "
            t"local targets. Resolve the bookmark conflict with "
            t"{ui.cmd('jj bookmark')} before submitting."
        )
    local_target = local_targets[0] if local_targets else None
    if local_target == desired_target:
        return "unchanged"
    if local_target is None:
        return "created"
    return "moved"


def _ensure_remote_can_be_updated(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    bookmark_state: BookmarkState,
    change_id: str,
    desired_target: str,
    remote: str,
    remote_state: RemoteBookmarkState | None,
    state: ReviewState,
) -> None:
    if remote_state is None or not remote_state.targets:
        return
    if len(remote_state.targets) > 1:
        raise CliError(
            t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} is conflicted. "
            t"Resolve it with {ui.cmd('jj git fetch')} and retry."
        )
    if remote_state.target == desired_target:
        return
    if _bookmark_link_is_proven(
        bookmark=bookmark,
        bookmark_source=bookmark_source,
        bookmark_state=bookmark_state,
        change_id=change_id,
        state=state,
    ):
        return
    raise CliError(
        t"Remote bookmark {ui.bookmark(f'{bookmark}@{remote}')} already exists and "
        t"points elsewhere. Submit will not take over an existing remote branch "
        t"unless its link is already proven by local state, saved jj-review data, "
        t"or explicit relinking."
    )


def _bookmark_link_is_proven(
    *,
    bookmark: str,
    bookmark_source: BookmarkSource,
    bookmark_state: BookmarkState,
    change_id: str,
    state: ReviewState,
) -> bool:
    if bookmark_state.local_target is not None:
        return True
    if bookmark_source == "discovered":
        return True
    if bookmark_source != "saved":
        return False
    cached_change = state.changes.get(change_id)
    return (
        cached_change is not None
        and not cached_change.is_unlinked
        and cached_change.bookmark == bookmark
    )


def _preflight_private_commits(
    client: PrivateCommitFinder,
    revisions: tuple[LocalRevision, ...],
) -> None:
    private = client.find_private_commits(revisions)
    if not private:
        return
    subjects: list[Message] = []
    for index, revision in enumerate(private):
        if index:
            subjects.append(", ")
        subjects.append(t"{ui.change_id(revision.change_id)} ({revision.subject})")
    raise CliError(
        t"Stack contains commits blocked by "
        t"{ui.semantic_text('git.private-commits', 'code')}: {tuple(subjects)}. "
        t"Remove these changes from the stack before submitting."
    )


async def _discover_pull_requests_by_bookmark(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    bookmarks: tuple[str, ...],
) -> dict[str, GithubPullRequest | None]:
    if not bookmarks:
        return {}

    try:
        discovered_pull_requests = await github_client.get_pull_requests_by_head_refs(
            github_repository.owner,
            github_repository.repo,
            head_refs=bookmarks,
        )
    except GithubClientError as error:
        raise CliError(f"Could not batch pull request discovery for branches: {error}") from error

    return {
        bookmark: _select_discovered_pull_request(
            head_label=f"{github_repository.owner}:{bookmark}",
            pull_requests=discovered_pull_requests.get(bookmark, ()),
        )
        for bookmark in bookmarks
    }


def _sync_remote_bookmarks(
    *,
    client: RemoteBookmarkSyncer,
    dry_run: bool,
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    remote: GitRemote,
) -> None:
    batch_push_bookmarks = tuple(
        prepared_revision.bookmark
        for prepared_revision in prepared_revisions
        if prepared_revision.push_operation == "batch"
    )
    if batch_push_bookmarks:
        if not dry_run:
            client.push_bookmarks(
                remote=remote.name,
                bookmarks=batch_push_bookmarks,
            )

    for prepared_revision in prepared_revisions:
        if prepared_revision.push_operation != "git_update":
            continue
        if not dry_run:
            if prepared_revision.expected_remote_target is None:
                raise AssertionError("Git remote update requires an expected target.")
            client.update_untracked_remote_bookmark(
                remote=remote.name,
                bookmark=prepared_revision.bookmark,
                desired_target=prepared_revision.revision.commit_id,
                expected_remote_target=prepared_revision.expected_remote_target,
            )


def _save_submit_state_checkpoint(
    *,
    dry_run: bool,
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
) -> None:
    if dry_run:
        return
    interim_state = state.model_copy(update={"changes": dict(state_changes)})
    state_store.save(interim_state)


def _resolve_generated_descriptions(
    *,
    describe_with: str | None,
    repo_root: Path,
    revisions: tuple[LocalRevision, ...],
    selected_revset: str,
) -> tuple[dict[str, GeneratedDescription], GeneratedDescription | None]:
    if describe_with is None:
        return (
            {
                revision.change_id: GeneratedDescription(
                    body=_pull_request_body(revision.description),
                    title=revision.subject,
                )
                for revision in revisions
            },
            None,
        )

    generated_descriptions = {
        revision.change_id: _run_description_command(
            command=describe_with,
            kind="pr",
            repo_root=repo_root,
            revset=revision.change_id,
        )
        for revision in revisions
    }
    generated_stack_description = None
    if len(revisions) > 1:
        stack_input = _build_stack_description_input(
            generated_descriptions=generated_descriptions,
            repo_root=repo_root,
            revisions=revisions,
        )
        with tempfile.TemporaryDirectory(prefix="jj-review-describe-with-") as tempdir:
            stack_input_path = Path(tempdir) / "stack-input.json"
            stack_input_path.write_text(json.dumps(stack_input), encoding="utf-8")
            generated_stack_description = _run_description_command(
                command=describe_with,
                extra_env={
                    _DESCRIBE_WITH_STACK_INPUT_ENV: str(stack_input_path),
                },
                kind="stack",
                repo_root=repo_root,
                revset=selected_revset,
            )
    return generated_descriptions, generated_stack_description


def _build_stack_description_input(
    *,
    generated_descriptions: dict[str, GeneratedDescription],
    repo_root: Path,
    revisions: tuple[LocalRevision, ...],
) -> dict[str, object]:
    return {
        "revisions": [
            {
                "body": generated_descriptions[revision.change_id].body,
                "change_id": revision.change_id,
                "diffstat": _describe_with_diffstat(
                    repo_root=repo_root,
                    revset=revision.change_id,
                ),
                "title": generated_descriptions[revision.change_id].title,
            }
            for revision in revisions
        ]
    }


def _describe_with_diffstat(*, repo_root: Path, revset: str) -> str:
    try:
        completed = subprocess.run(
            ["jj", "show", "--stat", "-r", revset],
            capture_output=True,
            check=False,
            cwd=repo_root,
            text=True,
        )
    except OSError as error:
        raise CliError(
            t"Could not collect diffstat for --stack {ui.revset(revset)}: {error}"
        ) from error
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip() or "unknown jj failure"
        raise CliError(t"Could not collect diffstat for --stack {ui.revset(revset)}: {detail}")

    lines = completed.stdout.rstrip().splitlines()
    diffstat_lines: list[str] = []
    for line in reversed(lines):
        if not line.strip():
            if diffstat_lines:
                break
            continue
        diffstat_lines.append(line)
    return "\n".join(reversed(diffstat_lines))


def _run_description_command(
    *,
    command: str,
    extra_env: dict[str, str] | None = None,
    kind: Literal["pr", "stack"],
    repo_root: Path,
    revset: str,
) -> GeneratedDescription:
    try:
        completed = subprocess.run(
            [command, f"--{kind}", revset],
            capture_output=True,
            check=False,
            cwd=repo_root,
            env=(
                None
                if extra_env is None
                else {
                    **os.environ,
                    **extra_env,
                }
            ),
            text=True,
        )
    except FileNotFoundError as error:
        raise CliError(t"Describe helper {ui.cmd(command)} was not found.") from error
    except OSError as error:
        raise CliError(t"Could not run describe helper {ui.cmd(command)}: {error}") from error

    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        if not detail:
            detail = f"exit status {completed.returncode}"
        raise CliError(
            t"Describe helper {ui.cmd(command)} failed for {ui.cmd(f'--{kind}')} "
            t"{ui.revset(revset)}: {detail}"
        )

    output = completed.stdout.strip()
    if not output:
        raise CliError(
            t"Describe helper {ui.cmd(command)} produced no JSON for "
            t"{ui.cmd(f'--{kind}')} "
            t"{ui.revset(revset)}."
        )

    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise CliError(
            t"Describe helper {ui.cmd(command)} returned invalid JSON for "
            t"{ui.cmd(f'--{kind}')} "
            t"{ui.revset(revset)}: {error}"
        ) from error

    if not isinstance(payload, dict):
        raise CliError(
            t"Describe helper {ui.cmd(command)} must return a JSON object for "
            t"{ui.cmd(f'--{kind}')} "
            t"{ui.revset(revset)}."
        )

    title = payload.get("title")
    body = payload.get("body")
    if not isinstance(title, str) or not isinstance(body, str):
        raise CliError(
            t"Describe helper {ui.cmd(command)} must return string "
            t"{ui.semantic_text('title', 'hint')} and "
            t"{ui.semantic_text('body', 'hint')} fields for "
            t"{ui.cmd(f'--{kind}')} {ui.revset(revset)}."
        )

    return GeneratedDescription(body=body, title=title)


async def _sync_pull_requests(
    *,
    draft_mode: SubmitDraftMode,
    discovered_pull_requests: dict[str, GithubPullRequest | None],
    dry_run: bool,
    generated_descriptions: dict[str, GeneratedDescription],
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    prepared_revisions: tuple[PreparedSubmitRevision, ...],
    reviewers: list[str],
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
    team_reviewers: list[str],
    trunk_branch: str,
    on_progress: Callable[[], None] | None = None,
) -> tuple[SubmittedRevision, ...]:
    pending = tuple(
        PendingPullRequestSync(
            base_branch=prepared_revisions[index - 1].bookmark if index > 0 else trunk_branch,
            discovered_pull_request=discovered_pull_requests[prepared_revision.bookmark],
            generated_description=generated_descriptions[prepared_revision.change_id],
            prepared_revision=prepared_revision,
        )
        for index, prepared_revision in enumerate(prepared_revisions)
    )

    def handle_success(_index: int, submitted: SubmittedPullRequestSync) -> None:
        _record_pull_request_success(
            dry_run=dry_run,
            state=state,
            state_changes=state_changes,
            state_store=state_store,
            submitted=submitted,
        )
        if on_progress is not None:
            on_progress()

    submitted_revisions = await run_bounded_tasks(
        concurrency=_GITHUB_INSPECTION_CONCURRENCY,
        items=pending,
        run_item=lambda pending_sync: _sync_pull_request_task(
            draft_mode=draft_mode,
            dry_run=dry_run,
            github_client=github_client,
            github_repository=github_repository,
            labels=labels,
            pending_sync=pending_sync,
            reviewers=reviewers,
            state=state,
            team_reviewers=team_reviewers,
        ),
        on_success=handle_success,
    )
    return tuple(submitted.submitted_revision for submitted in submitted_revisions)


def _record_pull_request_success(
    *,
    dry_run: bool,
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
    submitted: SubmittedPullRequestSync,
) -> None:
    if submitted.cached_change is not None:
        state_changes[submitted.submitted_revision.change_id] = submitted.cached_change
    _save_submit_state_checkpoint(
        dry_run=dry_run,
        state=state,
        state_changes=state_changes,
        state_store=state_store,
    )


async def _sync_pull_request_task(
    *,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    pending_sync: PendingPullRequestSync,
    reviewers: list[str],
    state: ReviewState,
    team_reviewers: list[str],
) -> SubmittedPullRequestSync:
    prepared_revision = pending_sync.prepared_revision
    pull_request_result = await _sync_pull_request(
        base_branch=pending_sync.base_branch,
        bookmark=prepared_revision.bookmark,
        change_id=prepared_revision.change_id,
        discovered_pull_request=pending_sync.discovered_pull_request,
        draft_mode=draft_mode,
        dry_run=dry_run,
        generated_description=pending_sync.generated_description,
        github_client=github_client,
        github_repository=github_repository,
        labels=labels,
        reviewers=reviewers,
        revision=prepared_revision.revision,
        state=state,
        team_reviewers=team_reviewers,
    )
    return SubmittedPullRequestSync(
        cached_change=pull_request_result.cached_change,
        submitted_revision=SubmittedRevision(
            bookmark=prepared_revision.bookmark,
            bookmark_source=prepared_revision.bookmark_source,
            change_id=prepared_revision.change_id,
            commit_id=prepared_revision.revision.commit_id,
            local_action=prepared_revision.local_action,
            pull_request_action=pull_request_result.action,
            pull_request_is_draft=(
                pull_request_result.pull_request.is_draft
                if pull_request_result.pull_request is not None
                else None
            ),
            pull_request_number=(
                pull_request_result.pull_request.number
                if pull_request_result.pull_request is not None
                else None
            ),
            pull_request_title=(
                pull_request_result.pull_request.title
                if pull_request_result.pull_request is not None
                else None
            ),
            pull_request_url=(
                pull_request_result.pull_request.html_url
                if pull_request_result.pull_request is not None
                else None
            ),
            remote_action=prepared_revision.remote_action,
            subject=prepared_revision.revision.subject,
        ),
    )


async def _sync_pull_request(
    *,
    base_branch: str,
    bookmark: str,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
    draft_mode: SubmitDraftMode,
    dry_run: bool,
    generated_description: GeneratedDescription,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    reviewers: list[str],
    revision: LocalRevision,
    state: ReviewState,
    team_reviewers: list[str],
) -> PullRequestSyncResult:
    cached_change = state.changes.get(change_id)
    _ensure_pull_request_link_is_consistent(
        bookmark=bookmark,
        cached_change=cached_change,
        change_id=change_id,
        discovered_pull_request=discovered_pull_request,
    )

    title = generated_description.title
    body = generated_description.body
    if discovered_pull_request is None:
        pull_request = None
        if not dry_run:
            pull_request = await _create_pull_request(
                base_branch=base_branch,
                body=body,
                draft=(draft_mode in ("draft", "draft_all")),
                github_client=github_client,
                github_repository=github_repository,
                head_branch=bookmark,
                title=title,
            )
        action: PullRequestAction = "created"
    elif (
        discovered_pull_request.base.ref == base_branch
        and (discovered_pull_request.body or "") == body
        and discovered_pull_request.title == title
    ):
        pull_request = discovered_pull_request
        action = "unchanged"
    else:
        pull_request = discovered_pull_request
        if not dry_run:
            pull_request = await _update_pull_request(
                base_branch=base_branch,
                body=body,
                github_client=github_client,
                github_repository=github_repository,
                pull_request=discovered_pull_request,
                title=title,
            )
        action = "updated"

    if pull_request is not None and pull_request.state == "open":
        if draft_mode == "publish" and pull_request.is_draft:
            if not dry_run:
                pull_request = await _mark_pull_request_ready_for_review(
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request=pull_request,
                )
            action = "updated"
        elif draft_mode == "draft_all" and not pull_request.is_draft:
            if not dry_run:
                pull_request = await _convert_pull_request_to_draft(
                    github_client=github_client,
                    github_repository=github_repository,
                    pull_request=pull_request,
                )
            action = "updated"

    if (
        not dry_run
        and pull_request is not None
        and _should_sync_pull_request_metadata(
            action=action,
            cached_change=cached_change,
        )
    ):
        await _sync_pull_request_metadata(
            github_client=github_client,
            github_repository=github_repository,
            labels=labels,
            pull_request_number=pull_request.number,
            reviewers=reviewers,
            team_reviewers=team_reviewers,
        )

    next_cached_change: CachedChange | None = None
    if pull_request is not None:
        next_cached_change = _updated_cached_change(
            bookmark=bookmark,
            cached_change=cached_change,
            commit_id=revision.commit_id,
            pull_request=pull_request,
        )
    return PullRequestSyncResult(
        action=action,
        cached_change=next_cached_change,
        pull_request=pull_request,
    )


def _should_sync_pull_request_metadata(
    *,
    action: PullRequestAction,
    cached_change: CachedChange | None,
) -> bool:
    if action != "unchanged":
        return True
    if cached_change is None:
        return True
    return cached_change.pr_number is None and cached_change.pr_url is None


def _select_discovered_pull_request(
    *,
    head_label: str,
    pull_requests: tuple[GithubPullRequest, ...],
) -> GithubPullRequest | None:
    if len(pull_requests) > 1:
        raise CliError(
            t"GitHub reports multiple pull requests for head branch "
            t"{ui.bookmark(head_label)}. Inspect the PR link with "
            t"{ui.cmd('status --fetch')} and repair it with {ui.cmd('relink')} "
            t"before submitting again."
        )
    if not pull_requests:
        return None
    pull_request = pull_requests[0]
    if pull_request.state != "open":
        raise CliError(
            t"GitHub reports pull request #{pull_request.number} for head branch "
            t"{ui.bookmark(head_label)} in state {pull_request.state}. Inspect the "
            t"PR link with {ui.cmd('status --fetch')} and repair it with "
            t"{ui.cmd('relink')} before submitting again."
        )
    return pull_request


def _ensure_pull_request_link_is_consistent(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    change_id: str,
    discovered_pull_request: GithubPullRequest | None,
) -> None:
    _ensure_change_is_not_unlinked(
        cached_change=cached_change,
        change_id=change_id,
    )
    if cached_change is None or (
        cached_change.pr_number is None and cached_change.pr_url is None
    ):
        return
    if discovered_pull_request is None:
        raise CliError(
            t"Saved pull request link exists for bookmark {ui.bookmark(bookmark)}, "
            t"but GitHub no longer reports a PR for that head branch. Inspect the "
            t"PR link with {ui.cmd('status --fetch')} and repair it with "
            t"{ui.cmd('relink')} before submitting again."
        )
    if cached_change.pr_number not in (None, discovered_pull_request.number):
        raise CliError(
            t"Saved pull request #{cached_change.pr_number} does not match the PR "
            t"GitHub reports for bookmark {ui.bookmark(bookmark)} "
            t"(#{discovered_pull_request.number}). Inspect the PR link with "
            t"{ui.cmd('status --fetch')} and repair it with {ui.cmd('relink')} "
            t"before submitting again."
        )
    if cached_change.pr_url not in (None, discovered_pull_request.html_url):
        raise CliError(
            t"Saved pull request URL for bookmark {ui.bookmark(bookmark)} does not "
            t"match GitHub. Inspect the PR link with {ui.cmd('status --fetch')} and "
            t"repair it with {ui.cmd('relink')} before submitting again."
        )


def _ensure_change_is_not_unlinked(
    *,
    cached_change: CachedChange | None,
    change_id: str,
) -> None:
    if cached_change is None or not cached_change.is_unlinked:
        return
    raise CliError(
        t"Change {ui.change_id(change_id)} is unlinked from review tracking. Run "
        t"{ui.cmd('relink')} to reattach it before submitting again."
    )


async def _create_pull_request(
    *,
    base_branch: str,
    body: str,
    draft: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    head_branch: str,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.create_pull_request(
            github_repository.owner,
            github_repository.repo,
            base=base_branch,
            body=body,
            draft=draft,
            head=head_branch,
            title=title,
        )
    except GithubClientError as error:
        raise CliError(
            t"Could not create a pull request for branch {ui.bookmark(head_branch)}: "
            t"{error}"
        ) from error


async def _sync_pull_request_metadata(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    labels: list[str],
    pull_request_number: int,
    reviewers: list[str],
    team_reviewers: list[str],
) -> None:
    try:
        if reviewers or team_reviewers:
            await github_client.request_reviewers(
                github_repository.owner,
                github_repository.repo,
                pull_number=pull_request_number,
                reviewers=reviewers,
                team_reviewers=team_reviewers,
            )
        if labels:
            await github_client.add_labels(
                github_repository.owner,
                github_repository.repo,
                issue_number=pull_request_number,
                labels=labels,
            )
    except GithubClientError as error:
        raise CliError(
            f"Could not synchronize metadata for pull request "
            f"#{pull_request_number}: {error}"
        ) from error


async def _mark_pull_request_ready_for_review(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise CliError(
            f"Could not publish draft pull request #{pull_request.number} for "
            f"{github_repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.mark_pull_request_ready_for_review(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not publish draft pull request #{pull_request.number} for "
            f"{github_repository.full_name}: {error}"
        ) from error


async def _convert_pull_request_to_draft(
    *,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
) -> GithubPullRequest:
    if pull_request.node_id is None:
        raise CliError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_repository.full_name}: GitHub did not return a node ID."
        )
    try:
        return await github_client.convert_pull_request_to_draft(
            pull_request_id=pull_request.node_id,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not return pull request #{pull_request.number} to draft for "
            f"{github_repository.full_name}: {error}"
        ) from error


async def _update_pull_request(
    *,
    base_branch: str,
    body: str,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request: GithubPullRequest,
    title: str,
) -> GithubPullRequest:
    try:
        return await github_client.update_pull_request(
            github_repository.owner,
            github_repository.repo,
            pull_number=pull_request.number,
            base=base_branch,
            body=body,
            title=title,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not update pull request #{pull_request.number}: {error}"
        ) from error


async def _sync_stack_comments(
    *,
    dry_run: bool,
    generated_stack_description: GeneratedDescription | None,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    revisions: tuple[SubmittedRevision, ...],
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
    trunk_branch: str,
    on_progress: Callable[[], None] | None = None,
) -> None:
    if not revisions:
        return

    head_change_id = revisions[-1].change_id
    pending: list[PendingStackCommentSync] = []
    for revision in revisions:
        if revision.pull_request_number is None:
            continue
        cached_change = state_changes.get(revision.change_id) or state.changes.get(
            revision.change_id
        )
        if cached_change is None:
            if dry_run:
                continue
            raise AssertionError("Stack summary comments require a saved pull request link.")
        comment_body = None
        if len(revisions) > 1 and revision.change_id == head_change_id:
            comment_body = _render_stack_comment(
                current=revision,
                revisions=revisions,
                stack_description=generated_stack_description,
                trunk_branch=trunk_branch,
            )
        pending.append(
            PendingStackCommentSync(
                cached_change=cached_change,
                change_id=revision.change_id,
                comment_body=comment_body,
                pull_request_number=revision.pull_request_number,
            )
        )
    if not pending:
        return

    def handle_success(_index: int, result: tuple[str, CachedChange]) -> None:
        _record_stack_comment_success(
            dry_run=dry_run,
            result=result,
            state=state,
            state_changes=state_changes,
            state_store=state_store,
        )
        if on_progress is not None:
            on_progress()

    await run_bounded_tasks(
        concurrency=_GITHUB_INSPECTION_CONCURRENCY,
        items=tuple(pending),
        run_item=lambda pending_sync: _sync_stack_comment_task(
            dry_run=dry_run,
            github_client=github_client,
            github_repository=github_repository,
            pending_sync=pending_sync,
        ),
        on_success=handle_success,
    )


def _record_stack_comment_success(
    *,
    dry_run: bool,
    result: tuple[str, CachedChange],
    state: ReviewState,
    state_changes: dict[str, CachedChange],
    state_store: ReviewStateStore,
) -> None:
    change_id, updated_change = result
    if state_changes.get(change_id) != updated_change:
        state_changes[change_id] = updated_change
        _save_submit_state_checkpoint(
            dry_run=dry_run,
            state=state,
            state_changes=state_changes,
            state_store=state_store,
        )


async def _sync_stack_comment_task(
    *,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pending_sync: PendingStackCommentSync,
) -> tuple[str, CachedChange]:
    if pending_sync.comment_body is None:
        updated_change = await _clear_stack_comment(
            cached_change=pending_sync.cached_change,
            dry_run=dry_run,
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pending_sync.pull_request_number,
        )
    else:
        comment = await _upsert_stack_comment(
            cached_change=pending_sync.cached_change,
            comment_body=pending_sync.comment_body,
            dry_run=dry_run,
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pending_sync.pull_request_number,
        )
        updated_change = pending_sync.cached_change.model_copy(
            update={"stack_comment_id": None if comment is None else comment.id}
        )
    return pending_sync.change_id, updated_change


async def _clear_stack_comment(
    *,
    cached_change: CachedChange,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request_number: int,
) -> CachedChange:
    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not list stack summary comments for pull request "
            f"#{pull_request_number}: {error}"
        ) from error
    if cached_change.stack_comment_id is not None:
        cached_comment = next(
            (comment for comment in comments if comment.id == cached_change.stack_comment_id),
            None,
        )
        if cached_comment is not None:
            if not is_stack_summary_comment(cached_comment.body):
                raise CliError(
                    t"Saved stack summary comment #{cached_change.stack_comment_id} for "
                    t"pull request #{pull_request_number} does not belong to "
                    t"jj-review. Inspect the PR link "
                    t"with {ui.cmd('status --fetch')} or delete the saved comment ID "
                    t"before submitting again."
                )
            if not dry_run:
                await _delete_stack_comment(
                    comment_id=cached_comment.id,
                    github_client=github_client,
                    github_repository=github_repository,
                )
            return cached_change.model_copy(update={"stack_comment_id": None})

    discovered_comment = await _discover_stack_comment(comments=comments)
    if discovered_comment is None:
        if cached_change.stack_comment_id is None:
            return cached_change
        return cached_change.model_copy(update={"stack_comment_id": None})
    if not dry_run:
        await _delete_stack_comment(
            comment_id=discovered_comment.id,
            github_client=github_client,
            github_repository=github_repository,
        )
    return cached_change.model_copy(update={"stack_comment_id": None})


async def _upsert_stack_comment(
    *,
    cached_change: CachedChange,
    comment_body: str,
    dry_run: bool,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request_number: int,
) -> GithubIssueComment | None:
    try:
        comments = await github_client.list_issue_comments(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not list stack summary comments for pull request "
            f"#{pull_request_number}: {error}"
        ) from error
    if cached_change.stack_comment_id is not None:
        cached_comment = next(
            (comment for comment in comments if comment.id == cached_change.stack_comment_id),
            None,
        )
        if cached_comment is not None:
            if not is_stack_summary_comment(cached_comment.body):
                raise CliError(
                    t"Saved stack summary comment #{cached_change.stack_comment_id} for "
                    t"pull request #{pull_request_number} does not belong to "
                    t"jj-review. Inspect the PR link "
                    t"with {ui.cmd('status --fetch')} or delete the saved comment ID "
                    t"before submitting again."
                )
            if cached_comment.body == comment_body:
                return cached_comment
            if dry_run:
                return cached_comment
            return await _update_stack_comment(
                comment_body=comment_body,
                comment_id=cached_change.stack_comment_id,
                github_client=github_client,
                github_repository=github_repository,
            )

    discovered_comment = await _discover_stack_comment(
        comments=comments,
    )
    if discovered_comment is None:
        if dry_run:
            return None
        return await _create_stack_comment(
            comment_body=comment_body,
            github_client=github_client,
            github_repository=github_repository,
            pull_request_number=pull_request_number,
        )
    if discovered_comment.body == comment_body:
        return discovered_comment
    if dry_run:
        return discovered_comment
    return await _update_stack_comment(
        comment_body=comment_body,
        comment_id=discovered_comment.id,
        github_client=github_client,
        github_repository=github_repository,
    )


async def _discover_stack_comment(
    *,
    comments: tuple[GithubIssueComment, ...],
) -> GithubIssueComment | None:
    matching_comments = [
        comment for comment in comments if is_stack_summary_comment(comment.body)
    ]
    if not matching_comments:
        return None
    if len(matching_comments) > 1:
        comment_ids = ", ".join(str(comment.id) for comment in matching_comments)
        raise CliError(
            t"GitHub reports multiple jj-review stack "
            t"summary comments for the same pull request: {comment_ids}. Inspect the "
            t"PR link with {ui.cmd('status --fetch')} or delete the extra stack "
            t"summary comments before submitting again."
        )
    return matching_comments[0]


async def _create_stack_comment(
    *,
    comment_body: str,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
    pull_request_number: int,
) -> GithubIssueComment:
    try:
        return await github_client.create_issue_comment(
            github_repository.owner,
            github_repository.repo,
            issue_number=pull_request_number,
            body=comment_body,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not create a stack summary comment for pull request "
            f"#{pull_request_number}: {error}"
        ) from error


async def _update_stack_comment(
    *,
    comment_body: str,
    comment_id: int,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
) -> GithubIssueComment:
    try:
        return await github_client.update_issue_comment(
            github_repository.owner,
            github_repository.repo,
            comment_id=comment_id,
            body=comment_body,
        )
    except GithubClientError as error:
        raise CliError(
            f"Could not update stack summary comment #{comment_id}: {error}"
        ) from error


async def _delete_stack_comment(
    *,
    comment_id: int,
    github_client: GithubClient,
    github_repository: ParsedGithubRepo,
) -> None:
    try:
        await github_client.delete_issue_comment(
            github_repository.owner,
            github_repository.repo,
            comment_id=comment_id,
        )
    except GithubClientError as error:
        if error.status_code == 404:
            return
        raise CliError(
            f"Could not delete stack summary comment #{comment_id}: {error}"
        ) from error


def _render_stack_comment(
    *,
    current: SubmittedRevision,
    revisions: tuple[SubmittedRevision, ...],
    stack_description: GeneratedDescription | None,
    trunk_branch: str,
) -> str:
    lines = [STACK_COMMENT_MARKER]
    description_lines = _render_generated_stack_description(stack_description)
    if description_lines:
        lines.extend(description_lines)
        lines.extend(("", "---"))
    lines.extend(
        [
            "This pull request is part of a stack tracked by `jj-review`.",
            "",
            "Stack:",
        ]
    )
    lines.extend(_render_stack_comment_entries(current=current, revisions=revisions))
    lines.append(f"trunk `{trunk_branch}`")
    return "\n".join(lines)


def _render_generated_stack_description(
    stack_description: GeneratedDescription | None,
) -> list[str]:
    if stack_description is None:
        return []

    lines: list[str] = []
    if stack_description.title:
        lines.append(f"## {stack_description.title}")
    if stack_description.body:
        if lines:
            lines.append("")
        lines.extend(stack_description.body.splitlines())
    return lines


def _render_stack_comment_entries(
    *,
    current: SubmittedRevision,
    revisions: tuple[SubmittedRevision, ...],
) -> list[str]:
    return [
        _render_stack_comment_entry(current=current, revision=revision)
        for revision in reversed(revisions)
    ]


def _render_stack_comment_entry(
    *,
    current: SubmittedRevision,
    revision: SubmittedRevision,
) -> str:
    title = revision.pull_request_title or revision.subject
    if revision.change_id == current.change_id:
        return f"**{title}**"
    if revision.pull_request_url is None:
        return title
    return f"[{title}]({revision.pull_request_url})"


def _pull_request_body(description: str) -> str:
    lines = description.splitlines()
    if not lines:
        return ""
    body = "\n".join(lines[1:]).strip()
    if body:
        return body
    return lines[0].strip()


def _updated_cached_change(
    *,
    bookmark: str,
    cached_change: CachedChange | None,
    commit_id: str,
    pull_request: GithubPullRequest,
) -> CachedChange:
    if cached_change is None:
        return CachedChange(
            bookmark=bookmark,
            last_submitted_commit_id=commit_id,
            pr_is_draft=pull_request.is_draft,
            pr_number=pull_request.number,
            pr_state=pull_request.state,
            pr_url=pull_request.html_url,
        )
    return cached_change.model_copy(
        update={
            "bookmark": bookmark,
            "last_submitted_commit_id": commit_id,
            "pr_is_draft": pull_request.is_draft,
            "pr_number": pull_request.number,
            "pr_state": pull_request.state,
            "pr_url": pull_request.html_url,
        }
    )
