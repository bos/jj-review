from __future__ import annotations

import subprocess
from importlib import import_module
from io import StringIO
from pathlib import Path

import pytest

from jj_review import ui as ui_module


def _style_cls():
    return import_module("rich.style").Style


def test_output_helpers_route_to_expected_streams() -> None:
    stdout = StringIO()
    stderr = StringIO()

    with ui_module.configured_ui(
        stdout=stdout,
        stderr=stderr,
        color_mode="never",
    ):
        ui_module.output("plain", "[status]")
        ui_module.note("note")
        ui_module.warning("warn")
        ui_module.error("boom")

    assert stdout.getvalue() == "plain [status]\nnote\n"
    assert stderr.getvalue() == "warn\nboom\n"


def test_time_output_prefixes_each_rendered_line() -> None:
    stdout = StringIO()

    with ui_module.configured_ui(
        stdout=stdout,
        stderr=StringIO(),
        color_mode="never",
        time_output=True,
    ):
        ui_module.output("first\nsecond")
        ui_module.output("third", end="")

    rendered = stdout.getvalue().splitlines()
    assert len(rendered) == 3
    assert rendered[0].endswith("first")
    assert rendered[1].endswith("second")
    assert rendered[2].endswith("third")
    for line in rendered:
        assert line.startswith("[")
        assert "] " in line


def test_semantic_style_uses_machine_readable_jj_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path.cwd()
    stdout = (
        'colors.change_id\0"ansi-color-81"\n'
        'colors.working_copy.bold\0true\n'
        'colors."working_copy change_id"\0"bright magenta"\n'
    )

    def fake_run(command, **kwargs):
        assert command == [
            "jj",
            "config",
            "list",
            "--include-defaults",
            "colors",
            "-T",
            r'name ++ "\0" ++ json(value) ++ "\n"',
        ]
        assert kwargs["cwd"] == repository
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(ui_module.subprocess, "run", fake_run)

    with ui_module.configured_ui(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="never",
        repository=repository,
    ):
        assert ui_module.semantic_style("missing") is None
        assert ui_module.semantic_style("change_id") == _style_cls()(color="color(81)")
        assert ui_module.semantic_style("working_copy", "change_id") == _style_cls()(
            color="bright_magenta",
            bold=True,
        )


def test_note_defaults_to_hint_heading_semantic_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    sentinel = _style_cls()(color="red")

    class FakeConsole:
        def print(self, *objects, **kwargs) -> None:
            observed["objects"] = objects
            observed["kwargs"] = kwargs

    def fake_semantic_style(*labels: str):
        observed["labels"] = labels
        return sentinel

    monkeypatch.setattr(ui_module, "_STDOUT_CONSOLE", FakeConsole())
    monkeypatch.setattr(ui_module, "semantic_style", fake_semantic_style)

    ui_module.note("note")

    assert observed["labels"] == ("hint heading",)
    assert observed["objects"] == ("note",)
    assert observed["kwargs"] == {"markup": False, "style": sentinel}
