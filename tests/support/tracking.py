"""Concise builders for explicit tracking-state components in tests."""

from __future__ import annotations

from jj_stack.models.tracking import PRIdentity


def make_pr_identity(
    *,
    head_ref: str = "jj-stack/example-abcdefgh",
    pr_number: int = 1,
) -> PRIdentity:
    """Build a saved PR number and head branch."""

    return PRIdentity(
        pr_number=pr_number,
        head_ref=head_ref,
    )
