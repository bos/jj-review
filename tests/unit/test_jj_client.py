from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jj_review.errors import CliError
from jj_review.jj import JjClient, JjCommandError, StaleWorkspaceError, UnsupportedStackError
from jj_review.models.stack import LocalRevision
from tests.support.revision_helpers import make_revision


def _revision_line(
    *,
    commit_id: str,
    parents: list[str],
    change_id: str,
    description: str,
    empty: bool = False,
    divergent: bool = False,
    hidden: bool = False,
    working_copy: bool = False,
    immutable: bool = False,
) -> str:
    import json

    fields = [
        json.dumps(change_id),
        json.dumps(commit_id),
        json.dumps(description),
        json.dumps(parents),
        "true" if empty else "false",
        "true" if divergent else "false",
        "true" if working_copy else "false",
        "true" if hidden else "false",
        "true" if immutable else "false",
    ]
    return "\t".join(fields) + "\n"


_TRUNK = _revision_line(
    commit_id="trunk", parents=["root"], change_id="trunk-change", description="main\n"
)
_ROOT = _revision_line(
    commit_id="root",
    parents=[],
    change_id="root-change",
    description="\n",
    empty=True,
    immutable=True,
)
_EMPTY_WORKING_COPY = _revision_line(
    commit_id="wc",
    parents=["head"],
    change_id="wc-change",
    description="\n",
    empty=True,
    working_copy=True,
)
_HEAD = _revision_line(
    commit_id="head", parents=["parent"], change_id="head-change", description="head\n"
)
_HEAD_ON_IMMUTABLE_PARENT = _revision_line(
    commit_id="head",
    parents=["immutable-parent"],
    change_id="head-change",
    description="head\n",
)
_PARENT = _revision_line(
    commit_id="parent", parents=["trunk"], change_id="parent-change", description="parent\n"
)
_MERGE = _revision_line(
    commit_id="merge",
    parents=["left", "right"],
    change_id="merge-change",
    description="merge\n",
)
_DIVERGENT = _revision_line(
    commit_id="divergent",
    parents=["trunk"],
    change_id="div-change",
    description="divergent\n",
    divergent=True,
)
_IMMUTABLE_PARENT = _revision_line(
    commit_id="immutable-parent",
    parents=["trunk"],
    change_id="immutable-parent-change",
    description="immutable parent\n",
    immutable=True,
)
_HIDDEN = _revision_line(
    commit_id="hidden",
    parents=["trunk"],
    change_id="hidden-change/1",
    description="hidden predecessor\n",
    hidden=True,
)
_CHILD_A = _revision_line(
    commit_id="child-a", parents=["parent"], change_id="child-a-change", description="child a\n"
)
_CHILD_B = _revision_line(
    commit_id="child-b", parents=["parent"], change_id="child-b-change", description="child b\n"
)


def test_discover_review_stack_returns_empty_revisions_when_head_is_trunk() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "trunk", "-T", _template(), "--limit", "2"): _TRUNK,
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack("trunk")

    assert stack.revisions == ()
    assert stack.head.commit_id == "trunk"


def test_discover_review_stack_uses_parent_of_empty_working_copy_as_default_selection() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "@ | @-", "-T", _template(), "--limit", "2"): (
            _EMPTY_WORKING_COPY + _HEAD
        ),
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head'", "-T", _template()): (
            _HEAD + _PARENT + _TRUNK
        ),
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack()

    assert stack.selected_revset == "@-"
    assert [revision.subject for revision in stack.revisions] == ["parent", "head"]


def test_discover_review_stack_uses_non_empty_working_copy_as_default_selection() -> None:
    working_copy_head = _revision_line(
        commit_id="head",
        parents=["parent"],
        change_id="head-change",
        description="head\n",
        working_copy=True,
    )
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "@ | @-", "-T", _template(), "--limit", "2"): (
            working_copy_head + _PARENT
        ),
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head'", "-T", _template()): (
            working_copy_head + _PARENT + _TRUNK
        ),
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack()

    assert stack.selected_revset == "@"
    assert [revision.subject for revision in stack.revisions] == ["parent", "head"]


def test_discover_review_stack_rejects_root_fallback_trunk() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _ROOT,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): _HEAD,
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match=r"trunk\(\) resolved to the root commit"):
        client.discover_review_stack("head")


