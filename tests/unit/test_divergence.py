from jj_stack.stack.divergence import divergence_recovery_hint
from jj_stack.ui import plain_text


def test_divergence_recovery_hint_uses_scoped_converge_command() -> None:
    hint = plain_text(
        divergence_recovery_hint(
            "abcdefghijkl",
            retry="rerun sync",
        )
    )

    assert hint.startswith("Run")
    assert "jj converge -r 'change_id(abcdefghijkl)'" in hint
    assert hint.endswith("then rerun sync.")
