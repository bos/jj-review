"""Shared data structures for the submit command."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, Protocol

from jj_stack.identifiers import CommitId
from jj_stack.jj.client import JjClient
from jj_stack.models.git import GitRemote
from jj_stack.models.github import GithubPR
from jj_stack.models.stack import LocalCommit, LocalStack
from jj_stack.models.tracking import PRIdentity, SubmittedBaseline, TrackingState
from jj_stack.state.store import TrackingStore

PRAction = Literal["created", "unchanged", "updated"]
PRDraftAction = Literal["draft", "ready"]
SubmitDraftMode = Literal["default", "draft", "draft_all", "open"]
RemoteBranchAction = Literal["pushed", "up to date"]


@dataclass(frozen=True, slots=True)
class SubmitOptions:
    """Parsed submit options after CLI normalization."""

    base_revset: str | None
    descriptions: tuple[str, ...]
    describe_with: str | None
    draft_mode: SubmitDraftMode
    dry_run: bool
    edit: bool | Path
    labels: list[str] | None
    re_request: bool
    reviewers: list[str] | None
    revset: str | None
    team_reviewers: list[str] | None


@dataclass(frozen=True, slots=True)
class PreparedSubmitChange:
    """PR branch state gathered before remote and GitHub mutation."""

    branch: str
    expected_remote_target: CommitId | None
    change: LocalCommit
    # The saved pull request GitHub reports, or None when submit creates one.
    pr: GithubPR | None

    @property
    def remote_action(self) -> RemoteBranchAction:
        return "up to date" if self.expected_remote_target == self.change.commit_id else "pushed"


@dataclass(frozen=True, slots=True)
class SubmittedChange[PR: GithubPR | None = GithubPR | None]:
    """GitHub pull request result for one prepared change in the submitted stack."""

    prepared: PreparedSubmitChange
    pr_action: PRAction
    # A new PR has no GitHub result during a dry run.
    pr: PR

    @property
    def change_id(self) -> str:
        """The submitted change's change ID."""

        return self.prepared.change.change_id


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """Remote branch and pull request state for the selected stack."""

    client: JjClient
    dry_run: bool
    changes: tuple[SubmittedChange, ...]
    trunk: LocalCommit
    # GitHub stack changes this submit made, as phrases such as "dissolved GitHub
    # stack #7".
    github_stack_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GeneratedDescription:
    """Resolved text and the fields explicitly supplied for this submit."""

    body: str
    title: str
    explicit_fields: frozenset[Literal["body", "title"]] = frozenset()


class PRMetadataAction(NamedTuple):
    """One planned additive metadata write for a pull request."""

    labels: list[str]
    reviewers: list[str]
    team_reviewers: list[str]


@dataclass(frozen=True, slots=True)
class PRSyncPlan:
    """Complete desired state for one pull request."""

    base_branch: str
    discovered_pr: GithubPR | None
    draft: bool
    generated_description: GeneratedDescription
    metadata: PRMetadataAction | None
    prepared: PreparedSubmitChange

    @property
    def action(self) -> PRAction:
        if self.discovered_pr is None:
            return "created"
        if any(update is not None for update in self.content_updates) or self.draft_action:
            return "updated"
        return "unchanged"

    @property
    def content_updates(self) -> tuple[str | None, str | None, str | None]:
        pr = self.discovered_pr
        if pr is None:
            return None, None, None
        return (
            self.base_branch if pr.base.ref != self.base_branch else None,
            (
                self.generated_description.body
                if (pr.body or "") != self.generated_description.body
                else None
            ),
            (
                self.generated_description.title
                if pr.title != self.generated_description.title
                else None
            ),
        )

    @property
    def draft_action(self) -> PRDraftAction | None:
        pr = self.discovered_pr
        if pr is None or pr.state != "open":
            return None
        if pr.is_draft == self.draft:
            return None
        return "draft" if self.draft else "ready"


@dataclass(frozen=True, slots=True)
class PublicationInputs:
    """Local publication inputs prepared before GitHub mutations begin."""

    client: JjClient
    generated_pr_descriptions: dict[str, GeneratedDescription]
    generated_stack_description: GeneratedDescription | None
    is_maximal_path: bool
    remote: GitRemote
    stack: LocalStack
    state: TrackingState
    submitted_commits: dict[str, LocalCommit]


@dataclass(slots=True)
class SubmitMutationRun:
    """Mutable submit state shared by mutation phases."""

    state: TrackingState
    state_store: TrackingStore
    github_stack_actions: tuple[str, ...] = ()

    def record_submission(
        self,
        *,
        baseline: SubmittedBaseline,
        change_id: str,
        identity: PRIdentity,
    ) -> None:
        """Save one GitHub-acknowledged PR snapshot."""

        current = self.state.prs.get(change_id)
        if current is None:
            self.state = self.state_store.create_pr(
                change_id,
                identity=identity,
                baseline=baseline,
            )
            return
        self.state = self.state_store.relink_pr(
            change_id,
            identity=current.pr_identity,
            baseline=baseline,
        )


class PrivateCommitFinder(Protocol):
    """Subset of the jj client interface needed for git.private-commits checks."""

    def find_private_commits(
        self,
        changes: tuple[LocalCommit, ...],
    ) -> tuple[LocalCommit, ...]:
        """Return the changes blocked by the repo's private-commit policy."""