def test_discover_review_stack_rejects_merge_commits() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "merge", "-T", _template(), "--limit", "2"): _MERGE,
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match="merge commits are not supported"):
        client.discover_review_stack("merge")


def test_discover_review_stack_rejects_divergent_changes() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "divergent", "-T", _template(), "--limit", "2"): (
            _DIVERGENT
        ),
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match="divergent changes are not supported") as exc:
        client.discover_review_stack("divergent")

    assert exc.value.change_id == "div-change"
    assert exc.value.reason == "divergent_change"


def test_discover_review_stack_allows_divergent_ancestor_for_inspection() -> None:
    divergent_parent = _revision_line(
        commit_id="div-parent",
        parents=["parent"],
        change_id="div-parent-change",
        description="div parent\n",
        divergent=True,
    )
    head = _revision_line(
        commit_id="head-2",
        parents=["div-parent"],
        change_id="head-2-change",
        description="head 2\n",
    )
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head-2", "-T", _template(), "--limit", "2"): head,
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head-2'", "-T", _template()): (
            head + divergent_parent + _PARENT + _TRUNK
        ),
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack(
        "head-2",
        allow_divergent=True,
    )

    assert [revision.subject for revision in stack.revisions] == [
        "parent",
        "div parent",
        "head 2",
    ]


def test_discover_review_stack_rejects_immutable_revisions() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): (
            _HEAD_ON_IMMUTABLE_PARENT
        ),
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head'", "-T", _template()): (
            _HEAD_ON_IMMUTABLE_PARENT + _IMMUTABLE_PARENT + _TRUNK
        ),
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match="immutable commits are not reviewable"):
        client.discover_review_stack("head")


def test_discover_review_stack_allows_immutable_ancestor_for_inspection() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): (
            _HEAD_ON_IMMUTABLE_PARENT
        ),
        ("jj", "log", "--no-graph", "-r", "'trunk'::'head'", "-T", _template()): (
            _HEAD_ON_IMMUTABLE_PARENT + _IMMUTABLE_PARENT + _TRUNK
        ),
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack(
        "head",
        allow_immutable=True,
    )

    assert [revision.subject for revision in stack.revisions] == [
        "immutable parent",
        "head",
    ]


def test_discover_review_stack_excludes_revisions_already_reachable_from_trunk() -> None:
    current_trunk = _revision_line(
        commit_id="current-trunk",
        parents=["old-trunk", "merged"],
        change_id="trunk-change",
        description="main\n",
    )
    merged = _revision_line(
        commit_id="merged",
        parents=["old-trunk"],
        change_id="merged-change",
        description="merged\n",
        immutable=True,
    )
    head = _revision_line(
        commit_id="head-3",
        parents=["merged"],
        change_id="head-3-change",
        description="head 3\n",
    )
    old_trunk = _revision_line(
        commit_id="old-trunk",
        parents=["root"],
        change_id="old-trunk-change",
        description="old trunk\n",
        immutable=True,
    )
    trunk_scan = _revision_with_flag_line(current_trunk, is_trunk=True) + _revision_with_flag_line(
        current_trunk,
        is_trunk=False,
    )
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "head-3", "-T", _template(), "--limit", "2"): head,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "trunk() | (merges() & ::trunk())",
            "-T",
            _trunk_scan_template(),
        ): trunk_scan,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "('current-trunk' | 'merged')::'head-3'",
            "-T",
            _template(),
        ): (
            head + merged + old_trunk + _ROOT
        ),
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack(
        "head-3",
        allow_immutable=True,
        allow_trunk_ancestors=True,
    )

    assert [revision.subject for revision in stack.revisions] == [
        "merged",
        "head 3",
    ]


