from __future__ import annotations

from io import StringIO
from types import SimpleNamespace
from typing import Any, cast

from jj_review import console as console_module
from jj_review.commands.list_ import _discover_stacks, _state_from_status
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.models.stack import LocalRevision
from jj_review.review.status import ReviewStatusRevision


def _render(value: object) -> str:
    stdout = StringIO()
    with console_module.configured_console(stdout=stdout, stderr=StringIO(), color_mode="never"):
        console_module.output(value)
    return stdout.getvalue()


def _open_revision(
    *, is_draft: bool = False, review_decision: str | None = None
) -> ReviewStatusRevision:
    return cast(
        Any,
        SimpleNamespace(
            cached_change=SimpleNamespace(has_review_identity=True),
            link_state="active",
            pull_request_lookup=SimpleNamespace(
                pull_request=SimpleNamespace(is_draft=is_draft, state="open"),
                review_decision=review_decision,
                state="open",
            ),
        ),
    )


def test_state_from_status_renders_approved_draft_as_draft_only() -> None:
    revision = _open_revision(is_draft=True, review_decision="approved")

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=(revision,),
        )
    )

    assert "draft" in rendered
    assert "approved" not in rendered


def test_state_from_status_renders_changes_requested_draft_as_draft_only() -> None:
    revision = _open_revision(is_draft=True, review_decision="changes_requested")

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=(revision,),
        )
    )

    assert "draft" in rendered
    assert "changes requested" not in rendered


def test_state_from_status_separates_drafts_from_open_published() -> None:
    revisions = (
        _open_revision(is_draft=True),
        _open_revision(is_draft=False, review_decision="approved"),
    )

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=revisions,
        )
    )

    assert "draft" in rendered
    assert "1 approved" in rendered


def test_state_from_status_reports_github_unavailable_on_remote_error() -> None:
    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error="boom",
            revisions=(),
        )
    )

    assert "GitHub unavailable" in rendered


def test_state_from_status_collapses_approved_label_when_all_open_are_approved() -> None:
    revisions = (
        _open_revision(review_decision="approved"),
        _open_revision(review_decision="approved"),
    )

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=revisions,
        )
    )

    assert "approved" in rendered
    assert "2 approved" not in rendered


def test_state_from_status_marks_stale_saved_pull_request_link() -> None:
    revision = cast(
        Any,
        SimpleNamespace(
            cached_change=SimpleNamespace(
                has_review_identity=True,
                pr_number=7,
                pr_url="https://example.test/pr/7",
            ),
            link_state="active",
            pull_request_lookup=SimpleNamespace(
                pull_request=None,
                review_decision=None,
                review_decision_error=None,
                state="missing",
            ),
        ),
    )

    rendered = _render(
        _state_from_status(
            github_error=None,
            local_fragments=(),
            remote_error=None,
            revisions=(revision,),
        )
    )

    assert "stale link" in rendered


def test_discover_stacks_extends_only_tracked_heads_for_fully_tracked_linear_stack() -> None:
    root = LocalRevision(
        change_id="a" * 32,
        commit_id="commit-a",
        current_working_copy=False,
        description="feature 1",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("main",),
    )
    middle = LocalRevision(
        change_id="b" * 32,
        commit_id="commit-b",
        current_working_copy=False,
        description="feature 2",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("commit-a",),
    )
    head = LocalRevision(
        change_id="c" * 32,
        commit_id="commit-c",
        current_working_copy=False,
        description="feature 3",
        divergent=False,
        empty=False,
        hidden=False,
        immutable=False,
        parents=("commit-b",),
    )
    tracked_revisions = {
        root.change_id: (root,),
        middle.change_id: (middle,),
        head.change_id: (head,),
    }
    queried_descendants: list[tuple[str, ...]] = []
    queried_base_parents: list[tuple[str, ...]] = []
    base_parent = root.model_copy(update={"commit_id": "main", "change_id": "m" * 32})

    jj_client = cast(
        Any,
        SimpleNamespace(
            query_revisions_by_change_ids=lambda change_ids: {
                change_id: tracked_revisions[change_id] for change_id in change_ids
            },
            query_descendant_revisions=lambda commit_ids: (
                queried_descendants.append(tuple(commit_ids)) or (head, middle, root)
            ),
            query_revisions_by_commit_ids=lambda commit_ids: (
                queried_base_parents.append(tuple(commit_ids)) or (base_parent,)
            ),
        ),
    )
    state = ReviewState(
        changes={
            revision.change_id: CachedChange(last_submitted_commit_id=revision.commit_id)
            for revision in (root, middle, head)
        }
    )

    discovered = _discover_stacks(jj_client=jj_client, state=state)

    assert tuple(stack.head.commit_id for stack in discovered.stacks) == (head.commit_id,)
    assert queried_descendants == [(root.commit_id, middle.commit_id, head.commit_id)]
    assert queried_base_parents == [("main",)]
