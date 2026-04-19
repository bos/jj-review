import pytest

import jj_review.cli as cli_module
from jj_review.cli import main


@pytest.fixture(autouse=True)
def no_configured_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_load_configured_jj_color", lambda **kwargs: None)


def test_main_reports_keyboard_interrupt_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_module.commands.status,
        "status",
        lambda **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    exit_code = main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert captured.out == ""
    assert captured.err.strip() == "Interrupted."
    assert "Traceback" not in captured.err


def test_main_preserves_partial_handler_output_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_status(**kwargs) -> int:
        print("before interrupt")
        raise KeyboardInterrupt()

    monkeypatch.setattr(cli_module.commands.status, "status", fake_status)

    exit_code = main(["status"])
    captured = capsys.readouterr()

    assert exit_code == 130
    assert "before interrupt" in captured.out
    assert captured.err.strip() == "Interrupted."
    assert "Traceback" not in captured.err
