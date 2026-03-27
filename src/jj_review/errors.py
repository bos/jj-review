"""User-facing error types shared across CLI commands."""

from __future__ import annotations


class CliError(RuntimeError):
    """Base error for user-facing CLI failures."""

    exit_code = 1