def test_discover_review_stack_rejects_shared_trunk_ancestor_without_merge() -> None:
    current_trunk = _revision_line(
        commit_id="current-trunk",
        parents=["old-trunk"],
        change_id="trunk-change",
        description="main\n",
    )
    head = _revision_line(
        commit_id="head-4",
        parents=["old-trunk"],
        change_id="head-4-change",
        description="head 4\n",
    )
    old_trunk = _revision_line(
        commit_id="old-trunk",
        parents=["root"],
        change_id="old-trunk-change",
        description="old trunk\n",
        immutable=True,
    )
    trunk_scan = _revision_with_flag_line(current_trunk, is_trunk=True)
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "head-4", "-T", _template(), "--limit", "2"): head,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "trunk() | (merges() & ::trunk())",
            "-T",
            _trunk_scan_template(),
        ): trunk_scan,
        ("jj", "log", "--no-graph", "-r", "'current-trunk'::'head-4'", "-T", _template()): "",
        ("jj", "log", "--no-graph", "-r", "old-trunk", "-T", _template(), "--limit", "2"): (
            old_trunk
        ),
        ("jj", "log", "--no-graph", "-r", "root", "-T", _template(), "--limit", "2"): _ROOT,
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match=r"root commit before trunk\(\)"):
        client.discover_review_stack(
            "head-4",
            allow_immutable=True,
            allow_trunk_ancestors=True,
        )


def test_discover_review_stack_rejects_hidden_revisions() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "hidden", "-T", _template(), "--limit", "2"): _HIDDEN,
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match="hidden commits are not reviewable"):
        client.discover_review_stack("hidden")


def test_discover_review_stack_raises_jj_command_error_on_wrong_field_count() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): (
            "not\tenough\n"
        ),
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(JjCommandError, match="unexpected format"):
        client.discover_review_stack("head")


def test_discover_review_stack_raises_jj_command_error_on_invalid_json() -> None:
    # An invalid JSON value in any field should raise JjCommandError, not a
    # bare json.JSONDecodeError.
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): (
            'NOT_JSON\t"commit-id"\t"desc"\t[]\tfalse\tfalse\tfalse\tfalse\tfalse\n'
        ),
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(JjCommandError, match="invalid JSON"):
        client.discover_review_stack("head")


def test_discover_review_stack_surfaces_stale_workspace_errors() -> None:
    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "trunk()",
            "-T",
            _template(),
            "--limit",
            "2",
        )
        assert cwd == Path("/repo")
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=(
                "Error: The working copy is stale (not updated since operation abc123).\n"
                "Hint: Run `jj workspace update-stale` to update it.\n"
            ),
        )

    client = JjClient(Path("/repo"), runner=run)
    with pytest.raises(StaleWorkspaceError, match="jj workspace update-stale"):
        client.discover_review_stack("head")


def test_discover_review_stack_raises_jj_command_error_on_wrong_field_type() -> None:
    # A JSON value of the wrong type (e.g. parents as a string, not a list)
    # should raise JjCommandError rather than a bare TypeError/ValueError.
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): (
            '"change-id"\t'
            '"commit-id"\t'
            '"desc"\t'
            '"not-a-list"\t'  # parents must be a list
            "false\tfalse\tfalse\tfalse\tfalse\n"
        ),
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(JjCommandError, match="unexpected field types"):
        client.discover_review_stack("head")


def test_supported_review_stack_change_ids_allows_sibling_stacks() -> None:
    child_a = LocalRevision(
        change_id="child-a-change",
        commit_id="child-a",
        current_working_copy=False,
        description="child a\n",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("parent",),
    )
    child_b = LocalRevision(
        change_id="child-b-change",
        commit_id="child-b",
        current_working_copy=False,
        description="child b\n",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("parent",),
    )
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "::('child-a' | 'child-b')", "-T", _template()): (
            _CHILD_A + _CHILD_B + _PARENT + _TRUNK
        ),
    }

    supported = JjClient(
        Path("/repo"),
        runner=_runner(responses),
    ).supported_review_stack_change_ids((child_a, child_b))

    assert supported == {"child-a-change", "child-b-change"}


def test_update_untracked_remote_bookmark_pushes_fetches_and_tracks() -> None:
    commands: list[tuple[str, ...]] = []

    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        assert cwd == Path("/repo")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    client = JjClient(Path("/repo"), runner=run)
    client.update_untracked_remote_bookmark(
        remote="origin",
        bookmark="review/foo",
        desired_target="new123",
        expected_remote_target="old456",
    )

    assert commands == [
        (
            "git",
            "push",
            "--force-with-lease=refs/heads/review/foo:old456",
            "origin",
            "new123:refs/heads/review/foo",
        ),
        ("jj", "git", "fetch", "--remote", "origin"),
        ("jj", "bookmark", "track", "review/foo", "--remote", "origin"),
    ]


