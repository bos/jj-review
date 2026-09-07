"""Typed models for jj-stack tracking data."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from jj_stack.identifiers import CommitId

if TYPE_CHECKING:
    from jj_stack.models.github import GithubPR


class PRIdentity(BaseModel):
    """Pinned nominal identity for one pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_number: int
    head_ref: str

    def matches_pr(self, pr: GithubPR) -> bool:
        """Whether live GitHub data is the exact pull request saved by this identity."""

        return pr.number == self.pr_number and pr.head.ref == self.head_ref


class SubmittedBaseline(BaseModel):
    """Exact snapshot most recently acknowledged for one pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    commit_id: CommitId


class TrackedPR(BaseModel):
    """The identity and submitted baseline of one tracked pull request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    pr_identity: PRIdentity
    submitted_baseline: SubmittedBaseline

    def matches_snapshot(self, pr: GithubPR) -> bool:
        """Whether live GitHub data matches this exact saved pull request snapshot."""

        return (
            self.pr_identity.matches_pr(pr) and pr.head.sha == self.submitted_baseline.commit_id
        )


class TrackingState(BaseModel):
    """Complete pull request records keyed by their owning change IDs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[8] = 8
    prs: dict[str, TrackedPR] = Field(default_factory=dict)
