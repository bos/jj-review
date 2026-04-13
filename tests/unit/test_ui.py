from __future__ import annotations

from io import StringIO

from jj_review import ui as ui_module


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
