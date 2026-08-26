"""Build default pull request text from a jj change description."""

from __future__ import annotations

from markdown_it import MarkdownIt

_MARKDOWN = MarkdownIt("commonmark").enable("table")


def default_pr_body(description: str, *, template: str) -> str:
    """Return the default PR body, unfolding Markdown soft line breaks."""

    lines = description.splitlines()
    if not lines:
        return template
    body = "\n".join(lines[1:]).strip()
    if body:
        return _unwrap_soft_line_breaks(body)
    if template:
        return template
    return lines[0].strip()


def _unwrap_soft_line_breaks(markdown: str) -> str:
    """Replace source wrapping with spaces while preserving Markdown structure."""

    lines = markdown.splitlines()
    replacements: list[tuple[int, int, list[str]]] = []
    for token in _MARKDOWN.parse(markdown):
        if token.type != "inline" or token.map is None or token.children is None:
            continue
        start, end = token.map
        if end - start < 2:
            continue
        content_lines = token.content.split("\n")
        breaks = [
            child.type for child in token.children if child.type in {"softbreak", "hardbreak"}
        ]
        if len(content_lines) != end - start or len(breaks) != len(content_lines) - 1:
            # A multiline inline construct, such as a code span, consumed a newline. Leave the
            # whole block alone because markdown-it does not expose child source positions.
            continue

        replacement = [lines[start]]
        for offset, break_type in enumerate(breaks, start=1):
            if break_type == "softbreak":
                replacement[-1] = f"{replacement[-1].rstrip()} {content_lines[offset].lstrip()}"
            else:
                replacement.append(lines[start + offset])
        replacements.append((start, end, replacement))

    for start, end, replacement in reversed(replacements):
        lines[start:end] = replacement
    return "\n".join(lines)
