from __future__ import annotations

from jj_stack.models.stack import LocalCommit


def make_change(
    *,
    commit_id: str,
    change_id: str,
    description: str,
    empty: bool = False,
    immutable: bool = False,
) -> LocalCommit:
    return LocalCommit(
        change_id=change_id,
        commit_id=commit_id,
        conflict=False,
        current_working_copy=False,
        description=description,
        divergent=False,
        empty=empty,
        hidden=False,
        immutable=immutable,
        parents=("trunk",),
        signed=False,
    )
