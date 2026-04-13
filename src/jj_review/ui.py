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
# - `markup=False` remains the default so arbitrary user-facing text does not
#   need per-call Rich escaping.
#
# - Optional time-prefixing stays here even though most commands still use the
#   legacy `print` shim today. We are likely to need the same behavior again as
#   command output moves onto these helpers.

from __future__ import annotations

import json
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import IO, Any, Literal, Protocol

ColorMode = Literal["auto", "always", "never"]
RequestedColorMode = Literal["always", "auto", "debug", "never"]
_JJ_COLORS_TEMPLATE = r'name ++ "\0" ++ json(value) ++ "\n"'
_JJ_STYLE_ATTRIBUTES = frozenset(
    {"bg", "bold", "dim", "fg", "italic", "reverse", "underline"}
)


class ConsoleLike(Protocol):
    """Minimal console protocol used by the module-level output helpers."""

    def print(self, *objects, **kwargs) -> None: ...


@dataclass(frozen=True, slots=True)
class _SemanticStyleRule:
    labels: frozenset[str]
    style: Any


class _SemanticStyles:
    """Resolve jj color-label sets into Rich styles."""

    def __init__(self, rules: tuple[_SemanticStyleRule, ...]) -> None:
        self._rules = tuple(
            sorted(
                rules,
                key=lambda rule: (len(rule.labels), tuple(sorted(rule.labels))),
            )
        )

    def for_labels(self, labels: tuple[str, ...]) -> Any | None:
        style_cls = import_module("rich.style").Style
        normalized_labels = _normalize_semantic_labels(labels)
        if not normalized_labels:
            return None

        style = style_cls.null()
        matched = False
        for rule in self._rules:
            if rule.labels.issubset(normalized_labels):
                style += rule.style
                matched = True
        return style if matched else None


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
    repository: Path | None = None,
    stderr: IO[str] | None = None,
    stdout: IO[str] | None = None,
    time_output: bool = False,
) -> tuple[ConsoleLike, ConsoleLike, _SemanticStyles | None]:
    start = time.perf_counter() if time_output else None
    stdout_stream = sys.stdout if stdout is None else stdout
    stderr_stream = sys.stderr if stderr is None else stderr
    semantic_styles = _load_semantic_styles(repository=repository)
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
        semantic_styles,
    )


_STDOUT_CONSOLE: ConsoleLike
_STDERR_CONSOLE: ConsoleLike
_SEMANTIC_STYLES: _SemanticStyles | None
_REQUESTED_COLOR_MODE: RequestedColorMode | None = None


def rich_color_mode(color_mode: RequestedColorMode | None) -> ColorMode:
    """Map `jj`-style color modes onto Rich's supported console modes."""

    if color_mode in {"always", "debug"}:
        return "always"
    if color_mode == "never":
        return "never"
    return "auto"


@contextmanager
def configured_ui(
    *,
    color_mode: ColorMode = "auto",
    repository: Path | None = None,
    requested_color_mode: RequestedColorMode | None = None,
    stderr: IO[str] | None = None,
    stdout: IO[str] | None = None,
    time_output: bool = False,
):
    """Temporarily install shared stdout and stderr consoles."""

    global _STDOUT_CONSOLE
    global _STDERR_CONSOLE
    global _SEMANTIC_STYLES
    global _REQUESTED_COLOR_MODE
    previous_stdout = _STDOUT_CONSOLE
    previous_stderr = _STDERR_CONSOLE
    previous_semantic_styles = _SEMANTIC_STYLES
    previous_requested_color_mode = _REQUESTED_COLOR_MODE
    _STDOUT_CONSOLE, _STDERR_CONSOLE, _SEMANTIC_STYLES = _build_consoles(
        color_mode=color_mode,
        repository=repository,
        stderr=stderr,
        stdout=stdout,
        time_output=time_output,
    )
    _REQUESTED_COLOR_MODE = requested_color_mode
    try:
        yield
    finally:
        _STDOUT_CONSOLE = previous_stdout
        _STDERR_CONSOLE = previous_stderr
        _SEMANTIC_STYLES = previous_semantic_styles
        _REQUESTED_COLOR_MODE = previous_requested_color_mode


def requested_color_mode() -> RequestedColorMode | None:
    """Return the active CLI `--color` override, if one was supplied."""

    return _REQUESTED_COLOR_MODE


def semantic_style(*labels: str) -> Any | None:
    """Resolve jj semantic color labels into the active Rich style."""

    if _SEMANTIC_STYLES is None:
        return None
    return _SEMANTIC_STYLES.for_labels(labels)


