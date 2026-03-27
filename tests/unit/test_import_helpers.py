from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
from typing import cast

import pytest

from jj_review.commands.import_ import (
    ImportResolutionError,
    _fetch_selected_stack_bookmarks,
    _prepared_status_has_discoverable_remote_link,
    _resolve_import_bookmark,
    _resolve_remote_head,
    _validate_bookmark_state,
    run_import,
)
from jj_review.commands.review_state import PreparedStatus
from jj_review.config import RepoConfig
from jj_review.jj import JjClient
from jj_review.models.bookmarks import BookmarkState, GitRemote, RemoteBookmarkState


@dataclass(frozen=True)
class _RevisionForImportTest:
    change_id: str


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

    assert "has no matching branch on the selected remote" in str(exc_info.value)


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
        == "Could not safely import the selected stack because saved branch "
        "'review/feature-aaaa' for aaaaaaaa is not present on the selected remote. "
        "Refresh with `status --fetch` or select an exact branch or pull request."
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
        == "Could not safely import the selected stack because saved branch "
        "'review/feature-aaaa' for aaaaaaaa points to a different revision on the "
        "selected remote. Refresh with `status --fetch` or repair the stale remote "
        "match before importing again."
    )


def test_prepared_status_has_discoverable_remote_link_from_remote_bookmark() -> None:
    assert _prepared_status_has_discoverable_remote_link(
        cast(
            PreparedStatus,
            SimpleNamespace(
                prepared=SimpleNamespace(
                    client=SimpleNamespace(
                        list_bookmark_states=lambda bookmarks: {
                            "review/feature-aaaa": BookmarkState(
                                name="review/feature-aaaa",
                                remote_targets=(
                                    RemoteBookmarkState(
                                        remote="origin",
                                        targets=("commit-1",),
                                    ),
                                ),
                            )
                        }
                    ),
                    remote=SimpleNamespace(name="origin"),
                    status_revisions=(SimpleNamespace(bookmark="review/feature-aaaa"),),
                )
            ),
        )
    )


def test_resolve_remote_head_requires_fetch_when_remote_bookmark_is_not_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_list_pull_requests_by_head(**kwargs):
        return ()

    monkeypatch.setattr(
        "jj_review.commands.import_.resolve_github_repository",
        lambda config, remote: SimpleNamespace(
            api_base_url="https://github.test/api/v3",
            full_name="octo-org/stacked-review",
            host="github.test",
            owner="octo-org",
            repo="stacked-review",
        ),
    )
    monkeypatch.setattr(
        "jj_review.commands.import_._list_pull_requests_by_head",
        fake_list_pull_requests_by_head,
    )

    with pytest.raises(ImportResolutionError) as exc_info:
        asyncio.run(
            _resolve_remote_head(
                client=cast(
                    JjClient,
                    SimpleNamespace(
                        list_git_remotes=lambda: (
                            SimpleNamespace(
                                name="origin",
                                url="https://example.test/repo.git",
                            ),
                        ),
                        get_bookmark_state=lambda bookmark: BookmarkState(name=bookmark),
                    ),
                ),
                config=RepoConfig(),
                fetch=False,
                head="review/feature-aaaaaaaa",
            )
        )

    assert "Re-run `import --fetch`" in str(exc_info.value)


def test_resolve_remote_head_fetches_selected_branch_when_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fetch_calls: list[tuple[str, tuple[str, ...] | None]] = []

    async def fake_list_pull_requests_by_head(**kwargs):
        return ()

    monkeypatch.setattr(
        "jj_review.commands.import_.resolve_github_repository",
        lambda config, remote: SimpleNamespace(
            api_base_url="https://github.test/api/v3",
            full_name="octo-org/stacked-review",
            host="github.test",
            owner="octo-org",
            repo="stacked-review",
        ),
    )
    monkeypatch.setattr(
        "jj_review.commands.import_._list_pull_requests_by_head",
        fake_list_pull_requests_by_head,
    )

    selection = asyncio.run(
        _resolve_remote_head(
            client=cast(
                JjClient,
                SimpleNamespace(
                    list_git_remotes=lambda: (
                        SimpleNamespace(name="origin", url="https://example.test/repo.git"),
                    ),
                    fetch_remote=lambda *, remote, branches=None: fetch_calls.append(
                        (remote, tuple(branches) if branches is not None else None)
                    ),
                    get_bookmark_state=lambda bookmark: BookmarkState(
                        name=bookmark,
                        remote_targets=(
                            RemoteBookmarkState(remote="origin", targets=("commit-2",)),
                        ),
                    ),
                ),
            ),
            config=RepoConfig(),
            fetch=True,
            head="review/feature-aaaaaaaa",
        )
    )

    assert fetch_calls == [("origin", ("review/feature-aaaaaaaa",))]
    assert selection.fetched_tip_commit == "commit-2"


def test_fetch_selected_stack_bookmarks_fetches_only_missing_exact_stack_branches() -> None:
    fetch_calls: list[tuple[str, tuple[str, ...] | None]] = []
    bookmark_state_calls = 0

    def list_bookmark_states(bookmarks=None):
        nonlocal bookmark_state_calls
        bookmark_state_calls += 1
        if bookmark_state_calls == 1:
            return {
                "review/custom-head": BookmarkState(
                    name="review/custom-head",
                    remote_targets=(
                        RemoteBookmarkState(remote="origin", targets=("commit-2",)),
                    ),
                ),
                "review/parent-bbbbbbbb": BookmarkState(name="review/parent-bbbbbbbb"),
            }
        return {}

    fetched = _fetch_selected_stack_bookmarks(
        client=cast(
            JjClient,
            SimpleNamespace(
                fetch_remote=lambda *, remote, branches=None: fetch_calls.append(
                    (remote, tuple(branches) if branches is not None else None)
                ),
                list_bookmark_states=list_bookmark_states,
                list_remote_branches=lambda *, remote, patterns: {
                    "review/custom-head": "commit-2",
                    "review/parent-bbbbbbbb": "commit-1",
                    "review/head-aaaaaaaa": "commit-2",
                },
            ),
        ),
        explicit_head_bookmark="review/custom-head",
        remote=GitRemote(name="origin", url="https://example.test/repo.git"),
        revisions=(
            _RevisionForImportTest(change_id="bbbbbbbbbbbbbbbb"),
            _RevisionForImportTest(change_id="aaaaaaaaaaaaaaaa"),
        ),
    )

    assert fetched == {
        "review/custom-head": "commit-2",
        "review/parent-bbbbbbbb": "commit-1",
    }
    assert fetch_calls == [("origin", ("review/parent-bbbbbbbb",))]


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
            config=RepoConfig(),
            current=True,
            fetch=False,
            head=None,
            pull_request_reference=None,
            repo_root=tmp_path,
            revset=None,
        )

    assert "has no matching remote pull request or branch" in str(exc_info.value)
