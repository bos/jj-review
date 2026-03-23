from __future__ import annotations

from types import SimpleNamespace

import pytest

from jj_review.commands.import_ import (
    ImportResolutionError,
    _resolve_import_bookmark,
    _validate_bookmark_state,
)
from jj_review.models.bookmarks import BookmarkState, RemoteBookmarkState


def test_validate_bookmark_state_ignores_other_remote_conflicts() -> None:
    _validate_bookmark_state(
        bookmark="review/feature-aaaaaaaa",
        bookmark_state=BookmarkState(
            name="review/feature-aaaaaaaa",
            remote_targets=(
                RemoteBookmarkState(remote="origin", targets=("commit-1",)),
                RemoteBookmarkState(remote="backup", targets=("other-commit",)),
            ),
        ),
        desired_commit_id="commit-1",
        selected_remote_name="origin",
    )


def test_validate_bookmark_state_rejects_selected_remote_conflicts() -> None:
    with pytest.raises(ImportResolutionError) as exc_info:
        _validate_bookmark_state(
            bookmark="review/feature-aaaaaaaa",
            bookmark_state=BookmarkState(
                name="review/feature-aaaaaaaa",
                remote_targets=(
                    RemoteBookmarkState(remote="origin", targets=("other-commit",)),
                    RemoteBookmarkState(remote="backup", targets=("commit-1",)),
                ),
            ),
            desired_commit_id="commit-1",
            selected_remote_name="origin",
        )

    assert (
        str(exc_info.value)
        == "Remote bookmark 'review/feature-aaaaaaaa'@origin already points to a "
        "different revision. Import will not overwrite a stale remote identity."
    )


def test_resolve_import_bookmark_rejects_generated_bookmark_without_selected_remote(
    ) -> None:
    with pytest.raises(ImportResolutionError) as exc_info:
        _resolve_import_bookmark(
            bookmark_by_change_id={},
            bookmark_states={},
            prepared_revision=SimpleNamespace(
                bookmark="review/feature-aaaaaaaa",
                bookmark_source="generated",
                revision=SimpleNamespace(
                    change_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    commit_id="commit-1",
                ),
            ),
            selected_remote_name=None,
        )

    assert (
        "has no discoverable review bookmark on the selected remote"
        in str(exc_info.value)
    )
