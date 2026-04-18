from __future__ import annotations

import subprocess
from importlib import import_module
from io import StringIO
from pathlib import Path

import pytest

from jj_review import console as console_module, ui as ui_module


def _style_cls():
    return import_module("rich.style").Style


def test_output_helpers_route_to_expected_streams() -> None:
    stdout = StringIO()
    stderr = StringIO()

    with console_module.configured_console(
        stdout=stdout,
        stderr=stderr,
        color_mode="never",
    ):
        console_module.output("plain", "[status]")
        console_module.note("note")
        console_module.warning("warn")
        console_module.error("boom")

    assert stdout.getvalue() == "plain [status]\nnote\n"
    assert stderr.getvalue() == "warn\nboom\n"


def test_output_accepts_semantic_messages_directly() -> None:
    stdout = StringIO()

    with console_module.configured_console(
        stdout=stdout,
        stderr=StringIO(),
        color_mode="never",
    ):
        console_module.output("The selected stack has no changes to review.")

    assert stdout.getvalue() == "The selected stack has no changes to review.\n"


def test_output_decodes_ansi_strings_before_printing() -> None:
    stdout = StringIO()

    with console_module.configured_console(
        stdout=stdout,
        stderr=StringIO(),
        color_mode="never",
    ):
        console_module.output("\x1b[31mred\x1b[0m")

    assert stdout.getvalue() == "red\n"


def test_output_prefixed_line_ends_with_newline() -> None:
    stdout = StringIO()

    with console_module.configured_console(
        stdout=stdout,
        stderr=StringIO(),
        color_mode="always",
    ):
        console_module.output(
            ui_module.prefixed_line(
                "  ✗ ",
                (ui_module.semantic_text("stop", "prefix"), ": ", "before something"),
                prefix_labels=("error heading",),
                message_labels=("warning heading",),
            )
        )

    assert stdout.getvalue().endswith("\n")


def test_progress_uses_rich_progress_on_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    progress_updates: list[int] = []
    progress_calls: list[dict[str, object]] = []

    class TtyStringIO(StringIO):
        def isatty(self) -> bool:
            return True

    class FakeProgress:
        def __init__(self, *columns, **kwargs):
            del columns
            self.kwargs = kwargs

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def add_task(self, description: str, *, total: int) -> str:
            progress_calls.append(
                {
                    "console": self.kwargs["console"],
                    "description": description,
                    "total": total,
                    "transient": self.kwargs["transient"],
                }
            )
            return "task-1"

        def advance(self, task_id: str, amount: int) -> None:
            assert task_id == "task-1"
            progress_updates.append(amount)

    monkeypatch.setattr(console_module, "Progress", FakeProgress)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=TtyStringIO(),
        color_mode="never",
    ):
        with console_module.progress(description="Inspecting GitHub", total=2) as progress:
            progress.advance()
            progress.advance()

    assert progress_updates == [1, 1]
    assert progress_calls[0]["description"] == "Inspecting GitHub"
    assert progress_calls[0]["total"] == 2
    assert progress_calls[0]["transient"] is True


def test_progress_skips_rich_progress_without_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_if_progress_used(*args, **kwargs):
        raise AssertionError("rich Progress should not run without a TTY")

    monkeypatch.setattr(console_module, "Progress", fail_if_progress_used)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="never",
    ):
        with console_module.progress(description="Inspecting GitHub", total=2) as progress:
            progress.advance()


def test_time_output_prefixes_each_rendered_line() -> None:
    stdout = StringIO()

    with console_module.configured_console(
        stdout=stdout,
        stderr=StringIO(),
        color_mode="never",
        time_output=True,
    ):
        console_module.output("first\nsecond")
        console_module.output("third", end="")

    rendered = stdout.getvalue().splitlines()
    assert len(rendered) == 3
    assert rendered[0].endswith("first")
    assert rendered[1].endswith("second")
    assert rendered[2].endswith("third")
    for line in rendered:
        assert line.startswith("[")
        assert "] " in line


def test_time_output_wraps_content_after_prefix_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    console_cls = import_module("rich.console").Console
    stream = StringIO()
    monkeypatch.setattr(console_module.time, "perf_counter", lambda: 0.0)

    console = console_module._ConfiguredConsole(
        console_cls(file=stream, force_terminal=False, width=15),
        prefix_style=None,
        start=0.0,
        time_output=True,
    )
    console.print("abcdef", end="")

    assert stream.getvalue() == "[0.000000] abcd\n[0.000000] ef"


def test_time_output_prefix_uses_prefix_and_timestamp_semantic_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path.cwd()
    stdout = 'colors.prefix.bold\0true\ncolors.timestamp\0"cyan"\n'

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(console_module.subprocess, "run", fake_run)
    monkeypatch.setattr(console_module.time, "perf_counter", lambda: 0.0)

    console_cls = import_module("rich.console").Console
    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="always",
        repository=repository,
        time_output=True,
    ):
        console = console_cls(width=40)
        lines = console.render_lines(
            console_module._TimePrefixedRenderable(
                renderable="timed",
                end="",
                prefix_style=console_module.semantic_style("prefix", "timestamp"),
                start=0.0,
            ),
            console.options,
            pad=False,
        )

    prefix_segment = lines[0][0]
    assert prefix_segment.text == "[0.000000] "
    assert prefix_segment.style == _style_cls()(color="cyan", bold=True)


def test_semantic_style_uses_machine_readable_jj_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path.cwd()
    stdout = (
        'colors.change_id\0"ansi-color-81"\n'
        "colors.working_copy.bold\0true\n"
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

    monkeypatch.setattr(console_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="never",
        repository=repository,
    ):
        assert console_module.semantic_style("missing") is None
        assert console_module.semantic_style("change_id") == _style_cls()(color="color(81)")
        assert console_module.semantic_style("working_copy", "change_id") == _style_cls()(
            color="bright_magenta",
            bold=True,
        )


def test_rich_text_renders_template_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path.cwd()
    stdout = (
        'colors.local_bookmarks\0"green"\n'
        "colors.change_id.bold\0true\n"
        'colors.change_id\0"ansi-color-81"\n'
    )

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(console_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="never",
        repository=repository,
    ):
        text = console_module.rich_text(
            t"delete {ui_module.bookmark('review/feature-aaaaaaaa')} for "
            t"{ui_module.change_id('aaaa1111bbbb2222')}"
        )

    assert text.plain == "delete review/feature-aaaaaaaa for aaaa1111"
    assert text.spans[0].start == 7
    assert text.spans[0].end == 30
    assert text.spans[0].style == _style_cls()(color="green")
    assert text.spans[1].start == 35
    assert text.spans[1].end == 43
    assert text.spans[1].style == _style_cls()(color="color(81)", bold=True)


def test_revset_uses_semantic_style(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Path.cwd()
    stdout = 'colors.revset\0"blue"\n'

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(console_module.subprocess, "run", fake_run)

    with console_module.configured_console(
        stdout=StringIO(),
        stderr=StringIO(),
        color_mode="never",
        repository=repository,
    ):
        text = console_module.rich_text(ui_module.revset("trunk()"))

    assert text.plain == "trunk()"
    assert text.spans == [import_module("rich.text").Span(0, 7, _style_cls()(color="blue"))]
