from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jj_review.commands.import_ import (
    _run_import_async,
)
from jj_review.config import RepoConfig
from jj_review.errors import CliError


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
            default_current_stack=True,
            selector="default current stack (@-)",
            head_bookmark=None,
            selected_revset="@-",
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
        "jj_review.commands.import_.stream_status_async",
        fail_stream_status_async,
    )

    with pytest.raises(CliError) as exc_info:
        asyncio.run(
            _run_import_async(
                config=RepoConfig(),
                fetch=False,
                pull_request_reference=None,
                repo_root=tmp_path,
                revset=None,
            )
        )

    assert "has no matching remote pull request" in str(exc_info.value)
