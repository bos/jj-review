"""Data models for per-operation intent files."""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field


class IntentBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1

    def change_ids(self) -> frozenset[str]:
        return frozenset()


class OperationIntent(IntentBase):
    pid: int
    label: str
    started_at: str  # ISO 8601


class OrderedChangeIdsIntent(OperationIntent):
    display_revset: str
    ordered_change_ids: tuple[str, ...]

    def change_ids(self) -> frozenset[str]:
        return frozenset(self.ordered_change_ids)


class SubmitIntent(OrderedChangeIdsIntent):
    kind: Literal["submit"]
    head_change_id: str
    bookmarks: dict[str, str]   # change_id → bookmark
    bases: dict[str, str]  # change_id → base_branch (may be empty until --abort is designed)


class CleanupApplyIntent(OperationIntent):
    kind: Literal["cleanup-apply"]


class CleanupRestackIntent(OrderedChangeIdsIntent):
    kind: Literal["cleanup-restack"]


class CloseIntent(OrderedChangeIdsIntent):
    kind: Literal["close"]
    cleanup: bool


class RelinkIntent(OperationIntent):
    kind: Literal["relink"]
    change_id: str

    def change_ids(self) -> frozenset[str]:
        return frozenset([self.change_id])


class LandIntent(OrderedChangeIdsIntent):
    kind: Literal["land"]
    bypass_readiness: bool
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
