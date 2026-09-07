"""Shared data structures for the merge command."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from jj_stack.bootstrap import CommandContext
from jj_stack.github.resolution import GithubRepoAddress, GithubTarget
from jj_stack.identifiers import ChangeId, CommitId
from jj_stack.models.stack import LocalStack
from jj_stack.models.tracking import PRIdentity, TrackingState
from jj_stack.ui import Message


@dataclass(frozen=True, slots=True)
class MergeAction:
    """One planned, applied, or blocked merge action."""

    kind: str
    body: Message
    status: Literal["applied", "blocked", "planned"]


@dataclass(frozen=True, slots=True)
class MergeResult:
    """Rendered merge result for one selected local stack."""

    actions: tuple[MergeAction, ...]
    enqueued: bool
    trunk_branch: str
    trunk_subject: str
    final_trunk_commit_id: str | None = None

    @property
    def applied(self) -> bool:
        return any(action.status == "applied" for action in self.actions)

    @property
    def blocked(self) -> bool:
        return any(action.status == "blocked" for action in self.actions)


@dataclass(frozen=True, slots=True)
class PreparedMerge:
    """Locally prepared merge inputs before GitHub planning and execution."""

    dry_run: bool
    context: CommandContext
    merge_method: str | None
    stack: LocalStack
    state: TrackingState
    target: GithubTarget
    target_change_id: str | None


@dataclass(frozen=True, slots=True)
class MergeExecutionInputs:
    """Mutation dependencies independent of normal stack/status preparation."""

    repo: GithubRepoAddress
    selected_revset: str
    trunk_branch: str
    trunk_subject: str

    def result(
        self,
        *,
        actions: tuple[MergeAction, ...],
        enqueued: bool = False,
        final_trunk_commit_id: str | None = None,
    ) -> MergeResult:
        return MergeResult(
            actions=actions,
            enqueued=enqueued,
            final_trunk_commit_id=final_trunk_commit_id,
            trunk_branch=self.trunk_branch,
            trunk_subject=self.trunk_subject,
        )


@dataclass(frozen=True, slots=True)
class MergePrecondition:
    """A failed merge precondition and the recovery it requires."""

    reason: Message
    recovery: Literal[
        "explained", "inspect", "reconcile", "resolve", "submit", "sync", "view"
    ] = "inspect"


@dataclass(frozen=True, slots=True)
class MergeChange:
    """One selected change plus its GitHub link."""

    base_ref: str
    change_id: ChangeId
    commit_id: CommitId
    identity: PRIdentity


@dataclass(frozen=True, slots=True)
class MergePlan:
    """Resolved merge plan for the selected stack."""

    boundary_action: MergeAction | None
    planned_changes: tuple[MergeChange, ...]
    linked_changes: tuple[MergeChange, ...]
