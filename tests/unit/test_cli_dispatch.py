from contextlib import contextmanager
from pathlib import Path

import pytest

import jj_review.cli as cli_module
from jj_review.cli import main


@pytest.fixture(autouse=True)
def no_configured_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "_load_configured_jj_color", lambda **kwargs: None)


def test_main_accepts_global_options_after_subcommand(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    def fake_status(**kwargs) -> int:
        calls.append(kwargs)
        return 0

    monkeypatch.setattr(cli_module.commands.status, "status", fake_status)

    exit_code = main(["status", "--debug", "--fetch", "--repository", str(tmp_path)])

    assert exit_code == 0
    assert calls == [
        {
            "config_path": None,
            "debug": True,
            "fetch": True,
            "repository": tmp_path,
            "revset": None,
            "verbose": False,
        }
    ]


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


def test_main_time_output_prefixes_handler_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_status(**kwargs) -> int:
        print("first line")
        print("second line")
        return 0

    monkeypatch.setattr(cli_module.commands.status, "status", fake_status)

    exit_code = main(["status", "--time-output"])
    captured = capsys.readouterr()

    assert exit_code == 0
    lines = [line for line in captured.out.splitlines() if line]
    assert lines
    assert all(line.startswith("[") for line in lines)
    assert any("first line" in line for line in lines)
    assert any("second line" in line for line in lines)


def test_main_uses_configured_jj_color_for_styled_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_load_configured_jj_color", lambda **kwargs: "debug")

    @contextmanager
    def fake_configured_console(**kwargs):
        observed.update(kwargs)
        yield

    monkeypatch.setattr(cli_module, "configured_console", fake_configured_console)
    monkeypatch.setattr(cli_module.commands.status, "status", lambda **kwargs: 0)

    exit_code = main(["status"])

    assert exit_code == 0
    assert observed["color_mode"] == "always"
    assert observed["requested_color_mode"] is None


def test_main_color_flag_overrides_configured_jj_color(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli_module, "_load_configured_jj_color", lambda **kwargs: "debug")

    @contextmanager
    def fake_configured_console(**kwargs):
        observed.update(kwargs)
        yield

    monkeypatch.setattr(cli_module, "configured_console", fake_configured_console)
    monkeypatch.setattr(cli_module.commands.status, "status", lambda **kwargs: 0)

    exit_code = main(["status", "--color=never"])

    assert exit_code == 0
    assert observed["color_mode"] == "never"
    assert observed["requested_color_mode"] == "never"
