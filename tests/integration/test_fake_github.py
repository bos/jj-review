from __future__ import annotations

import asyncio
import subprocess
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


async def _round_trip_issue_comment(app: FastAPI) -> tuple[str, str]:
    transport = httpx.ASGITransport(app=app)
    async with GithubClient(base_url="https://api.github.test", transport=transport) as client:
        pull_request = await client.create_pull_request(
            "octo-org",
            "stacked-review",
            base="main",
            body="body",
            head="feature",
            title="feature",
        )
        created = await client.create_issue_comment(
            "octo-org",
            "stacked-review",
            issue_number=pull_request.number,
            body="first body",
        )
        listed = await client.list_issue_comments(
            "octo-org",
            "stacked-review",
            issue_number=pull_request.number,
        )
        updated = await client.update_issue_comment(
            "octo-org",
            "stacked-review",
            comment_id=created.id,
            body="updated body",
        )
    return listed[0].body, updated.body


async def _round_trip_pull_request_reviews(app: FastAPI) -> tuple[str, str]:
    transport = httpx.ASGITransport(app=app)
    async with GithubClient(base_url="https://api.github.test", transport=transport) as client:
        reviews = await client.list_pull_request_reviews(
            "octo-org",
            "stacked-review",
            pull_number=1,
        )
    if reviews[0].user is None:
        raise AssertionError("Review payload should include a user.")
    return reviews[0].user.login, reviews[1].state


async def _round_trip_draft_pull_request(app: FastAPI) -> tuple[bool, bool, bool]:
    transport = httpx.ASGITransport(app=app)
    async with GithubClient(base_url="https://api.github.test", transport=transport) as client:
        pull_request = await client.create_pull_request(
            "octo-org",
            "stacked-review",
            base="main",
            body="body",
            draft=True,
            head="feature",
            title="feature",
        )
        published = await client.mark_pull_request_ready_for_review(
            pull_request_id=pull_request.node_id or "",
        )
        redrafted = await client.convert_pull_request_to_draft(
            pull_request_id=pull_request.node_id or "",
        )
    return pull_request.is_draft, published.is_draft, redrafted.is_draft


async def _lookup_pull_requests_by_head_refs(app: FastAPI) -> tuple[int, int]:
    transport = httpx.ASGITransport(app=app)
    async with GithubClient(base_url="https://api.github.test", transport=transport) as client:
        pull_requests = await client.get_pull_requests_by_head_refs(
            "octo-org",
            "stacked-review",
            head_refs=("feature-1", "feature-2"),
        )
    return (
        pull_requests["feature-1"][0].number,
        pull_requests["feature-2"][0].number,
    )


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


def test_fake_github_issue_comments_round_trip_through_client(tmp_path: Path) -> None:
    fake_repo = initialize_bare_repository(
        tmp_path,
        owner="octo-org",
        name="stacked-review",
    )
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", str(worktree)], capture_output=True, check=True, text=True)
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "Test User"],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
        capture_output=True,
        check=True,
        text=True,
    )
    (worktree / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "README.md"],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "base"],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "remote", "add", "origin", str(fake_repo.git_dir)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "push", "origin", "HEAD:main"],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "push", "origin", "HEAD:feature"],
        capture_output=True,
        check=True,
        text=True,
    )
    app = create_app(FakeGithubState.single_repository(fake_repo))

    listed_body, updated_body = asyncio.run(_round_trip_issue_comment(app))

    assert listed_body == "first body"
    assert updated_body == "updated body"


def test_fake_github_pull_request_reviews_round_trip_through_client(tmp_path: Path) -> None:
    fake_repo = initialize_bare_repository(
        tmp_path,
        owner="octo-org",
        name="stacked-review",
    )
    fake_repo.create_pull_request(
        base_ref="main",
        body="body",
        head_ref="feature",
        title="feature",
    )
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="reviewer-1",
        state="APPROVED",
    )
    fake_repo.create_pull_request_review(
        pull_number=1,
        reviewer_login="reviewer-2",
        state="COMMENTED",
    )
    app = create_app(FakeGithubState.single_repository(fake_repo))

    first_reviewer, second_state = asyncio.run(_round_trip_pull_request_reviews(app))

    assert first_reviewer == "reviewer-1"
    assert second_state == "COMMENTED"


def test_fake_github_draft_pull_requests_round_trip_through_client(tmp_path: Path) -> None:
    fake_repo = initialize_bare_repository(
        tmp_path,
        owner="octo-org",
        name="stacked-review",
    )
    worktree = tmp_path / "worktree"
    subprocess.run(["git", "init", str(worktree)], capture_output=True, check=True, text=True)
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.name", "Test User"],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "config", "user.email", "test@example.com"],
        capture_output=True,
        check=True,
        text=True,
    )
    (worktree / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(worktree), "add", "README.md"],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "commit", "-m", "base"],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "remote", "add", "origin", str(fake_repo.git_dir)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "push", "origin", "HEAD:main"],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(worktree), "push", "origin", "HEAD:feature"],
        capture_output=True,
        check=True,
        text=True,
    )
    app = create_app(FakeGithubState.single_repository(fake_repo))

    created_is_draft, published_is_draft, redrafted_is_draft = asyncio.run(
        _round_trip_draft_pull_request(app)
    )

    assert created_is_draft is True
    assert published_is_draft is False
    assert redrafted_is_draft is True


def test_fake_github_graphql_head_lookup_round_trips_through_client(tmp_path: Path) -> None:
    fake_repo = initialize_bare_repository(
        tmp_path,
        owner="octo-org",
        name="stacked-review",
    )
    fake_repo.create_pull_request(
        base_ref="main",
        body="body 1",
        head_ref="feature-1",
        title="feature 1",
    )
    fake_repo.create_pull_request(
        base_ref="feature-1",
        body="body 2",
        head_ref="feature-2",
        title="feature 2",
    )
    app = create_app(FakeGithubState.single_repository(fake_repo))

    pull_request_numbers = asyncio.run(_lookup_pull_requests_by_head_refs(app))

    assert pull_request_numbers == (1, 2)
