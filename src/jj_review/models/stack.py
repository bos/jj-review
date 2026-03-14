"""Typed local stack models derived from `jj` state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LocalRevision(BaseModel):
    """A commit with the fields needed for stack discovery."""

    model_config = ConfigDict(frozen=True)

    change_id: str
    commit_id: str
    current_working_copy: bool
    description: str
    divergent: bool
    empty: bool
    hidden: bool
    immutable: bool
    parents: tuple[str, ...]

    @property
    def subject(self) -> str:
        """Return the first non-empty description line for display."""

        first_line = self.description.splitlines()[0] if self.description else ""
        return first_line or "(no description set)"

    def is_reviewable(self) -> bool:
        """Whether the revision should count as a review unit."""

        return (
            not self.hidden
            and not self.immutable
            and not self.divergent
            and not (self.current_working_copy and self.empty)
        )

    def only_parent_commit_id(self) -> str:
        """Return the sole parent commit ID when the revision is linear."""

        if len(self.parents) != 1:
            raise ValueError("Revision does not have exactly one parent.")
        return self.parents[0]


class LocalStack(BaseModel):
    """A linear stack of reviewable revisions from `trunk()` to a selected head."""

    model_config = ConfigDict(frozen=True)

    head: LocalRevision
    revisions: tuple[LocalRevision, ...]
    selected_revset: str
    trunk: LocalRevision
