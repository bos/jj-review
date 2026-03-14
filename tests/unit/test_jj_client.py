from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from jj_review.jj import JjClient, JjCommandError, StaleWorkspaceError, UnsupportedStackError


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
_DIVERGENT_SIBLING = _revision_line(
    commit_id="div-sibling",
    parents=["parent"],
    change_id="div-sibling-change",
    description="divergent sibling\n",
    divergent=True,
)


def test_discover_review_stack_returns_empty_revisions_when_head_is_trunk() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "trunk", "-T", _template(), "--limit", "2"): _TRUNK,
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack("trunk")

    assert stack.revisions == ()
    assert stack.head.commit_id == "trunk"


def test_discover_review_stack_defaults_to_parent_of_empty_working_copy() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "@", "-T", _template(), "--limit", "2"): (
            _EMPTY_WORKING_COPY
        ),
        ("jj", "log", "--no-graph", "-r", "@-", "-T", _template(), "--limit", "2"): _HEAD,
        ("jj", "log", "--no-graph", "-r", "parent", "-T", _template(), "--limit", "2"): _PARENT,
        ("jj", "log", "--no-graph", "-r", "trunk", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "children('parent')", "-T", _template()): _HEAD,
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack()

    assert stack.selected_revset == "@-"
    assert [revision.subject for revision in stack.revisions] == ["parent", "head"]


def test_discover_review_stack_rejects_root_fallback_trunk() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _ROOT,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): _HEAD,
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match=r"`trunk\(\)` resolved to the root commit"):
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
    with pytest.raises(UnsupportedStackError, match="divergent changes are not supported"):
        client.discover_review_stack("divergent")


def test_discover_review_stack_rejects_immutable_revisions() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): (
            _HEAD_ON_IMMUTABLE_PARENT
        ),
        (
            "jj",
            "log",
            "--no-graph",
            "-r",
            "immutable-parent",
            "-T",
            _template(),
            "--limit",
            "2",
        ): _IMMUTABLE_PARENT,
        ("jj", "log", "--no-graph", "-r", "children('immutable-parent')", "-T", _template()): (
            _HEAD
        ),
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match="immutable commits are not reviewable"):
        client.discover_review_stack("head")


def test_discover_review_stack_rejects_hidden_revisions() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "hidden", "-T", _template(), "--limit", "2"): _HIDDEN,
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(UnsupportedStackError, match="hidden commits are not reviewable"):
        client.discover_review_stack("hidden")


def test_discover_review_stack_rejects_multiple_reviewable_children() -> None:
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): _HEAD,
        ("jj", "log", "--no-graph", "-r", "parent", "-T", _template(), "--limit", "2"): _PARENT,
        ("jj", "log", "--no-graph", "-r", "trunk", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "children('parent')", "-T", _template()): (
            _CHILD_A + _CHILD_B
        ),
    }

    client = JjClient(Path("/repo"), runner=_runner(responses))
    with pytest.raises(
        UnsupportedStackError,
        match="multiple reviewable children require separate PR chains",
    ):
        client.discover_review_stack("head")


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


def test_discover_review_stack_excludes_divergent_siblings_from_child_count() -> None:
    # A divergent sibling of a node in the walk path must not be counted as a
    # second reviewable child.  Before the fix, is_reviewable() did not exclude
    # divergent revisions, so the walk would fail with "multiple reviewable
    # children" instead of succeeding.
    responses: dict[tuple[str, ...], str] = {
        ("jj", "log", "--no-graph", "-r", "trunk()", "-T", _template(), "--limit", "2"): _TRUNK,
        ("jj", "log", "--no-graph", "-r", "head", "-T", _template(), "--limit", "2"): _HEAD,
        ("jj", "log", "--no-graph", "-r", "parent", "-T", _template(), "--limit", "2"): _PARENT,
        ("jj", "log", "--no-graph", "-r", "trunk", "-T", _template(), "--limit", "2"): _TRUNK,
        # parent has one valid child (head) and one divergent sibling — only
        # head is reviewable, so there is no branching conflict.
        ("jj", "log", "--no-graph", "-r", "children('parent')", "-T", _template()): (
            _HEAD + _DIVERGENT_SIBLING
        ),
    }

    stack = JjClient(Path("/repo"), runner=_runner(responses)).discover_review_stack("head")

    assert [r.subject for r in stack.revisions] == ["parent", "head"]


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


def _template() -> str:
    return (
        r'json(change_id) ++ "\t" ++ json(commit_id) ++ "\t" ++ json(description) ++ "\t" ++ '
        r'json(parents.map(|p| p.commit_id())) ++ "\t" ++ '
        r'json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
        r'json(current_working_copy) ++ "\t" ++ json(self.hidden()) ++ "\t" ++ '
        r'json(immutable) ++ "\n"'
    )


def _runner(
    responses: dict[tuple[str, ...], str],
):
    def run(command: tuple[str, ...] | list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        key = tuple(command)
        assert cwd == Path("/repo")
        if key not in responses:
            raise AssertionError(f"Unexpected command: {key!r}")
        return subprocess.CompletedProcess(command, 0, stdout=responses[key], stderr="")

    return run
