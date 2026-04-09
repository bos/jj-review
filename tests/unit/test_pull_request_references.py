from __future__ import annotations

from jj_review.pull_request_references import (
    ParsedPullRequestUrl,
    parse_pull_request_number,
    parse_pull_request_url,
)


def test_parse_pull_request_number_accepts_digits() -> None:
    assert parse_pull_request_number("42") == 42


def test_parse_pull_request_number_rejects_non_digits() -> None:
    assert parse_pull_request_number("pr-42") is None


def test_parse_pull_request_url_accepts_standard_pull_request_url() -> None:
    assert parse_pull_request_url("https://github.test/octo-org/stacked-review/pull/17") == (
        ParsedPullRequestUrl(
            host="github.test",
            number=17,
            owner="octo-org",
            repo="stacked-review",
        )
    )


def test_parse_pull_request_url_rejects_non_pull_request_urls() -> None:
    assert parse_pull_request_url("https://github.test/octo-org/stacked-review/issues/17") is None
