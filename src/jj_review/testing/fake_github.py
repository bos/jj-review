"""Minimal fake GitHub server used for local integration tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, HTTPException


@dataclass(slots=True, frozen=True)
class FakeGithubRepository:
    """Repository metadata plus its backing bare Git repository."""

    default_branch: str
    git_dir: Path
    name: str
    owner: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    def to_payload(self, *, api_origin: str, web_origin: str) -> dict[str, object]:
        return {
            "clone_url": f"{web_origin}/{self.full_name}.git",
            "default_branch": self.default_branch,
            "full_name": self.full_name,
            "html_url": f"{web_origin}/{self.full_name}",
            "name": self.name,
            "private": True,
            "url": f"{api_origin}/repos/{self.full_name}",
        }


@dataclass(slots=True, frozen=True)
class FakeGithubState:
    """Static state served by the fake GitHub app."""

    repositories: dict[tuple[str, str], FakeGithubRepository]
    api_origin: str = "https://api.github.test"
    web_origin: str = "https://github.test"

    @classmethod
    def single_repository(cls, repository: FakeGithubRepository) -> FakeGithubState:
        return cls(repositories={(repository.owner, repository.name): repository})


def create_app(state: FakeGithubState) -> FastAPI:
    """Create a FastAPI app that serves the configured fake GitHub state."""

    app = FastAPI(docs_url=None, redoc_url=None, title="fake-github")

    @app.get("/repos/{owner}/{repo}")
    async def get_repository(owner: str, repo: str) -> dict[str, object]:
        repository = state.repositories.get((owner, repo))
        if repository is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return repository.to_payload(
            api_origin=state.api_origin,
            web_origin=state.web_origin,
        )

    return app


def initialize_bare_repository(
    root_dir: Path,
    *,
    owner: str,
    name: str,
    default_branch: str = "main",
) -> FakeGithubRepository:
    """Create a bare Git repository that the fake server can expose."""

    owner_dir = root_dir / owner
    owner_dir.mkdir(parents=True, exist_ok=True)
    git_dir = owner_dir / f"{name}.git"

    subprocess.run(
        ["git", "init", "--bare", str(git_dir)],
        capture_output=True,
        check=True,
        text=True,
    )
    subprocess.run(
        ["git", "symbolic-ref", "HEAD", f"refs/heads/{default_branch}"],
        capture_output=True,
        check=True,
        cwd=git_dir,
        text=True,
    )

    return FakeGithubRepository(
        default_branch=default_branch,
        git_dir=git_dir,
        name=name,
        owner=owner,
    )
