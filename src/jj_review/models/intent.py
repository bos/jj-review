"""Data models for per-operation intent files."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class SubmitIntent:
    kind: Literal["submit"]
    pid: int
    label: str
    display_revset: str
    head_change_id: str
    ordered_change_ids: tuple[str, ...]
    bookmarks: dict[str, str]   # change_id → bookmark
    bases: dict[str, str]       # change_id → base_branch (may be empty until --abort is designed)
    started_at: str             # ISO 8601


@dataclass(frozen=True, slots=True)
class CleanupApplyIntent:
    kind: Literal["cleanup-apply"]
    pid: int
    label: str
    started_at: str


@dataclass(frozen=True, slots=True)
class CleanupRestackIntent:
    kind: Literal["cleanup-restack"]
    pid: int
    label: str
    display_revset: str
    ordered_change_ids: tuple[str, ...]
    started_at: str


@dataclass(frozen=True, slots=True)
class RelinkIntent:
    kind: Literal["relink"]
    pid: int
    label: str
    change_id: str
    started_at: str


@dataclass(frozen=True, slots=True)
class LandIntent:
    kind: Literal["land"]
    pid: int
    label: str
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
    expected_pr_number: int | None
    started_at: str


@dataclass(frozen=True, slots=True)
class LoadedIntent:
    path: Path
    intent: SubmitIntent | CleanupApplyIntent | CleanupRestackIntent | RelinkIntent | LandIntent


IntentFile = SubmitIntent | CleanupApplyIntent | CleanupRestackIntent | RelinkIntent | LandIntent
MatchResult = Literal["exact", "superset", "overlap", "disjoint"]
