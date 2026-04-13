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
from pathlib import Path
from string.templatelib import Interpolation, Template, convert
from typing import IO, Any, Literal, Protocol, cast

from rich.console import Console, Group, NewLine
from rich.segment import Segment
from rich.style import Style
from rich.table import Table
from rich.text import Text

ColorMode = Literal["auto", "always", "never"]
RequestedColorMode = Literal["always", "auto", "debug", "never"]
_JJ_COLORS_TEMPLATE = r'name ++ "\0" ++ json(value) ++ "\n"'
_JJ_STYLE_ATTRIBUTES = frozenset(
    {"bg", "bold", "dim", "fg", "italic", "reverse", "underline"}
)
StyleArg = Style | str


class ConsoleLike(Protocol):
    """Minimal console protocol used by the module-level output helpers."""

    def print(self, *objects, **kwargs) -> None: ...


@dataclass(frozen=True, slots=True)
class _SemanticStyleRule:
    labels: frozenset[str]
    style: Style


@dataclass(frozen=True, slots=True)
class SemanticText:
    """A short semantic text fragment that should inherit jj color labels."""

    text: str
    labels: tuple[str, ...]

    def __str__(self) -> str:
        return self.text


class _SemanticStyles:
    """Resolve jj color-label sets into Rich styles."""

    def __init__(self, rules: tuple[_SemanticStyleRule, ...]) -> None:
        self._rules = tuple(
            sorted(
                rules,
                key=lambda rule: (len(rule.labels), tuple(sorted(rule.labels))),
            )
        )

    def for_labels(self, labels: tuple[str, ...]) -> Style | None:
        normalized_labels = _normalize_semantic_labels(labels)
        if not normalized_labels:
            return None

        style = Style.null()
        matched = False
        for rule in self._rules:
            if rule.labels.issubset(normalized_labels):
                style += rule.style
                matched = True
        return style if matched else None


@dataclass(slots=True)
class _TimePrefixedRenderable:
    """Render content with a Rich-managed elapsed-time prefix on every line."""

    renderable: Any
    end: str
    prefix_style: Any | None
    start: float

    def __rich_console__(self, console, options):
        prefix = f"[{time.perf_counter() - self.start:0.6f}] "
        prefix_width = len(prefix)
        inner_width = max(1, options.max_width - prefix_width)
        inner_options = options.update(width=inner_width, max_width=inner_width)
        lines = console.render_lines(self.renderable, inner_options, pad=False)
        prefix_segment = Segment(prefix, self.prefix_style)
        for index, line in enumerate(lines):
            yield prefix_segment
            yield from line
            if index < len(lines) - 1:
                yield Segment.line()
        if self.end == "\n":
            yield Segment.line()
        elif self.end:
            yield from console.render(self.end, options)


class _ConfiguredConsole:
    """Wrap a Rich console with optional jj-style elapsed-time prefixes."""

    def __init__(
        self,
        console: Any,
        *,
        prefix_style: Any | None,
        start: float | None,
        time_output: bool,
    ) -> None:
        self._console = console
        self._prefix_style = prefix_style
        self._start = start
        self._time_output = time_output

    def print(
        self,
        *objects,
        sep: str = " ",
        end: str = "\n",
        style=None,
        justify=None,
        overflow=None,
        no_wrap=None,
        emoji=None,
        markup=None,
        highlight=None,
        width=None,
        height=None,
        crop: bool = True,
        soft_wrap=None,
        new_line_start: bool = False,
    ) -> None:
        if not self._time_output or self._start is None:
            self._console.print(
                *objects,
                sep=sep,
                end=end,
                style=style,
                justify=justify,
                overflow=overflow,
                no_wrap=no_wrap,
                emoji=emoji,
                markup=markup,
                highlight=highlight,
                width=width,
                height=height,
                crop=crop,
                soft_wrap=soft_wrap,
                new_line_start=new_line_start,
            )
            return

        if not objects:
            objects = (NewLine(),)

        renderables = self._console._collect_renderables(
            objects,
            sep,
            "",
            justify=justify,
            emoji=emoji,
            markup=markup,
            highlight=highlight,
        )
        wrapped = _TimePrefixedRenderable(
            renderable=Group(*renderables),
            end=end,
            prefix_style=self._prefix_style,
            start=self._start,
        )
        self._console.print(
            wrapped,
            end="",
            style=style,
            justify=justify,
            overflow=overflow,
            no_wrap=no_wrap,
            width=width,
            height=height,
            crop=crop,
            soft_wrap=soft_wrap,
            new_line_start=new_line_start,
        )


