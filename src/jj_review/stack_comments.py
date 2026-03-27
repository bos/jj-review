"""Shared helpers for jj-review stack summary comments."""

from __future__ import annotations

STACK_COMMENT_MARKER = "<!-- jj-review-stack -->"


def is_stack_summary_comment(body: str) -> bool:
    """Return whether a GitHub comment body belongs to jj-review."""

    return STACK_COMMENT_MARKER in body
