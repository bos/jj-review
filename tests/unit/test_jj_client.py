from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jj_review.jj import JjClient, UnsupportedStackError

_TRUNK = (
    '{"commit_id":"trunk","parents":["root"],"change_id":"trunk-change",'
    '"description":"main\\n","author":{},"committer":{}}'
    "\tfalse\tfalse\tfalse\tfalse\n"
)
_ROOT = (
    '{"commit_id":"root","parents":[],"change_id":"root-change",'
    '"description":"\\n","author":{},"committer":{}}'
    "\ttrue\tfalse\tfalse\ttrue\n"
)
_EMPTY_WORKING_COPY = (
    '{"commit_id":"wc","parents":["head"],"change_id":"wc-change",'
    '"description":"\\n","author":{},"committer":{}}'
    "\ttrue\tfalse\ttrue\tfalse\n"
)
_HEAD = (
    '{"commit_id":"head","parents":["parent"],"change_id":"head-change",'
    '"description":"head\\n","author":{},"committer":{}}'
    "\tfalse\tfalse\tfalse\tfalse\n"
)
_HEAD_ON_IMMUTABLE_PARENT = (
    '{"commit_id":"head","parents":["immutable-parent"],"change_id":"head-change",'
    '"description":"head\\n","author":{},"committer":{}}'
    "\tfalse\tfalse\tfalse\tfalse\n"
)
_PARENT = (
    '{"commit_id":"parent","parents":["trunk"],"change_id":"parent-change",'
    '"description":"parent\\n","author":{},"committer":{}}'
    "\tfalse\tfalse\tfalse\tfalse\n"
)
_MERGE = (
    '{"commit_id":"merge","parents":["left","right"],"change_id":"merge-change",'
    '"description":"merge\\n","author":{},"committer":{}}'
    "\tfalse\tfalse\tfalse\tfalse\n"
)
_DIVERGENT = (
    '{"commit_id":"divergent","parents":["trunk"],"change_id":"div-change",'
    '"description":"divergent\\n","author":{},"committer":{}}'
    "\tfalse\ttrue\tfalse\tfalse\n"
)
_IMMUTABLE_PARENT = (
    '{"commit_id":"immutable-parent","parents":["trunk"],'
    '"change_id":"immutable-parent-change","description":"immutable parent\\n",'
    '"author":{},"committer":{}}'
    "\tfalse\tfalse\tfalse\ttrue\n"
)
_CHILD_A = (
    '{"commit_id":"child-a","parents":["parent"],"change_id":"child-a-change",'
    '"description":"child a\\n","author":{},"committer":{}}'
    "\tfalse\tfalse\tfalse\tfalse\n"
)
_CHILD_B = (
    '{"commit_id":"child-b","parents":["parent"],"change_id":"child-b-change",'
    '"description":"child b\\n","author":{},"committer":{}}'
    "\tfalse\tfalse\tfalse\tfalse\n"
)
_DIVERGENT_SIBLING = (
    '{"commit_id":"div-sibling","parents":["parent"],"change_id":"div-sibling-change",'
    '"description":"divergent sibling\\n","author":{},"committer":{}}'
    "\tfalse\ttrue\tfalse\tfalse\n"
)


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


def _template() -> str:
    return (
        r'json(self) ++ "\t" ++ json(empty) ++ "\t" ++ json(divergent) ++ "\t" ++ '
        r'json(current_working_copy) ++ "\t" ++ json(immutable) ++ "\n"'
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