def test_delete_remote_bookmark_pushes_with_lease_and_fetches() -> None:
    commands: list[tuple[str, ...]] = []

    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        assert cwd == Path("/repo")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    client = JjClient(Path("/repo"), runner=run)
    client.delete_remote_bookmarks(
        remote="origin",
        deletions=(("review/foo", "old456"),),
    )

    assert commands == [
        (
            "git",
            "push",
            "--force-with-lease=refs/heads/review/foo:old456",
            "origin",
            ":refs/heads/review/foo",
        ),
        ("jj", "git", "fetch", "--remote", "origin"),
    ]


def test_delete_remote_bookmarks_batches_push_and_fetch() -> None:
    commands: list[tuple[str, ...]] = []

    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        assert cwd == Path("/repo")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    client = JjClient(Path("/repo"), runner=run)
    client.delete_remote_bookmarks(
        remote="origin",
        deletions=(
            ("review/foo", "old456"),
            ("review/bar", "old789"),
        ),
    )

    assert commands == [
        (
            "git",
            "push",
            "--force-with-lease=refs/heads/review/foo:old456",
            "--force-with-lease=refs/heads/review/bar:old789",
            "origin",
            ":refs/heads/review/foo",
            ":refs/heads/review/bar",
        ),
        ("jj", "git", "fetch", "--remote", "origin"),
    ]


def test_forget_bookmarks_batches_single_command() -> None:
    commands: list[tuple[str, ...]] = []

    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        assert cwd == Path("/repo")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    client = JjClient(Path("/repo"), runner=run)
    client.forget_bookmarks(("review/foo", "review/bar"))

    assert commands == [
        ("jj", "bookmark", "forget", "review/foo", "review/bar"),
    ]


def test_fetch_remote_can_limit_to_selected_branches() -> None:
    commands: list[tuple[str, ...]] = []

    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        commands.append(tuple(command))
        assert cwd == Path("/repo")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    client = JjClient(Path("/repo"), runner=run)
    client.fetch_remote(remote="origin", branches=("review/foo", "review/bar"))

    assert commands == [
        (
            "jj",
            "git",
            "fetch",
            "--remote",
            "origin",
            "--branch",
            "review/foo",
            "--branch",
            "review/bar",
        )
    ]


def test_list_remote_branches_returns_matching_branch_heads() -> None:
    responses: dict[tuple[str, ...], str] = {
        (
            "git",
            "ls-remote",
            "--refs",
            "origin",
            "refs/heads/review/*-aaaaaaaa",
            "refs/heads/review/*-bbbbbbbb",
        ): (
            "abc123\trefs/heads/review/feature-aaaaaaaa\n"
            "def456\trefs/heads/review/parent-bbbbbbbb\n"
        ),
    }

    branches = JjClient(Path("/repo"), runner=_runner(responses)).list_remote_branches(
        remote="origin",
        patterns=("refs/heads/review/*-aaaaaaaa", "refs/heads/review/*-bbbbbbbb"),
    )

    assert branches == {
        "review/feature-aaaaaaaa": "abc123",
        "review/parent-bbbbbbbb": "def456",
    }


def test_list_bookmark_states_treats_null_targets_as_deleted() -> None:
    responses: dict[tuple[str, ...], str] = {
        (
            "jj",
            "bookmark",
            "list",
            "--all-remotes",
            "-T",
            'json(self) ++ "\\n"',
            "review/foo",
        ): (
            '{"name":"review/foo","target":[null]}\n'
            '{"name":"review/foo","remote":"origin","target":["abc123"],'
            '"tracking_target":[null]}\n'
        ),
    }

    bookmark_state = JjClient(Path("/repo"), runner=_runner(responses)).get_bookmark_state(
        "review/foo"
    )

    assert bookmark_state.local_targets == ()
    remote_state = bookmark_state.remote_target("origin")
    assert remote_state is not None
    assert remote_state.targets == ("abc123",)
    assert remote_state.tracking_targets == ()


def test_get_config_string_returns_value_when_key_is_set() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "git.private-commits"): "description(private)\n",
    }

    value = JjClient(Path("/repo"), runner=_runner(responses)).get_config_string(
        "git.private-commits"
    )

    assert value == "description(private)"


def test_get_config_string_returns_none_when_key_is_unset() -> None:
    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == ("jj", "config", "get", "git.private-commits")
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Config error: no config value for 'git.private-commits'\n",
        )

    value = JjClient(Path("/repo"), runner=run).get_config_string("git.private-commits")

    assert value is None