def _build_console(
    stream: IO[str],
    *,
    color_mode: ColorMode,
    semantic_styles: _SemanticStyles | None,
    time_output: bool,
    start: float | None,
) -> ConsoleLike:
    kwargs: dict[str, object] = {
        "file": stream,
    }
    if color_mode == "always":
        kwargs["force_terminal"] = True
    elif color_mode == "never":
        kwargs["no_color"] = True
    console = Console(**cast(Any, kwargs))
    return _ConfiguredConsole(
        console,
        prefix_style=_time_output_prefix_style(semantic_styles=semantic_styles),
        start=start,
        time_output=time_output,
    )


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
            semantic_styles=semantic_styles,
            time_output=time_output,
            start=start,
        ),
        _build_console(
            stderr_stream,
            color_mode=color_mode,
            semantic_styles=semantic_styles,
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


def semantic_style(*labels: str) -> Style | None:
    """Resolve jj semantic color labels into the active Rich style."""

    if _SEMANTIC_STYLES is None:
        return None
    return _SEMANTIC_STYLES.for_labels(labels)


def semantic_text(text: str, *labels: str) -> SemanticText:
    """Wrap text with semantic labels for later Rich rendering."""

    return SemanticText(text=text, labels=labels)


def bookmark(name: str) -> SemanticText:
    """Wrap a bookmark or remote bookmark label for semantic rendering."""

    label = "remote_bookmarks" if "@" in name else "local_bookmarks"
    return semantic_text(name, label)


def change_id(name: str) -> SemanticText:
    """Wrap a change ID for semantic rendering, shortening it for display."""

    return semantic_text(name[:8], "change_id")


def revset(text: str) -> SemanticText:
    """Wrap jj revset syntax for semantic rendering."""

    return semantic_text(text, "revset")


def plain_text(content: Template | SemanticText | Any) -> str:
    """Render semantic template content into plain text."""

    parts: list[str] = []
    _append_plain_text(parts, content)
    return "".join(parts)


def rich_text(content: Template | SemanticText | Any, *, style: object | None = None) -> Text:
    """Render semantic template content into Rich `Text`."""

    rendered = Text("") if style is None else Text("", style=cast(StyleArg, style))
    _append_rich_text(rendered, content, base_style=style)
    return rendered


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


def prefixed_message(
    prefix: str,
    message: Any,
    *,
    message_style: object | None = None,
    prefix_style: object | None = None,
) -> Table:
    """Return a hanging-indent renderable with a fixed prefix column."""

    prefix_width = max(1, len(prefix))
    table = Table.grid(padding=(0, 0), expand=False)
    table.add_column(width=prefix_width, no_wrap=True)
    table.add_column()
    if isinstance(message, str | Template | SemanticText):
        message_cell: Any = rich_text(message, style=message_style)
    else:
        message_cell = message
    prefix_cell = (
        Text(prefix)
        if prefix_style is None
        else Text(prefix, style=cast(StyleArg, prefix_style))
    )
    table.add_row(
        prefix_cell,
        message_cell,
    )
    return table


def action_row(
    *,
    prefix: str,
    body: Any,
    body_style: object | None = None,
    prefix_style: object | None = None,
) -> Table:
    """Return one standard two-column action row."""

    return prefixed_message(
        prefix,
        body,
        message_style=body_style,
        prefix_style=prefix_style,
    )


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
        rules.append(_SemanticStyleRule(labels=labels, style=Style(**cast(Any, style_kwargs))))
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


def _time_output_prefix_style(*, semantic_styles: _SemanticStyles | None) -> Style | None:
    if semantic_styles is None:
        return None
    return semantic_styles.for_labels(("prefix", "timestamp"))


def _append_plain_text(parts: list[str], content: Template | SemanticText | Any) -> None:
    if isinstance(content, Template):
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            else:
                _append_plain_text(parts, _resolve_interpolation(part))
        return
    if isinstance(content, SemanticText):
        parts.append(content.text)
        return
    parts.append(str(content))


def _append_rich_text(rendered, content: Template | SemanticText | Any, *, base_style) -> None:
    if isinstance(content, Template):
        for part in content:
            if isinstance(part, str):
                rendered.append(part, style=base_style)
            else:
                _append_rich_text(
                    rendered,
                    _resolve_interpolation(part),
                    base_style=base_style,
                )
        return
    if isinstance(content, SemanticText):
        rendered.append(
            content.text,
            style=_combine_styles(base_style, semantic_style(*content.labels)),
        )
        return
    if isinstance(content, Text):
        appended = content.copy()
        if base_style is not None and appended.plain:
            appended.stylize(base_style, 0, len(appended.plain))
        rendered.append_text(appended)
        return
    rendered.append(str(content), style=base_style)


def _resolve_interpolation(interpolation: Interpolation) -> Template | SemanticText | Any:
    value = interpolation.value
    if isinstance(value, SemanticText):
        if interpolation.conversion is not None:
            converted = convert(value.text, interpolation.conversion)
            return (
                format(converted, interpolation.format_spec)
                if interpolation.format_spec
                else converted
            )
        if interpolation.format_spec:
            return SemanticText(
                text=format(value.text, interpolation.format_spec),
                labels=value.labels,
            )
        return value
    if isinstance(value, Template):
        if interpolation.conversion is not None or interpolation.format_spec:
            plain = plain_text(value)
            converted = convert(plain, interpolation.conversion)
            if interpolation.format_spec:
                return format(converted, interpolation.format_spec)
            return converted
        return value

    converted = convert(value, interpolation.conversion)
    if interpolation.format_spec:
        return format(converted, interpolation.format_spec)
    return converted


def _combine_styles(base_style: object | None, extra_style: object | None) -> object | None:
    if base_style is None:
        return extra_style
    if extra_style is None:
        return base_style
    return _to_rich_style(base_style) + _to_rich_style(extra_style)


def _to_rich_style(style: object) -> Style:
    if isinstance(style, Style):
        return style
    if isinstance(style, str):
        return Style.parse(style)
    return cast(Style, style)


_STDOUT_CONSOLE, _STDERR_CONSOLE, _SEMANTIC_STYLES = _build_consoles()
