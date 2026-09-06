from __future__ import annotations

import pytest

from jj_stack.errors import EXIT_NO_STACK, CliError, error_hint, error_message
from jj_stack.stack.selected import require_submittable_changes
from jj_stack.ui import plain_text
from tests.support.change_helpers import make_change

_CHANGE_ID = "abcdefghijkl"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    (
        pytest.param({"hidden": True}, "hidden changes cannot be submitted", id="hidden"),
        pytest.param(
            {"immutable": True}, "immutable changes cannot be submitted", id="immutable"
        ),
        pytest.param({"divergent": True}, "jj converge -r", id="divergent"),
        pytest.param({"empty": True}, "jj abandon abcdefgh", id="empty"),
        pytest.param({"description": "\n"}, "jj describe abcdefgh", id="undescribed"),
    ),
)
def test_require_submittable_changes_refuses_each_unpublishable_shape(
    overrides: dict[str, object], expected: str
) -> None:
    """Any such change anywhere in the selection stops the command before GitHub is touched."""

    described = make_change(change_id="published", commit_id="c1", description="feature\n")
    offender = make_change(
        change_id=_CHANGE_ID, commit_id="c2", description="feature\n"
    ).model_copy(update=overrides)

    require_submittable_changes((described,))
    with pytest.raises(CliError) as caught:
        require_submittable_changes((described, offender))

    rendered = plain_text(error_message(caught.value)) + plain_text(
        error_hint(caught.value) or ""
    )
    assert caught.value.exit_code == EXIT_NO_STACK
    assert _CHANGE_ID[:8] in rendered
    assert expected in rendered
