"""Typed models for sparse local review state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class CachedChange(BaseModel):
    """Persisted review state for one logical `jj` change."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bookmark: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    stack_comment_id: int | None = None


class ReviewState(BaseModel):
    """Sparse local cache and override state."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    version: int = 1
    changes: dict[str, CachedChange] = Field(default_factory=dict, alias="change")
