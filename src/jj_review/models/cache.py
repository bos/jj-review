"""Typed models for sparse local review state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LinkState = Literal["active", "detached"]


class CachedChange(BaseModel):
    """Persisted review state for one logical `jj` change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bookmark: str | None = None
    detached_at: str | None = None
    last_submitted_commit_id: str | None = None
    link_state: LinkState = "active"
    pr_is_draft: bool | None = None
    pr_number: int | None = None
    pr_review_decision: str | None = None
    pr_state: str | None = None
    pr_url: str | None = None
    stack_comment_id: int | None = None

    @property
    def is_detached(self) -> bool:
        """Whether this change has been intentionally detached from managed review."""

        return self.link_state == "detached"


class ReviewState(BaseModel):
    """Sparse local cache and override state."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    version: int = 1
    changes: dict[str, CachedChange] = Field(default_factory=dict, alias="change")
