"""Typed local stack models derived from `jj` state."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class LocalCommit(BaseModel):
    """A commit with the fields needed for stack discovery."""

    model_config = ConfigDict(frozen=True)

    change_id: str
    commit_id: str
    conflict: bool = False
    current_working_copy: bool
    description: str
    divergent: bool
    empty: bool
    hidden: bool
    immutable: bool
    parents: tuple[str, ...]
    working_copy_workspaces: tuple[str, ...] = ()

    @property
    def subject(self) -> str:
        """Return the first non-empty description line for display."""

        first_line = self.description.splitlines()[0] if self.description else ""
        return first_line or "(no description set)"

    @property
    def is_working_copy(self) -> bool:
        """Whether any workspace currently uses this change as its working copy."""

        return self.current_working_copy or bool(self.working_copy_workspaces)

    def holds_unpublished_edit(self, submitted_commit_id: str) -> bool:
        """Whether this change holds work that was never submitted.

        Callers check this because acting on a wrong answer destroys local work. An immutable
        change cannot have been edited locally, and an empty change modifies no files relative
        to its parent, so removing either discards no content.
        """

        return not self.immutable and not self.empty and self.commit_id != submitted_commit_id

    def is_submittable(self) -> bool:
        """Whether the change can be submitted as part of a stack."""

        return (
            not self.hidden
            and not self.immutable
            and not self.divergent
            and not self.empty
            and len(self.parents) == 1
        )


class LocalStack(BaseModel):
    """A linear stack of submittable changes with explicit trunk and base-parent context."""

    model_config = ConfigDict(frozen=True)

    base_parent: LocalCommit
    head: LocalCommit
    changes: tuple[LocalCommit, ...]
    selected_revset: str
    trunk: LocalCommit