def test_resolve_color_when_honors_explicit_jj_config() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "ui.color"): "debug\n",
    }

    value = JjClient(Path("/repo"), runner=_runner(responses)).resolve_color_when(
        stdout_is_tty=True
    )

    assert value == "debug"


def test_resolve_color_when_maps_auto_to_terminal_capability() -> None:
    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == ("jj", "config", "get", "ui.color")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="no config\n")

    client = JjClient(Path("/repo"), runner=run)

    assert client.resolve_color_when(stdout_is_tty=True) == "always"
    assert client.resolve_color_when(stdout_is_tty=False) == "never"


def test_resolve_color_when_cli_override_beats_jj_config() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "ui.color"): "debug\n",
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))

    assert client.resolve_color_when(cli_color="never", stdout_is_tty=True) == "never"
    assert client.resolve_color_when(cli_color="auto", stdout_is_tty=False) == "never"
    assert client.resolve_color_when(cli_color="auto", stdout_is_tty=True) == "always"


def test_render_revision_log_lines_uses_native_jj_log_output() -> None:
    revision = make_revision(
        commit_id="head",
        change_id="head-change",
        description="subject\n",
    )
    responses: dict[tuple[str, ...], str] = {
        (
            "jj",
            "--ignore-working-copy",
            "--no-pager",
            "--color",
            "always",
            "log",
            "-r",
            "'head'",
            "--limit",
            "1",
        ): "rendered head\nbody line\n~\n",
    }

    lines = JjClient(Path("/repo"), runner=_runner(responses)).render_revision_log_lines(
        revision,
        color_when="always",
    )

    assert lines == ("rendered head", "body line")


def test_find_private_commits_returns_empty_when_config_is_unset() -> None:
    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == ("jj", "config", "get", "git.private-commits")
        return subprocess.CompletedProcess(command, 1, stdout="", stderr="no config\n")

    revisions = (
        make_revision(commit_id="head", change_id="head-change", description="head\n"),
        make_revision(commit_id="parent", change_id="parent-change", description="parent\n"),
    )
    result = JjClient(Path("/repo"), runner=run).find_private_commits(revisions)

    assert result == ()


def test_find_private_commits_returns_matching_revisions() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "git.private-commits"): "description(private)\n",
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "(description(private)) & ('head' | 'parent')",
            "-T",
            _template(),
        ): _HEAD,
    }

    revisions = (
        make_revision(commit_id="head", change_id="head-change", description="head\n"),
        make_revision(commit_id="parent", change_id="parent-change", description="parent\n"),
    )
    result = JjClient(Path("/repo"), runner=_runner(responses)).find_private_commits(revisions)

    assert len(result) == 1
    assert result[0].commit_id == "head"


def test_find_private_commits_returns_empty_when_no_revision_matches() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "git.private-commits"): "description(private)\n",
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "(description(private)) & ('head' | 'parent')",
            "-T",
            _template(),
        ): "",
    }

    revisions = (
        make_revision(commit_id="head", change_id="head-change", description="head\n"),
        make_revision(commit_id="parent", change_id="parent-change", description="parent\n"),
    )
    result = JjClient(Path("/repo"), runner=_runner(responses)).find_private_commits(revisions)

    assert result == ()


def test_find_private_commits_returns_empty_when_revisions_is_empty() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "config", "get", "git.private-commits"): "description(private)\n",
    }

    result = JjClient(Path("/repo"), runner=_runner(responses)).find_private_commits(())

    assert result == ()


def test_query_revisions_returns_empty_when_change_id_no_longer_exists() -> None:
    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "missing-change-id",
            "-T",
            _template(),
            "--limit",
            "2",
        )
        assert cwd == Path("/repo")
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr="Error: Revision `missing-change-id` doesn't exist\n",
        )

    client = JjClient(Path("/repo"), runner=run)

    assert client.query_revisions("missing-change-id", limit=2) == ()


