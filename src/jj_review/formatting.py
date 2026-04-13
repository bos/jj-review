"""Shared short change ID and output-formatting helpers."""

from __future__ import annotations

import re


def short_change_id(change_id: str) -> str:
    """Return a stable short prefix for a full change ID."""

    return change_id[:8]


def format_change_marker(change_id: str) -> str:
    """Render a short change ID marker for CLI output."""

    return f"({short_change_id(change_id)})"


def format_revision_label(subject: str, change_id: str) -> str:
    """Render a revision subject with its short change ID marker."""

    return f"{subject} {format_change_marker(change_id)}"


def format_action_line(*, status: str, kind: str, message: str) -> str:
    """Render a status-tagged action line for CLI output."""

    return f"- {status}: {kind}: {message}"


def format_status_annotation(annotation: str) -> str:
    """Render a parenthetical status annotation for CLI output."""

    return f"({annotation})"


def format_pull_request_label(
    pull_request_number: int,
    *,
    is_draft: bool,
    prefix: str = "",
) -> str:
    """Render a pull request label for CLI output."""

    label = f"PR #{pull_request_number}"
    if is_draft:
        label = f"draft {label}"
    return f"{prefix}{label}"


def render_revision_with_suffix_lines(
    *,
    client,
    color_when: str,
    revision,
    bookmark: str | None = None,
    suffix: str | None = None,
) -> tuple[str, ...]:
    """Render one revision with native `jj log` output plus an optional suffix."""

    lines = list(
        strip_revision_bookmark_from_rendered_lines(
            client.render_revision_log_lines(revision, color_when=color_when),
            bookmark=bookmark or "",
        )
    )
    if not lines:
        raise AssertionError("Expected `jj log` to render at least one line for a revision.")
    if suffix is not None:
        lines[0] = f"{lines[0]}: {suffix}"
    return tuple(lines)


def strip_revision_bookmark_from_rendered_lines(
    lines: tuple[str, ...],
    *,
    bookmark: str,
) -> tuple[str, ...]:
    """Drop the managed review bookmark token from rendered `jj log` output."""

    if not bookmark:
        return lines
    pattern = re.compile(
        r" ?(?:\x1b\[[0-9;]*m)*"
        + re.escape(bookmark)
        + r"(?:@[^ \x1b]+)?"
        + r"(?:\x1b\[[0-9;]*m)*"
    )
    return tuple(pattern.sub("", line, count=1) for line in lines)
