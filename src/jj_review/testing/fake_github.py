"""Minimal fake GitHub server used for local integration tests."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from fastapi import Body, FastAPI, HTTPException


@dataclass(slots=True)
class FakeGithubPullRequest:
    """Mutable pull request state served by the fake API."""

    base_ref: str
    body: str
    head_label: str
    head_ref: str
    merged_at: str | None
    number: int
    title: str
    state: str = "open"

    def to_payload(
        self,
        *,
        repository: FakeGithubRepository,
        web_origin: str,
    ) -> dict[str, object]:
        return {
            "base": {"label": f"{repository.full_name}:{self.base_ref}", "ref": self.base_ref},
            "body": self.body,
            "head": {"label": self.head_label, "ref": self.head_ref},
            "html_url": f"{web_origin}/{repository.full_name}/pull/{self.number}",
            "merged_at": self.merged_at,
            "number": self.number,
            "state": self.state,
            "title": self.title,
        }


@dataclass(slots=True)
class FakeGithubPullRequestReview:
    """Mutable pull request review state served by the fake API."""

    id: int
    pull_request_number: int
    reviewer_login: str
    state: str

    def to_payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "state": self.state,
            "user": {"login": self.reviewer_login},
        }


@dataclass(slots=True)
class FakeGithubIssueComment:
    """Mutable issue comment state served by the fake API."""

    body: str
    id: int
    issue_number: int

    def to_payload(
        self,
        *,
        repository: FakeGithubRepository,
        web_origin: str,
    ) -> dict[str, object]:
        return {
            "body": self.body,
            "html_url": (
                f"{web_origin}/{repository.full_name}/issues/{self.issue_number}"
                f"#issuecomment-{self.id}"
            ),
            "id": self.id,
        }


@dataclass(slots=True)
class FakeGithubRepository:
    """Repository metadata plus its backing bare Git repository."""

    default_branch: str
    git_dir: Path
    name: str
    owner: str
    next_issue_comment_id: int = 1
    next_pull_request_number: int = 1
    next_pull_request_review_id: int = 1
    issue_comments: dict[int, list[FakeGithubIssueComment]] = field(default_factory=dict)
    pull_requests: dict[int, FakeGithubPullRequest] = field(default_factory=dict)
    pull_request_reviews: dict[int, list[FakeGithubPullRequestReview]] = field(
        default_factory=dict
    )

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

    def create_pull_request(
        self,
        *,
        base_ref: str,
        body: str,
        head_ref: str,
        title: str,
    ) -> FakeGithubPullRequest:
        number = self.next_pull_request_number
        self.next_pull_request_number += 1
        pull_request = FakeGithubPullRequest(
            base_ref=base_ref,
            body=body,
            head_label=f"{self.owner}:{head_ref}",
            head_ref=head_ref,
            merged_at=None,
            number=number,
            title=title,
        )
        self.pull_requests[number] = pull_request
        return pull_request

    def list_pull_request_reviews(self, pull_number: int) -> list[FakeGithubPullRequestReview]:
        self._require_issue_number(pull_number)
        return list(self.pull_request_reviews.get(pull_number, ()))

    def create_pull_request_review(
        self,
        *,
        pull_number: int,
        reviewer_login: str,
        state: str,
    ) -> FakeGithubPullRequestReview:
        self._require_issue_number(pull_number)
        review = FakeGithubPullRequestReview(
            id=self.next_pull_request_review_id,
            pull_request_number=pull_number,
            reviewer_login=reviewer_login,
            state=state,
        )
        self.next_pull_request_review_id += 1
        self.pull_request_reviews.setdefault(pull_number, []).append(review)
        return review

    def list_issue_comments(self, issue_number: int) -> list[FakeGithubIssueComment]:
        self._require_issue_number(issue_number)
        return list(self.issue_comments.get(issue_number, ()))

    def create_issue_comment(
        self,
        *,
        body: str,
        issue_number: int,
    ) -> FakeGithubIssueComment:
        self._require_issue_number(issue_number)
        comment = FakeGithubIssueComment(
            body=body,
            id=self.next_issue_comment_id,
            issue_number=issue_number,
        )
        self.next_issue_comment_id += 1
        self.issue_comments.setdefault(issue_number, []).append(comment)
        return comment

    def update_issue_comment(
        self,
        *,
        body: str,
        comment_id: int,
    ) -> FakeGithubIssueComment | None:
        for comments in self.issue_comments.values():
            for comment in comments:
                if comment.id == comment_id:
                    comment.body = body
                    return comment
        return None

    def _require_issue_number(self, issue_number: int) -> None:
        if issue_number not in self.pull_requests:
            raise HTTPException(status_code=404, detail="Not Found")


@dataclass(slots=True, frozen=True)
class FakeGithubState:
    """Static state served by the fake GitHub app."""

    repositories: dict[tuple[str, str], FakeGithubRepository]
    api_origin: str = "https://api.github.test"
    web_origin: str = "https://github.test"

    @classmethod
    def single_repository(cls, repository: FakeGithubRepository) -> FakeGithubState:
        return cls(repositories={(repository.owner, repository.name): repository})


def create_app(fake_state: FakeGithubState) -> FastAPI:
    """Create a FastAPI app that serves the configured fake GitHub state."""

    app = FastAPI(docs_url=None, redoc_url=None, title="fake-github")

    @app.get("/repos/{owner}/{repo}")
    async def get_repository(owner: str, repo: str) -> dict[str, object]:
        repository = fake_state.repositories.get((owner, repo))
        if repository is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return repository.to_payload(
            api_origin=fake_state.api_origin,
            web_origin=fake_state.web_origin,
        )

    @app.get("/repos/{owner}/{repo}/pulls")
    async def list_pull_requests(
        owner: str,
        repo: str,
        head: str | None = None,
        state: str = "open",
    ) -> list[dict[str, object]]:
        repository = _get_repository(fake_state, owner, repo)
        requested_state = state or "open"
        pull_requests = list(repository.pull_requests.values())
        if head is not None:
            pull_requests = [
                candidate for candidate in pull_requests if candidate.head_label == head
            ]
        if requested_state != "all":
            pull_requests = [
                candidate
                for candidate in pull_requests
                if candidate.state == requested_state
            ]
        return [
            pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)
            for pull_request in sorted(pull_requests, key=lambda candidate: candidate.number)
        ]

    @app.post("/repos/{owner}/{repo}/pulls", status_code=201)
    async def create_pull_request(
        owner: str,
        repo: str,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        title = _require_string(payload, "title")
        head_ref = _require_string(payload, "head")
        base_ref = _require_string(payload, "base")
        body = _optional_string(payload, "body") or ""
        _require_branch(repository, head_ref)
        _require_branch(repository, base_ref)
        pull_request = repository.create_pull_request(
            base_ref=base_ref,
            body=body,
            head_ref=head_ref,
            title=title,
        )
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.get("/repos/{owner}/{repo}/pulls/{pull_number}")
    async def get_pull_request(
        owner: str,
        repo: str,
        pull_number: int,
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        pull_request = repository.pull_requests.get(pull_number)
        if pull_request is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.patch("/repos/{owner}/{repo}/pulls/{pull_number}")
    async def update_pull_request(
        owner: str,
        repo: str,
        pull_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        pull_request = repository.pull_requests.get(pull_number)
        if pull_request is None:
            raise HTTPException(status_code=404, detail="Not Found")
        if "title" in payload:
            pull_request.title = _require_string(payload, "title")
        if "body" in payload:
            pull_request.body = _optional_string(payload, "body") or ""
        if "base" in payload:
            pull_request.base_ref = _require_string(payload, "base")
            _require_branch(repository, pull_request.base_ref)
        return pull_request.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.get("/repos/{owner}/{repo}/pulls/{pull_number}/reviews")
    async def list_pull_request_reviews(
        owner: str,
        repo: str,
        pull_number: int,
    ) -> list[dict[str, object]]:
        repository = _get_repository(fake_state, owner, repo)
        reviews = repository.list_pull_request_reviews(pull_number)
        return [
            review.to_payload()
            for review in sorted(reviews, key=lambda candidate: candidate.id)
        ]

    @app.get("/repos/{owner}/{repo}/issues/{issue_number}/comments")
    async def list_issue_comments(
        owner: str,
        repo: str,
        issue_number: int,
    ) -> list[dict[str, object]]:
        repository = _get_repository(fake_state, owner, repo)
        comments = repository.list_issue_comments(issue_number)
        return [
            comment.to_payload(repository=repository, web_origin=fake_state.web_origin)
            for comment in sorted(comments, key=lambda candidate: candidate.id)
        ]

    @app.post("/repos/{owner}/{repo}/issues/{issue_number}/comments", status_code=201)
    async def create_issue_comment(
        owner: str,
        repo: str,
        issue_number: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        comment = repository.create_issue_comment(
            body=_require_string(payload, "body"),
            issue_number=issue_number,
        )
        return comment.to_payload(repository=repository, web_origin=fake_state.web_origin)

    @app.patch("/repos/{owner}/{repo}/issues/comments/{comment_id}")
    async def update_issue_comment(
        owner: str,
        repo: str,
        comment_id: int,
        payload: Annotated[dict[str, object], Body(...)],
    ) -> dict[str, object]:
        repository = _get_repository(fake_state, owner, repo)
        comment = repository.update_issue_comment(
            body=_require_string(payload, "body"),
            comment_id=comment_id,
        )
        if comment is None:
            raise HTTPException(status_code=404, detail="Not Found")
        return comment.to_payload(repository=repository, web_origin=fake_state.web_origin)

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


def _get_repository(state: FakeGithubState, owner: str, repo: str) -> FakeGithubRepository:
    repository = state.repositories.get((owner, repo))
    if repository is None:
        raise HTTPException(status_code=404, detail="Not Found")
    return repository


def _optional_string(payload: dict[str, object], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise HTTPException(status_code=422, detail=f"Expected {key!r} to be a string.")


def _require_branch(repository: FakeGithubRepository, branch: str) -> None:
    completed = subprocess.run(
        [
            "git",
            "--git-dir",
            str(repository.git_dir),
            "show-ref",
            "--verify",
            f"refs/heads/{branch}",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if completed.returncode == 0:
        return
    raise HTTPException(status_code=422, detail=f"Branch {branch!r} does not exist.")


def _require_string(payload: dict[str, object], key: str) -> str:
    value = _optional_string(payload, key)
    if value is None:
        raise HTTPException(status_code=422, detail=f"Missing required field {key!r}.")
    return value
