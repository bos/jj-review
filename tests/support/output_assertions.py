"""Helpers for robust assertions against user-facing command output."""

from __future__ import annotations


def normalize_output(text: str) -> str:
    """Collapse output whitespace so assertions ignore terminal wrapping."""

    return " ".join(text.split())


def assert_output_contains(text: str, *fragments: str) -> None:
    """Assert every fragment is present after terminal-width normalization."""

    normalized = normalize_output(text)
    for fragment in fragments:
        assert fragment in normalized


def assert_output_in_order(text: str, *fragments: str) -> None:
    """Assert fragments appear in order after terminal-width normalization."""

    normalized = normalize_output(text)
    start = 0
    for fragment in fragments:
        index = normalized.index(fragment, start)
        start = index + len(fragment)
