import pytest

from jj_review.command_ui import resolve_selected_revset
from jj_review.errors import CliError


def test_resolve_selected_revset_returns_explicit_value() -> None:
    assert (
        resolve_selected_revset(
            command_label="submit",
            default_revset="@-",
            require_explicit=False,
            revset="@",
        )
        == "@"
    )


def test_resolve_selected_revset_uses_default_when_omitted() -> None:
    assert (
        resolve_selected_revset(
            command_label="submit",
            default_revset="@-",
            require_explicit=False,
            revset=None,
        )
        == "@-"
    )


def test_resolve_selected_revset_requires_explicit_selection() -> None:
    with pytest.raises(CliError, match="requires an explicit revision selection"):
        resolve_selected_revset(
            command_label="relink",
            require_explicit=True,
            revset=None,
        )
