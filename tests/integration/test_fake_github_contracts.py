"""Contract checks for fake GitHub recovery and merge behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2
import pytest

from ..support.fake_github import FakeGithubState, create_app
from ..support.integration_helpers import (
    delete_remote_ref,
    init_fake_github_repo_with_submitted_feature,
    init_fake_github_repo_with_submitted_stack,
    run_command,
    update_remote_ref,
)


@pytest.mark.merge_recovery
def test_fake_rejects_retargets_and_reopens_that_github_cannot_apply(tmp_path: Path) -> None:
    _repo, fake = init_fake_github_repo_with_submitted_feature(tmp_path)
    pr = fake.prs[1]
    head = pr.head_sha
    base = fake.ref_target("main")
    assert base is not None
    update_remote_ref(fake, branch="landed", target=head)
    app = create_app(FakeGithubState.single_repo(fake))
    path = f"/repos/{fake.full_name}"

    async def exercise() -> None:
        async with httpx2.AsyncClient(
            base_url="https://api.github.test", transport=httpx2.ASGITransport(app=app)
        ) as client:
            response = await client.patch(f"{path}/pulls/1", json={"base": "landed"})
            assert response.status_code == 422
            assert (pr.base_ref, pr.state, pr.merged_at) == ("main", "open", None)

            assert (await client.patch(f"{path}/issues/1", json={"state": "closed"})).is_success
            response = await client.patch(f"{path}/pulls/1", json={"base": "landed"})
            assert response.status_code == 422
            assert pr.base_ref == "main"

            for branch, commit in ((pr.head_ref, head), ("main", base)):
                delete_remote_ref(fake, branch=branch)
                response = await client.patch(f"{path}/issues/1", json={"state": "open"})
                assert response.status_code == 422
                payload = (await client.get(f"{path}/pulls/1")).json()
                assert payload["state"] == "closed"
                assert payload["head"]["sha"] == head
                update_remote_ref(fake, branch=branch, target=commit)

            assert (await client.patch(f"{path}/issues/1", json={"state": "open"})).is_success
            update_remote_ref(fake, branch="main", target=head)
            payload = (await client.get(f"{path}/pulls/1")).json()
            assert payload["merged_at"] is not None
            assert payload["merge_commit_sha"] == head
            response = await client.patch(f"{path}/issues/1", json={"state": "open"})
            assert response.status_code == 422

    asyncio.run(exercise())


@pytest.mark.merge_recovery
def test_fake_partial_stack_merge_preserves_base_changes_in_merge_and_survivor(
    tmp_path: Path,
) -> None:
    repo, fake = init_fake_github_repo_with_submitted_stack(tmp_path, size=2)
    trunk = fake.ref_target("main")
    assert trunk is not None
    update_remote_ref(fake, branch="integration", target=trunk)
    fake.update_pr_base(fake.prs[1], base_ref="integration")
    fake.advance_branch("integration", path="upstream.txt", contents="before\n")
    advanced = fake.advance_branch("integration", path="upstream.txt", contents="upstream\n")
    bottom, top = fake.prs[1], fake.prs[2]
    submitted_top = top.head_sha
    app = create_app(FakeGithubState.single_repo(fake))
    path = f"/repos/{fake.full_name}"

    async def exercise() -> None:
        async with httpx2.AsyncClient(
            base_url="https://api.github.test", transport=httpx2.ASGITransport(app=app)
        ) as client:
            response = await client.put(
                f"{path}/pulls/1/merge-async",
                json={
                    "sha": bottom.head_sha,
                    "merge_action": "direct_merge",
                    "merge_method": "squash",
                },
            )
            assert response.status_code == 202
            uuid = response.json()["details"]["uuid"]
            payload = (await client.get(f"{path}/pulls/1/merge-async/{uuid}")).json()
            assert payload["status"] == "merged"
            assert payload["details"]["sha"] == fake.ref_target("integration")

    asyncio.run(exercise())
    assert fake.ref_target("main") == trunk
    assert bottom.base_ref == top.base_ref == "integration"
    assert bottom.merged_at is not None
    assert top.state == "open" and top.merged_at is None
    assert top.head_sha != submitted_top
    assert bottom.merge_commit_sha is not None
    assert fake.is_ancestor(advanced, bottom.merge_commit_sha)
    assert fake.is_ancestor(bottom.merge_commit_sha, top.head_sha)
    for commit in (bottom.merge_commit_sha, top.head_sha):
        assert (
            run_command(
                ["git", "--git-dir", str(fake.git_dir), "show", f"{commit}:upstream.txt"], repo
            ).stdout
            == "upstream\n"
        )
