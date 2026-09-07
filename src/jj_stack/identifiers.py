"""Shared representations of jj-stack identifiers."""

from typing import NewType

# Logical changes survive rewrites; commit IDs name immutable snapshots. Keep those
# identities distinct through observations and mutation plans without changing their wire format.
ChangeId = NewType("ChangeId", str)
CommitId = NewType("CommitId", str)

SHORT_CHANGE_ID_LENGTH = 8
SHORT_COMMIT_ID_LENGTH = 8


def short_change_id(change_id: str) -> str:
    """Return a stable short prefix for a full change ID."""

    return change_id[:SHORT_CHANGE_ID_LENGTH]


def short_commit_id(commit_id: str) -> str:
    """Return a stable short prefix for a full commit ID."""

    return commit_id[:SHORT_COMMIT_ID_LENGTH]
