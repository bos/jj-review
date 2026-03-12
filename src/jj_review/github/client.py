"""Minimal async GitHub API client."""

from __future__ import annotations

import httpx

from jj_review.models.github import GithubRepository


class GithubClientError(RuntimeError):
    """Raised when GitHub returns a non-success response."""


class GithubClient:
    """Thin async wrapper around the GitHub REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "jj-review/dev",
        }
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"

        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=30.0,
            transport=transport,
        )

    async def __aenter__(self) -> GithubClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_repository(self, owner: str, repo: str) -> GithubRepository:
        response = await self._client.get(f"/repos/{owner}/{repo}")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise GithubClientError(
                f"GitHub request failed: {error.response.status_code} {error.response.text}"
            ) from error

        return GithubRepository.model_validate(response.json())
