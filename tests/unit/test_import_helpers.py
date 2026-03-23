from __future__ import annotations

from types import SimpleNamespace

import pytest

from jj_review.commands.import_ import (
    ImportResolutionError,
    _prepared_status_has_discoverable_remote_linkage,
    _resolve_import_bookmark,
    run_import,
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


def test_resolve_import_bookmark_rejects_missing_cached_remote_bookmark() -> None:
    with pytest.raises(ImportResolutionError) as exc_info:
        _resolve_import_bookmark(
            bookmark_by_change_id={"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "review/feature-aaaa"},
            bookmark_states={
                "review/feature-aaaa": BookmarkState(name="review/feature-aaaa")
            },
            prepared_revision=SimpleNamespace(
                bookmark="review/feature-aaaa",
                bookmark_source="cached",
                revision=SimpleNamespace(
                    change_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    commit_id="commit-1",
                ),
            ),
            selected_remote_name="origin",
        )

    assert (
        str(exc_info.value)
        == "Could not safely import the selected stack because cached review bookmark "
        "'review/feature-aaaa' for aaaaaaaa is not present on the selected remote. "
        "Refresh with `status --fetch` or select an exact review branch or pull "
        "request."
    )


def test_resolve_import_bookmark_rejects_stale_cached_remote_bookmark_target() -> None:
    with pytest.raises(ImportResolutionError) as exc_info:
        _resolve_import_bookmark(
            bookmark_by_change_id={"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa": "review/feature-aaaa"},
            bookmark_states={
                "review/feature-aaaa": BookmarkState(
                    name="review/feature-aaaa",
                    remote_targets=(
                        RemoteBookmarkState(remote="origin", targets=("other-commit",)),
                    ),
                )
            },
            prepared_revision=SimpleNamespace(
                bookmark="review/feature-aaaa",
                bookmark_source="cached",
                revision=SimpleNamespace(
                    change_id="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    commit_id="commit-1",
                ),
            ),
            selected_remote_name="origin",
        )

    assert (
        str(exc_info.value)
        == "Could not safely import the selected stack because cached review bookmark "
        "'review/feature-aaaa' for aaaaaaaa points to a different revision on the "
        "selected remote. Refresh with `status --fetch` or repair the stale remote "
        "linkage before importing again."
    )


def test_prepared_status_has_discoverable_remote_linkage_from_remote_bookmark() -> None:
    assert _prepared_status_has_discoverable_remote_linkage(
        SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(
                    list_bookmark_states=lambda bookmarks: {
                        "review/feature-aaaa": BookmarkState(
                            name="review/feature-aaaa",
                            remote_targets=(
                                RemoteBookmarkState(remote="origin", targets=("commit-1",)),
                            ),
                        )
                    }
                ),
                remote=SimpleNamespace(name="origin"),
                status_revisions=(SimpleNamespace(bookmark="review/feature-aaaa"),),
            )
        )
    )


def test_run_import_current_rejects_before_github_inspection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        "jj_review.commands.import_.JjClient",
        lambda repo_root: object(),
    )
    async def fake_resolve_selection(**kwargs):
        return SimpleNamespace(
            selector="--current",
            head_bookmark=None,
            selected_revset=None,
        )

    monkeypatch.setattr("jj_review.commands.import_._resolve_selection", fake_resolve_selection)
    monkeypatch.setattr(
        "jj_review.commands.import_.prepare_status",
        lambda **kwargs: SimpleNamespace(
            prepared=SimpleNamespace(
                client=SimpleNamespace(list_bookmark_states=lambda bookmarks: {}),
                remote=SimpleNamespace(name="origin"),
                remote_error=None,
                stack=SimpleNamespace(revisions=()),
                state_store=SimpleNamespace(load=lambda: SimpleNamespace(changes={})),
                status_revisions=(SimpleNamespace(bookmark="review/feature-aaaa"),),
            ),
            github_repository=SimpleNamespace(full_name="octo-org/stacked-review"),
            selected_revset="@",
        ),
    )

    async def fail_stream_status_async(**kwargs):
        raise AssertionError("GitHub inspection should not run for this failure path.")

    monkeypatch.setattr(
        "jj_review.commands.import_._stream_status_async",
        fail_stream_status_async,
    )

    with pytest.raises(ImportResolutionError) as exc_info:
        run_import(
            change_overrides={},
            config=SimpleNamespace(),
            current=True,
            head=None,
            pull_request_reference=None,
            repo_root=tmp_path,
            revset=None,
        )

    assert "has no discoverable remote review linkage" in str(exc_info.value)