def test_query_revisions_by_change_ids_batches_lookup_and_groups_results() -> None:
    change_1 = _revision_line(
        commit_id="commit-1",
        parents=["trunk"],
        change_id="change-1",
        description="change 1\n",
    )
    change_2 = _revision_line(
        commit_id="commit-2",
        parents=["trunk"],
        change_id="change-2",
        description="change 2\n",
    )
    responses: dict[tuple[str, ...], str] = {
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "(present('change-1') | present('change-2'))",
            "-T",
            _template(),
        ): change_1 + change_2,
    }

    grouped = JjClient(Path("/repo"), runner=_runner(responses)).query_revisions_by_change_ids(
        ("change-1", "change-2")
    )

    assert grouped == {
        "change-1": (
            LocalRevision(
                change_id="change-1",
                commit_id="commit-1",
                current_working_copy=False,
                description="change 1\n",
                divergent=False,
                empty=False,
                hidden=False,
                immutable=False,
                parents=("trunk",),
            ),
        ),
        "change-2": (
            LocalRevision(
                change_id="change-2",
                commit_id="commit-2",
                current_working_copy=False,
                description="change 2\n",
                divergent=False,
                empty=False,
                hidden=False,
                immutable=False,
                parents=("trunk",),
            ),
        ),
    }


def test_query_ancestor_revisions_and_children_by_parent_for_commit_ids() -> None:
    child = _revision_line(
        commit_id="child",
        parents=["parent"],
        change_id="child-change",
        description="child\n",
    )
    parent = _revision_line(
        commit_id="parent",
        parents=["trunk"],
        change_id="parent-change",
        description="parent\n",
    )
    sibling = _revision_line(
        commit_id="sibling",
        parents=["parent"],
        change_id="sibling-change",
        description="sibling\n",
    )
    responses: dict[tuple[str, ...], str] = {
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "::'child'",
            "-T",
            _template(),
        ): child + parent + _TRUNK,
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "children(::'child')",
            "-T",
            _template(),
        ): child + sibling,
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))

    ancestors = client.query_ancestor_revisions(("child",))
    children_by_parent = client.query_children_by_parent_for_commit_ids(("child",))

    assert [revision.commit_id for revision in ancestors] == ["child", "parent", "trunk"]
    assert [revision.commit_id for revision in children_by_parent["parent"]] == [
        "child",
        "sibling",
    ]


@pytest.mark.parametrize(
    ("revset", "stderr", "expected_message"),
    [
        pytest.param(
            "missing-change-id",
            "Error: Revision `missing-change-id` doesn't exist\n",
            r"Revision `missing-change-id` doesn't exist",
            id="missing-revset",
        ),
        pytest.param(
            "x(",
            (
                "Error: Failed to parse revset: Syntax error\n"
                "Hint: See https://docs.jj-vcs.dev/latest/revsets/\n"
            ),
            r"Invalid revset x\(: Failed to parse revset: Syntax error\.",
            id="invalid-revset",
        ),
    ],
)
def test_resolve_revision_reports_revset_errors_without_wrapped_command(
    revset: str,
    stderr: str,
    expected_message: str,
) -> None:
    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        assert tuple(command) == (
            "jj",
            "log",
            "--no-graph",
            "-r",
            revset,
            "-T",
            _template(),
            "--limit",
            "2",
        )
        assert cwd == Path("/repo")
        return subprocess.CompletedProcess(
            command,
            1,
            stdout="",
            stderr=stderr,
        )

    client = JjClient(Path("/repo"), runner=run)

    with pytest.raises(CliError, match=expected_message):
        client.resolve_revision(revset)


def _template() -> str:
    return (
        r'json(change_id) ++ "\t" ++ json(commit_id) ++ "\t" ++ json(description) ++ "\t" ++ '
        r'json(parents.map(|p| p.commit_id())) ++ "\t" ++ '
        r'json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
        r'json(current_working_copy) ++ "\t" ++ json(self.hidden()) ++ "\t" ++ '
        r'json(immutable) ++ "\n"'
    )


def _trunk_scan_template() -> str:
    return _template().removesuffix(r'"\n"') + r'"\t" ++ json(self.contained_in("trunk()")) ++ "\n"'


def _revision_with_flag_line(revision_line: str, *, is_trunk: bool) -> str:
    return revision_line.removesuffix("\n") + f"\t{'true' if is_trunk else 'false'}\n"


def _runner(
    responses: dict[tuple[str, ...], str],
):
    def run(command: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        key = tuple(command)
        assert cwd == Path("/repo")
        if key not in responses:
            raise AssertionError(f"Unexpected command: {key!r}")
        return subprocess.CompletedProcess(command, 0, stdout=responses[key], stderr="")

    return run
