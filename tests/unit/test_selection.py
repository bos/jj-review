from pathlib import Path
from typing import cast

import pytest

from jj_review.errors import CliError
from jj_review.jj import JjClient
from jj_review.models.bookmarks import GitRemote
from jj_review.models.review_state import CachedChange, ReviewState
from jj_review.review.selection import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)


def test_resolve_selected_revset_returns_explicit_value() -> None:
    assert (
        resolve_selected_revset(
            command_label="submit",
            default_revset="@-",
            require_explicit=False,
            revset="@",
        )
        == "@"
    )


def test_resolve_selected_revset_requires_explicit_selection() -> None:
    with pytest.raises(CliError, match="requires an explicit revision selection"):
        resolve_selected_revset(
            command_label="relink",
            require_explicit=True,
            revset=None,
        )


def test_resolve_linked_change_for_pull_request_uses_action_specific_guidance(
    monkeypatch,
) -> None:
    _patch_review_state(
        monkeypatch,
        ReviewState(changes={"change-1": CachedChange(pr_number=17)}),
    )
    jj_client = _JjClientStub(_REPO_ROOT, revisions_by_change_id={"change-1": ()})

    with pytest.raises(CliError, match="Close by revision once it is visible again."):
        resolve_linked_change_for_pull_request(
            action_name="close",
            jj_client=cast(JjClient, jj_client),
            pull_request_reference="17",
            revset=None,
        )


_REPO_ROOT = Path(__file__).resolve().parent


class _StateStoreStub:
    def __init__(self, state: ReviewState) -> None:
        self._state = state

    def load(self) -> ReviewState:
        return self._state


class _JjClientStub:
    def __init__(
        self,
        repo_root,
        *,
        remotes: tuple[GitRemote, ...] = (),
        revisions_by_change_id: dict[str, tuple[object, ...]] | None = None,
    ) -> None:
        self.repo_root = repo_root
        self._remotes = remotes
        self._revisions_by_change_id = revisions_by_change_id or {}

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        return self._remotes

    def query_revisions_by_change_ids(self, change_ids):
        return {
            change_id: self._revisions_by_change_id.get(change_id, ())
            for change_id in change_ids
        }


def _patch_review_state(monkeypatch, state: ReviewState) -> None:
    monkeypatch.setattr(
        "jj_review.review.selection.ReviewStateStore.for_repo",
        lambda repo_root: _StateStoreStub(state),
    )
