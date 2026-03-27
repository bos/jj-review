"""Shared CLI argument helpers for command modules."""

from __future__ import annotations

from collections.abc import Sequence

from jj_review.errors import CliError


def resolve_selected_revset(
    *,
    command_label: str,
    current: bool,
    require_explicit: bool,
    revset: str | None,
) -> str | None:
    """Resolve `<revset>` versus `--current` for revision-oriented commands."""

    if current and revset is not None:
        raise CliError(
            f"`{command_label}` accepts either `<revset>` or `--current`, not both."
        )
    if current:
        return None
    if revset is not None:
        return revset
    if require_explicit:
        raise CliError(
            f"`{command_label}` requires an explicit revision selection; "
            "pass `<revset>` or `--current`."
        )
    return None


def parse_comma_separated_flag_values(
    values: Sequence[str] | None,
) -> list[str] | None:
    """Parse repeated comma-separated flag values into a deduplicated list."""

    if values is None:
        return None

    parsed_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in value.split(","):
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            parsed_values.append(normalized)
    return parsed_values
