"""Minimal async GitHub API client."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from jj_review.models.github import (
    GithubIssueComment,
    GithubPullRequest,
    GithubPullRequestReview,
    GithubRepository,
)

logger = logging.getLogger(__name__)

_DEFAULT_RATE_LIMIT_RETRIES = 3
_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS = 1.0
_DEFAULT_MAX_RATE_LIMIT_BACKOFF_SECONDS = 8.0


class GithubClientError(RuntimeError):
    """Raised when GitHub returns a non-success response."""

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


class GithubClient:
    """Thin async wrapper around the GitHub REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        base_rate_limit_backoff_seconds: float = _DEFAULT_RATE_LIMIT_BACKOFF_SECONDS,
        max_rate_limit_backoff_seconds: float = _DEFAULT_MAX_RATE_LIMIT_BACKOFF_SECONDS,
        max_rate_limit_retries: int = _DEFAULT_RATE_LIMIT_RETRIES,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
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
        self._base_rate_limit_backoff_seconds = base_rate_limit_backoff_seconds
        self._max_rate_limit_backoff_seconds = max_rate_limit_backoff_seconds
        self._max_rate_limit_retries = max_rate_limit_retries
        self._sleep = sleep

    async def __aenter__(self) -> GithubClient:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_repository(self, owner: str, repo: str) -> GithubRepository:
        response = await self._request("GET", f"/repos/{owner}/{repo}")
        return GithubRepository.model_validate(self._expect_success(response))

    async def list_pull_requests(
        self,
        owner: str,
        repo: str,
        *,
        head: str,
        state: str = "all",
    ) -> tuple[GithubPullRequest, ...]:
        payload = await self._get_paginated_json_array(
            f"/repos/{owner}/{repo}/pulls",
            params={"head": head, "state": state},
            response_name="pull request list",
        )
        return tuple(GithubPullRequest.model_validate(item) for item in payload)

    async def get_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        pull_number: int,
    ) -> GithubPullRequest:
        response = await self._request(
            "GET",
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
        )
        return GithubPullRequest.model_validate(self._expect_success(response))

    async def create_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        base: str,
        body: str,
        head: str,
        title: str,
    ) -> GithubPullRequest:
        response = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/pulls",
            json={"base": base, "body": body, "head": head, "title": title},
        )
        return GithubPullRequest.model_validate(self._expect_success(response))

    async def list_pull_request_reviews(
        self,
        owner: str,
        repo: str,
        *,
        pull_number: int,
    ) -> tuple[GithubPullRequestReview, ...]:
        payload = await self._get_paginated_json_array(
            f"/repos/{owner}/{repo}/pulls/{pull_number}/reviews",
            response_name="pull request reviews",
        )
        return tuple(GithubPullRequestReview.model_validate(item) for item in payload)

    async def list_issue_comments(
        self,
        owner: str,
        repo: str,
        *,
        issue_number: int,
    ) -> tuple[GithubIssueComment, ...]:
        payload = await self._get_paginated_json_array(
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            response_name="issue comment list",
        )
        return tuple(GithubIssueComment.model_validate(item) for item in payload)

    async def create_issue_comment(
        self,
        owner: str,
        repo: str,
        *,
        issue_number: int,
        body: str,
    ) -> GithubIssueComment:
        response = await self._request(
            "POST",
            f"/repos/{owner}/{repo}/issues/{issue_number}/comments",
            json={"body": body},
        )
        return GithubIssueComment.model_validate(self._expect_success(response))

    async def update_issue_comment(
        self,
        owner: str,
        repo: str,
        *,
        comment_id: int,
        body: str,
    ) -> GithubIssueComment:
        response = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}",
            json={"body": body},
        )
        return GithubIssueComment.model_validate(self._expect_success(response))

    async def update_pull_request(
        self,
        owner: str,
        repo: str,
        *,
        pull_number: int,
        base: str,
        body: str,
        title: str,
    ) -> GithubPullRequest:
        response = await self._request(
            "PATCH",
            f"/repos/{owner}/{repo}/pulls/{pull_number}",
            json={"base": base, "body": body, "title": title},
        )
        return GithubPullRequest.model_validate(self._expect_success(response))

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        for attempt in range(self._max_rate_limit_retries + 1):
            try:
                response = await self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                )
            except httpx.RequestError as error:
                raise GithubClientError(f"GitHub request failed: {error}") from error

            retry_after_seconds = self._retry_after_seconds(
                attempt=attempt,
                response=response,
            )
            if retry_after_seconds is None:
                return response

            logger.debug(
                "github rate limit encountered: method=%s path=%s status=%s attempt=%d "
                "retry_after_seconds=%.3f",
                method,
                path,
                response.status_code,
                attempt + 1,
                retry_after_seconds,
            )
            await self._sleep(retry_after_seconds)

        raise AssertionError("Rate-limit retry loop did not return a response.")

    async def _get_paginated_json_array(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        response_name: str,
    ) -> tuple[object, ...]:
        items: list[object] = []
        next_path: str | None = path
        next_params = params

        while next_path is not None:
            response = await self._request(
                "GET",
                next_path,
                params=next_params,
            )
            payload = self._expect_success(response)
            if not isinstance(payload, list):
                raise GithubClientError(
                    f"GitHub {response_name} response was not a JSON array."
                )
            items.extend(payload)
            next_path = response.links.get("next", {}).get("url")
            next_params = None

        return tuple(items)

    def _expect_success(self, response: httpx.Response) -> Any:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            raise GithubClientError(
                f"GitHub request failed: {error.response.status_code} {error.response.text}",
                retry_after_seconds=_parse_retry_after_header(
                    error.response.headers.get("Retry-After")
                ),
                status_code=error.response.status_code,
            ) from error
        return response.json()

    def _retry_after_seconds(
        self,
        *,
        attempt: int,
        response: httpx.Response,
    ) -> float | None:
        if not _is_retryable_rate_limit(response):
            return None
        if attempt >= self._max_rate_limit_retries:
            return None

        retry_after_seconds = _parse_retry_after_header(response.headers.get("Retry-After"))
        if retry_after_seconds is not None:
            return retry_after_seconds

        reset_after_seconds = _seconds_until_rate_limit_reset(
            response.headers.get("X-RateLimit-Reset")
        )
        if reset_after_seconds is not None:
            return reset_after_seconds

        backoff_seconds = self._base_rate_limit_backoff_seconds * (2**attempt)
        return min(backoff_seconds, self._max_rate_limit_backoff_seconds)


def _is_retryable_rate_limit(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    if "Retry-After" in response.headers or "X-RateLimit-Reset" in response.headers:
        return True
    if response.headers.get("X-RateLimit-Remaining") == "0":
        return True
    return "rate limit" in response.text.lower()


def _parse_retry_after_header(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        retry_after_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return max(retry_after_at.timestamp() - time.time(), 0.0)


def _seconds_until_rate_limit_reset(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return max(float(value) - time.time(), 0.0)
    except ValueError:
        return None
