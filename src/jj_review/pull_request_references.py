"""Helpers for parsing pull request numbers and URLs."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

_PULL_REQUEST_URL_RE = re.compile(
    r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pull/(?P<number>[0-9]+)/?$"
)


@dataclass(frozen=True, slots=True)
class ParsedPullRequestUrl:
    host: str
    number: int
    owner: str
    repo: str


def parse_pull_request_number(reference: str) -> int | None:
    if reference.isdigit():
        return int(reference)
    return None


def parse_pull_request_url(reference: str) -> ParsedPullRequestUrl | None:
    parsed = urlparse(reference)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    match = _PULL_REQUEST_URL_RE.fullmatch(parsed.path)
    if match is None:
        return None
    return ParsedPullRequestUrl(
        host=parsed.hostname,
        number=int(match.group("number")),
        owner=match.group("owner"),
        repo=match.group("repo"),
    )