def output(*objects, **kwargs) -> None:
    """Write plain user-facing output to stdout."""

    kwargs.setdefault("markup", False)
    _STDOUT_CONSOLE.print(*objects, **kwargs)


def error(*objects, **kwargs) -> None:
    """Write styled error output to stderr."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", semantic_style("error heading") or "red")
    _STDERR_CONSOLE.print(*objects, **kwargs)


def warning(*objects, **kwargs) -> None:
    """Write styled warning output to stderr."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", semantic_style("warning heading") or "yellow")
    _STDERR_CONSOLE.print(*objects, **kwargs)


def note(*objects, **kwargs) -> None:
    """Write styled note output to stdout."""

    kwargs.setdefault("markup", False)
    kwargs.setdefault("style", semantic_style("hint heading") or "cyan")
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


def _load_semantic_styles(*, repository: Path | None) -> _SemanticStyles | None:
    """Load effective jj semantic color styles for Rich-authored output."""

    cwd = (
        repository
        if repository is not None and repository.exists() and repository.is_dir()
        else Path.cwd()
    )
    try:
        completed = subprocess.run(
            [
                "jj",
                "config",
                "list",
                "--include-defaults",
                "colors",
                "-T",
                _JJ_COLORS_TEMPLATE,
            ],
            capture_output=True,
            check=False,
            cwd=cwd,
            text=True,
        )
    except (FileNotFoundError, OSError):
        return None

    if completed.returncode != 0:
        return None

    rules = _semantic_style_rules_from_config_list(completed.stdout)
    return _SemanticStyles(rules) if rules else None


def _semantic_style_rules_from_config_list(stdout: str) -> tuple[_SemanticStyleRule, ...]:
    """Parse `jj config list colors` output into Rich style rules."""

    style_cls = import_module("rich.style").Style
    grouped_style_kwargs: dict[frozenset[str], dict[str, object]] = {}
    for raw_line in stdout.splitlines():
        if not raw_line:
            continue
        try:
            raw_name, raw_value = raw_line.split("\0", maxsplit=1)
        except ValueError:
            continue
        label_name, attribute = _parse_color_config_name(raw_name)
        if label_name is None:
            continue
        label_set = _normalize_semantic_labels((label_name,))
        if not label_set:
            continue

        style_kwargs = grouped_style_kwargs.setdefault(label_set, {})
        try:
            value = json.loads(raw_value)
        except json.JSONDecodeError:
            continue
        if attribute is None:
            rich_color = _normalize_jj_color_value(value)
            if rich_color is not None:
                style_kwargs["color"] = rich_color
            continue
        if attribute == "fg":
            rich_color = _normalize_jj_color_value(value)
            if rich_color is not None:
                style_kwargs["color"] = rich_color
            continue
        if attribute == "bg":
            rich_color = _normalize_jj_color_value(value)
            if rich_color is not None:
                style_kwargs["bgcolor"] = rich_color
            continue
        if isinstance(value, bool):
            style_kwargs[attribute] = value

    rules: list[_SemanticStyleRule] = []
    for labels, style_kwargs in grouped_style_kwargs.items():
        if not style_kwargs:
            continue
        rules.append(_SemanticStyleRule(labels=labels, style=style_cls(**style_kwargs)))
    return tuple(rules)


def _parse_color_config_name(name: str) -> tuple[str | None, str | None]:
    """Extract a jj color label name and optional style attribute."""

    try:
        parsed = tomllib.loads(f"{name} = 0\n")
    except tomllib.TOMLDecodeError:
        return None, None

    colors = parsed.get("colors")
    if not isinstance(colors, dict) or len(colors) != 1:
        return None, None

    label_name, value = next(iter(colors.items()))
    if not isinstance(label_name, str):
        return None, None
    if not isinstance(value, dict):
        return label_name, None
    if len(value) != 1:
        return None, None

    attribute = next(iter(value))
    if attribute not in _JJ_STYLE_ATTRIBUTES:
        return None, None
    return label_name, attribute


def _normalize_semantic_labels(labels: tuple[str, ...]) -> frozenset[str]:
    normalized: set[str] = set()
    for label in labels:
        normalized.update(part for part in label.split() if part)
    return frozenset(normalized)


def _normalize_jj_color_value(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("ansi-color-"):
        index = value.removeprefix("ansi-color-")
        return f"color({index})" if index.isdigit() else None
    if value.startswith("bright "):
        return value.replace(" ", "_", 1)
    return value


_STDOUT_CONSOLE, _STDERR_CONSOLE, _SEMANTIC_STYLES = _build_consoles()
