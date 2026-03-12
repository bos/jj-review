from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import FastAPI

from jj_review.github.client import GithubClient
from jj_review.testing.fake_github import (
    FakeGithubState,
    create_app,
    initialize_bare_repository,
)


async def _fetch_repository(app: FastAPI) -> tuple[str, str]:
    transport = httpx.ASGITransport(app=app)
    async with GithubClient(base_url="https://api.github.test", transport=transport) as client:
        repository = await client.get_repository("octo-org", "stacked-review")
    return repository.full_name, repository.default_branch


def test_fake_github_repository_endpoint_round_trips_through_client(tmp_path: Path) -> None:
    fake_repo = initialize_bare_repository(
        tmp_path,
        owner="octo-org",
        name="stacked-review",
    )
    state = FakeGithubState.single_repository(fake_repo)
    app = create_app(state)

    full_name, default_branch = asyncio.run(_fetch_repository(app))

    assert full_name == "octo-org/stacked-review"
    assert default_branch == "main"
    assert fake_repo.git_dir.is_dir()
    head_ref = (fake_repo.git_dir / "HEAD").read_text(encoding="utf-8").strip()
    assert head_ref == "ref: refs/heads/main"
