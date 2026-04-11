"""Data models for per-operation intent files."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


class IntentBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1


class SubmitIntent(IntentBase):
    kind: Literal["submit"]
    pid: int
    label: str
    display_revset: str
    head_change_id: str
    ordered_change_ids: tuple[str, ...]
    bookmarks: dict[str, str]   # change_id → bookmark
    bases: dict[str, str]  # change_id → base_branch (may be empty until --abort is designed)
    started_at: str  # ISO 8601


class CleanupApplyIntent(IntentBase):
    kind: Literal["cleanup-apply"]
    pid: int
    label: str
    started_at: str


class CleanupRestackIntent(IntentBase):
    kind: Literal["cleanup-restack"]
    pid: int
    label: str
    display_revset: str
    ordered_change_ids: tuple[str, ...]
    started_at: str


class CloseIntent(IntentBase):
    kind: Literal["close"]
    pid: int
    label: str
    display_revset: str
    ordered_change_ids: tuple[str, ...]
    cleanup: bool
    started_at: str


class RelinkIntent(IntentBase):
    kind: Literal["relink"]
    pid: int
    label: str
    change_id: str
    started_at: str


class LandIntent(IntentBase):
    kind: Literal["land"]
    pid: int
    label: str
    bypass_readiness: bool
    display_revset: str
    ordered_change_ids: tuple[str, ...]
    ordered_commit_ids: tuple[str, ...]
    landed_change_ids: tuple[str, ...]
    landed_bookmarks: dict[str, str]
    landed_commit_ids: dict[str, str]
    landed_pull_request_numbers: dict[str, int]
    landed_subjects: dict[str, str]
    completed_change_ids: tuple[str, ...]
    trunk_branch: str
    trunk_commit_id: str
    landed_commit_id: str
    expected_pr_number: int | None = None
    started_at: str


IntentFile: TypeAlias = Annotated[
    SubmitIntent
    | CleanupApplyIntent
    | CleanupRestackIntent
    | CloseIntent
    | RelinkIntent
    | LandIntent,
    Field(discriminator="kind"),
]


class LoadedIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: Path
    intent: IntentFile


MatchResult = Literal["exact", "superset", "overlap", "disjoint"]
