from pathlib import Path

import pytest

from jj_review.command_ui import (
    resolve_linked_change_for_pull_request,
    resolve_selected_revset,
)
from jj_review.errors import CliError
from jj_review.models.bookmarks import GitRemote
from jj_review.models.cache import CachedChange, ReviewState


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


def test_resolve_selected_revset_uses_default_when_omitted() -> None:
    assert (
        resolve_selected_revset(
            command_label="submit",
            default_revset="@-",
            require_explicit=False,
            revset=None,
        )
        == "@-"
    )


def test_resolve_selected_revset_requires_explicit_selection() -> None:
    with pytest.raises(CliError, match="requires an explicit revision selection"):
        resolve_selected_revset(
            command_label="relink",
            require_explicit=True,
            revset=None,
        )


def test_resolve_linked_change_for_pull_request_accepts_numeric_selector_without_remote_lookup(
    monkeypatch,
) -> None:
    _patch_review_state(
        monkeypatch,
        ReviewState(changes={"change-1": CachedChange(pr_number=17)}),
    )
    _patch_jj_client(
        monkeypatch,
        revisions_by_change_id={"change-1": (_revision("change-1"),)},
    )
    monkeypatch.setattr(
        "jj_review.command_ui.select_submit_remote",
        lambda remotes: (_ for _ in ()).throw(AssertionError("remote lookup should not run")),
    )

    assert resolve_linked_change_for_pull_request(
        action_name="land",
        pull_request_reference="17",
        repo_root=_REPO_ROOT,
        revset=None,
    ) == (17, "change-1")


def test_resolve_linked_change_for_pull_request_accepts_repository_url_selector(
    monkeypatch,
) -> None:
    _patch_review_state(
        monkeypatch,
        ReviewState(changes={"change-1": CachedChange(pr_number=17)}),
    )
    _patch_jj_client(
        monkeypatch,
        remotes=(GitRemote(name="origin", url="https://github.test/octo-org/stacked-review"),),
        revisions_by_change_id={"change-1": (_revision("change-1"),)},
    )

    assert resolve_linked_change_for_pull_request(
        action_name="close",
        pull_request_reference="https://github.test/octo-org/stacked-review/pull/17",
        repo_root=_REPO_ROOT,
        revset=None,
    ) == (17, "change-1")


def test_resolve_linked_change_for_pull_request_uses_action_specific_guidance(
    monkeypatch,
) -> None:
    _patch_review_state(
        monkeypatch,
        ReviewState(changes={"change-1": CachedChange(pr_number=17)}),
    )
    _patch_jj_client(monkeypatch, revisions_by_change_id={"change-1": ()})

    with pytest.raises(CliError, match="Close by revision once it is visible again."):
        resolve_linked_change_for_pull_request(
            action_name="close",
            pull_request_reference="17",
            repo_root=_REPO_ROOT,
            revset=None,
        )
_REPO_ROOT = Path(__file__).resolve().parent


class _StateStoreStub:
    def __init__(self, state: ReviewState) -> None:
        self._state = state

    def load(self) -> ReviewState:
        return self._state


class _JjClientStub:
    remotes: tuple[GitRemote, ...] = ()
    revisions_by_change_id: dict[str, tuple[object, ...]] = {}

    def __init__(self, repo_root) -> None:
        self.repo_root = repo_root

    def list_git_remotes(self) -> tuple[GitRemote, ...]:
        return type(self).remotes

    def query_revisions_by_change_ids(self, change_ids):
        return {
            change_id: type(self).revisions_by_change_id.get(change_id, ())
            for change_id in change_ids
        }


def _patch_review_state(monkeypatch, state: ReviewState) -> None:
    monkeypatch.setattr(
        "jj_review.command_ui.ReviewStateStore.for_repo",
        lambda repo_root: _StateStoreStub(state),
    )


def _patch_jj_client(
    monkeypatch,
    *,
    remotes: tuple[GitRemote, ...] = (),
    revisions_by_change_id: dict[str, tuple[object, ...]],
) -> None:
    _JjClientStub.remotes = remotes
    _JjClientStub.revisions_by_change_id = revisions_by_change_id
    monkeypatch.setattr("jj_review.command_ui.JjClient", _JjClientStub)


def _revision(change_id: str) -> object:
    return type("Revision", (), {"change_id": change_id})()
