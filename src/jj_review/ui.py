"""Shared Rich-backed CLI output helpers."""

# Design notes:
#
# - The public API is intentionally just the top-level helper functions and the
#   `configured_ui()` context manager. Command modules should not need to manage
#   console objects directly.
#
# - The module keeps stdout and stderr console setup in one place so we can
#   migrate commands from `print(...)` incrementally without spreading Rich
#   policy across the codebase.
#
# - `markup=False` is the default for all helpers because existing jj-review
#   output uses square-bracket literals like `[done]` and `[in progress]`.
#
# - Optional time-prefixing stays here even though most commands still use the
#   legacy `print` shim today. We are likely to need the same behavior again as
#   command output moves onto these helpers.

from __future__ import annotations

import sys
import time
from contextlib import contextmanager
from importlib import import_module
from typing import IO, Any, Literal, Protocol

ColorMode = Literal["auto", "always", "never"]


class ConsoleLike(Protocol):
    """Minimal console protocol used by the module-level output helpers."""

    def print(self, *objects, **kwargs) -> None: ...


class _TimestampWriter:
    """Wrap a text stream and prefix each rendered line with elapsed time."""

    def __init__(self, stream: IO[str], *, start: float) -> None:
        self._stream = stream
        self._start = start
        self._at_line_start = True

    @property
    def encoding(self) -> str | None:
        return getattr(self._stream, "encoding", None)

    def fileno(self) -> int:
        return self._stream.fileno()

    def flush(self) -> None:
        self._stream.flush()

    def isatty(self) -> bool:
        isatty = getattr(self._stream, "isatty", None)
        return bool(isatty()) if callable(isatty) else False

    def write(self, rendered: str) -> int:
        if not rendered:
            return 0

        prefixed, self._at_line_start = _prefix_rendered_output(
            rendered,
            prefix=f"[{time.perf_counter() - self._start:0.6f}] ",
            at_line_start=self._at_line_start,
        )
        self._stream.write(prefixed)
        return len(rendered)


def _console_file(
    stream: IO[str],
    *,
    time_output: bool,
    start: float | None,
) -> Any:
    if not time_output or start is None:
        return stream
    return _TimestampWriter(stream, start=start)


def _build_console(
    stream: IO[str],
    *,
    color_mode: ColorMode,
    time_output: bool,
    start: float | None,
) -> ConsoleLike:
    console_cls = import_module("rich.console").Console
    kwargs: dict[str, object] = {
        "file": _console_file(stream, time_output=time_output, start=start),
        "soft_wrap": True,
    }
    if color_mode == "always":
        kwargs["force_terminal"] = True
    elif color_mode == "never":
        kwargs["no_color"] = True
    return console_cls(**kwargs)


def _build_consoles(
    *,
    color_mode: ColorMode = "auto",
    stderr: IO[str] | None = None,
    stdout: IO[str] | None = None,
    time_output: bool = False,
) -> tuple[ConsoleLike, ConsoleLike]:
    start = time.perf_counter() if time_output else None
    stdout_stream = sys.stdout if stdout is None else stdout
    stderr_stream = sys.stderr if stderr is None else stderr
    return (
        _build_console(
            stdout_stream,
            color_mode=color_mode,
            time_output=time_output,
            start=start,
        ),
        _build_console(
            stderr_stream,
            color_mode=color_mode,
            time_output=time_output,
            start=start,
        ),
    )


_STDOUT_CONSOLE, _STDERR_CONSOLE = _build_consoles()


@contextmanager
def configured_ui(
    *,
    color_mode: ColorMode = "auto",
    stderr: IO[str] | None = None,
    stdout: IO[str] | None = None,
    time_output: bool = False,
):
    """Temporarily install shared stdout and stderr consoles."""

    global _STDOUT_CONSOLE
    global _STDERR_CONSOLE
    previous_stdout = _STDOUT_CONSOLE
    previous_stderr = _STDERR_CONSOLE
    _STDOUT_CONSOLE, _STDERR_CONSOLE = _build_consoles(
        color_mode=color_mode,
        stderr=stderr,
        stdout=stdout,
        time_output=time_output,
    )
    try:
        yield
    finally:
        _STDOUT_CONSOLE = previous_stdout
        _STDERR_CONSOLE = previous_stderr


def output(*objects, **kwargs) -> None:
    """Write plain user-facing output to stdout."""

    kwargs.setdefault("markup", False)
    _STDOUT_CONSOLE.print(*objects, **kwargs)


def error(*objects, **kwargs) -> None:
    """Write styled error output to stderr."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", "red")
    _STDERR_CONSOLE.print(*objects, **kwargs)


def warning(*objects, **kwargs) -> None:
    """Write styled warning output to stderr."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", "yellow")
    _STDERR_CONSOLE.print(*objects, **kwargs)


def note(*objects, **kwargs) -> None:
    """Write styled note output to stdout."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", "cyan")
    _STDOUT_CONSOLE.print(*objects, **kwargs)


def _prefix_rendered_output(
    rendered: str,
    *,
    prefix: str,
    at_line_start: bool,
) -> tuple[str, bool]:
    """Prefix each logical line in a rendered string."""

    if not rendered:
        return "", at_line_start

    chunks: list[str] = []
    current_at_line_start = at_line_start
    for chunk in rendered.splitlines(keepends=True):
        if current_at_line_start:
            chunks.append(prefix)
        chunks.append(chunk)
        current_at_line_start = chunk.endswith("\n")
    return "".join(chunks), current_at_line_start
