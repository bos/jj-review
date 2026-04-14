"""User-facing error types shared across CLI commands."""

from __future__ import annotations

from string.templatelib import Template

from jj_review import ui

type ErrorMessage = str | Template | ui.SemanticText | tuple[object, ...]


def error_message(error: BaseException) -> ErrorMessage:
    """Return a user-facing renderable for an exception."""

    if isinstance(error, CliError):
        return error.message
    return str(error)


class CliError(RuntimeError):
    """Base error for user-facing CLI failures."""

    exit_code = 1

    def __init__(self, message: ErrorMessage) -> None:
        self.message = message
        super().__init__(ui.plain_text(message))

    def __str__(self) -> str:
        return ui.plain_text(self.message)
