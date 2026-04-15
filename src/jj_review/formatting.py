"""Shared short change ID and output-formatting helpers."""

from __future__ import annotations

import re
import sys
from typing import IO, Literal, Protocol

from jj_review.console import RequestedColorMode, requested_color_mode


class NativeRevisionRenderClient(Protocol):
    """Subset of the jj client interface used for native revision rendering."""

    def resolve_color_when(
        self,
        *,
        cli_color: RequestedColorMode | None = None,
        stdout_is_tty: bool,
    ) -> Literal["always", "debug", "never"]: ...

    def render_revision_log_lines(
        self,
        revision,
        *,
        color_when: Literal["always", "debug", "never"],
    ) -> tuple[str, ...]: ...


def short_change_id(change_id: str) -> str:
    """Return a stable short prefix for a full change ID."""

    return change_id[:8]


def format_change_marker(change_id: str) -> str:
    """Render a short change ID marker for CLI output."""

    return f"({short_change_id(change_id)})"


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


def render_revision_lines(
    *,
    client: NativeRevisionRenderClient,
    revision,
    bookmark: str | None = None,
    stdout: IO[str] | None = None,
    suffix: str | None = None,
) -> tuple[str, ...]:
    """Render one revision using the active CLI/UI color policy."""

    stream = sys.stdout if stdout is None else stdout
    color_when = client.resolve_color_when(
        cli_color=requested_color_mode(),
        stdout_is_tty=stream.isatty(),
    )
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


def render_revision_with_suffix_lines(
    *,
    client: NativeRevisionRenderClient,
    revision,
    bookmark: str | None = None,
    suffix: str | None = None,
) -> tuple[str, ...]:
    """Render one revision with native `jj log` output plus an optional suffix."""

    return render_revision_lines(
        client=client,
        revision=revision,
        bookmark=bookmark,
        suffix=suffix,
    )


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
