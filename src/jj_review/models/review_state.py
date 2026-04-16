"""Typed models for saved local jj-review data."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

LinkState = Literal["active", "unlinked"]


class CachedChange(BaseModel):
    """Saved jj-review data for one logical `jj` change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bookmark: str | None = None
    last_submitted_commit_id: str | None = None
    link_state: LinkState = "active"
    pr_is_draft: bool | None = None
    pr_number: int | None = None
    pr_review_decision: str | None = None
    pr_state: str | None = None
    pr_url: str | None = None
    stack_comment_id: int | None = None

    @property
    def is_unlinked(self) -> bool:
        """Whether this change has been intentionally unlinked from review tracking."""

        return self.link_state == "unlinked"


class ReviewState(BaseModel):
    """Saved local jj-review data and user overrides."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    changes: dict[str, CachedChange] = Field(default_factory=dict)
