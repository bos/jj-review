"""Shared representations of jj-stack identifiers."""

SHORT_CHANGE_ID_LENGTH = 8


def short_change_id(change_id: str) -> str:
    """Return a stable short prefix for a full change ID."""

    return change_id[:SHORT_CHANGE_ID_LENGTH]
