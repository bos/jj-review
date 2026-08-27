from __future__ import annotations

import asyncio
import json

import httpxyz
import pytest

from jj_stack.github.client import GithubClient, GithubClientError
from jj_stack.github.resolution import GithubRepoAddress


def _github_client(handler, *, client_type=GithubClient) -> GithubClient:
    return client_type(
        httpxyz.AsyncClient(
            base_url="https://api.github.test",
            transport=httpxyz.MockTransport(handler),
        ),
        repo=GithubRepoAddress(
            owner="octo-org",
            repo="stacked-prs",
        ),
    )


def test_github_client_retries_429_responses_with_retry_after() -> None:
    attempts = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpxyz.Response(
                429,
                headers={"Retry-After": "0"},
                json={"message": "slow down"},
                request=request,
            )
        return httpxyz.Response(
            200,
            json={
                "default_branch": "main",
                "full_name": "octo-org/stacked-prs",
            },
            request=request,
        )

    async def run_test() -> str:
        async with _github_client(handler) as client:
            repo = await client.get_repo()
        return repo.full_name

    assert asyncio.run(run_test()) == "octo-org/stacked-prs"
    assert attempts == 2


def test_github_client_retries_secondary_rate_limits_without_retry_after() -> None:
    attempts = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpxyz.Response(
                403,
                headers={"X-RateLimit-Reset": "0"},
                json={"message": "You have exceeded a secondary rate limit."},
                request=request,
            )
        return httpxyz.Response(
            200,
            json={
                "default_branch": "main",
                "full_name": "octo-org/stacked-prs",
            },
            request=request,
        )

    async def run_test() -> str:
        async with _github_client(handler) as client:
            repo = await client.get_repo()
        return repo.default_branch or ""

    assert asyncio.run(run_test()) == "main"
    assert attempts == 2


def test_github_client_does_not_retry_non_rate_limited_errors() -> None:
    attempts = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal attempts
        attempts += 1
        return httpxyz.Response(404, json={"message": "Not Found"}, request=request)

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_repo()

    with pytest.raises(GithubClientError, match="GitHub request failed: 404"):
        asyncio.run(run_test())

    assert attempts == 1


def test_github_client_treats_an_unknown_head_commit_as_no_matching_branches() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.method == "GET"
        assert request.url.path == (
            "/repos/octo-org/stacked-prs/commits/trunk123/branches-where-head"
        )
        return httpxyz.Response(
            422,
            json={"message": "No commit found for SHA: trunk123"},
            request=request,
        )

    async def run_test() -> tuple[str, ...]:
        async with _github_client(handler) as client:
            return await client.list_branches_for_head_commit(commit_sha="trunk123")

    assert asyncio.run(run_test()) == ()


@pytest.mark.parametrize(
    ("base", "body", "title", "expected_payload"),
    (
        (None, "new body", "new title", {"body": "new body", "title": "new title"}),
        ("main", None, None, {"base": "main"}),
        ("main", "", "new title", {"base": "main", "body": "", "title": "new title"}),
    ),
)
def test_github_client_sends_only_supplied_pr_updates(
    base: str | None,
    body: str | None,
    title: str | None,
    expected_payload: dict[str, str],
) -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert json.loads(request.content.decode("utf-8")) == expected_payload
        return httpxyz.Response(
            200,
            json={
                "base": {"ref": base or "old-base"},
                "body": body or "",
                "head": {"ref": "jj-stack/feature"},
                "html_url": "https://github.test/octo-org/stacked-prs/pull/7",
                "number": 7,
                "state": "open",
                "title": title or "old title",
            },
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.update_pr(
                pr_number=7,
                base=base,
                body=body,
                title=title,
            )

    asyncio.run(run_test())


def test_github_client_distinguishes_dissolved_and_locked_stack() -> None:
    attempts = 0

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        nonlocal attempts
        attempts += 1
        assert request.method == "POST"
        assert request.url.path == "/repos/octo-org/stacked-prs/stacks/3/unstack"
        if attempts == 1:
            return httpxyz.Response(204, request=request)
        return httpxyz.Response(
            200,
            json={
                "number": 3,
                "pull_requests": [
                    {
                        "head": {"ref": "jj-stack/eight", "sha": "head-eight"},
                        "merged_at": None,
                        "number": 8,
                        "state": "open",
                    },
                    {
                        "head": {"ref": "jj-stack/nine", "sha": "head-nine"},
                        "merged_at": None,
                        "number": 9,
                        "state": "open",
                    },
                ],
            },
            request=request,
        )

    async def run_test() -> tuple[object, tuple[int, ...]]:
        async with _github_client(handler) as client:
            dissolved = await client.unstack(stack_number=3)
            remaining = await client.unstack(stack_number=3)
        if remaining is None:
            raise AssertionError("The second unstack should return its locked member.")
        return dissolved, remaining.pr_numbers

    assert asyncio.run(run_test()) == (None, (8, 9))


def test_github_client_paginates_stack_list() -> None:
    def _stack(number: int, *pr_numbers: int) -> dict[str, object]:
        return {
            "number": number,
            "pull_requests": [
                {
                    "head": {"ref": f"jj-stack/{pr_number}", "sha": f"head-{pr_number}"},
                    "merged_at": None,
                    "number": pr_number,
                    "state": "open",
                }
                for pr_number in pr_numbers
            ],
        }

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/repos/octo-org/stacked-prs/stacks"
        if request.url.params.get("page") == "2":
            return httpxyz.Response(200, json=[_stack(2, 20, 21)], request=request)
        return httpxyz.Response(
            200,
            headers={
                "Link": (
                    "<https://api.github.test/repos/octo-org/stacked-prs/stacks?page=2>; "
                    'rel="next"'
                )
            },
            json=[_stack(1, 10, 11)],
            request=request,
        )

    async def run_test() -> tuple[int, ...]:
        async with _github_client(handler) as client:
            return tuple(stack.number for stack in await client.list_stacks())

    assert asyncio.run(run_test()) == (1, 2)


def test_github_client_batches_pr_lookup_by_number_with_graphql() -> None:
    request_sizes: list[int] = []

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {"owner": "octo-org", "repo": "stacked-prs"}
        request_sizes.append(payload["query"].count("pullRequest(number:"))
        if len(request_sizes) == 1:
            assert "pr_7: pullRequest(number: 7)" in payload["query"]
            assert "pr_9: pullRequest(number: 9)" in payload["query"]
            assert "pr_11: pullRequest(number: 11)" in payload["query"]
            assert "autoMergeRequest" not in payload["query"]
            assert "mergeQueueEntry" in payload["query"]
            assert "statusCheckRollup" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pr_7": {
                            "autoMergeRequest": None,
                            "baseRefName": "main",
                            "body": "body 7",
                            "headRefName": "jj-stack/seven",
                            "headRepositoryOwner": {"login": "octo-org"},
                            "mergeQueueEntry": {"id": "queue-entry"},
                            "mergedAt": None,
                            "number": 7,
                            "state": "OPEN",
                            "statusCheckRollup": {"state": "SUCCESS"},
                            "title": "seven",
                            "url": "https://github.test/octo-org/stacked-prs/pull/7",
                        },
                        "pr_9": {
                            "autoMergeRequest": None,
                            "baseRefName": "jj-stack/base",
                            "body": None,
                            "headRefName": "jj-stack/nine",
                            "headRepositoryOwner": {"login": "octo-org"},
                            "mergeQueueEntry": None,
                            "mergedAt": "2026-03-16T12:00:00Z",
                            "number": 9,
                            "state": "CLOSED",
                            "title": "nine",
                            "url": "https://github.test/octo-org/stacked-prs/pull/9",
                        },
                        "pr_11": None,
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[str, str, str | None, bool, str | None, bool]:
        async with _github_client(handler) as client:
            prs = await client.get_prs_by_numbers(
                pr_numbers=(7, 9, 11, *range(100, 124)),
            )
        pr_7 = prs[7]
        pr_9 = prs[9]
        if pr_7 is None or pr_9 is None:
            raise AssertionError("GraphQL lookup should return both pull requests.")
        return (
            pr_7.head.ref,
            pr_9.state,
            pr_7.head.label,
            pr_7.is_queued,
            pr_7.check_rollup_status,
            prs[11] is None,
        )

    assert asyncio.run(run_test()) == (
        "jj-stack/seven",
        "closed",
        "octo-org:jj-stack/seven",
        True,
        "passed",
        True,
    )
    assert request_sizes == [25, 2]


def test_github_client_observes_exact_and_suffix_matched_branch_targets() -> None:
    suffix_queries: list[str] = []

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        query = payload["query"]
        if "BranchTargetsBySuffix" in query:
            suffix_queries.append(query)
            variables = payload["variables"]
            assert variables["owner"] == "octo-org"
            assert variables["repo"] == "stacked-prs"
            assert variables["suffix_0"] == "-aaaaaaaa"
            assert variables["ref_prefix"] == "refs/heads/jj-stack/"
            assert "query: $suffix_0" in query
            assert "refPrefix: $ref_prefix" in query
            if "after: $cursor_0" in query:
                assert variables["cursor_0"] == "page-1"
                nodes = [
                    {
                        "name": "old-slug-aaaaaaaa",
                        "prefix": "refs/heads/jj-stack/",
                        "target": {"oid": "old-target"},
                    }
                ]
                page_info = {"endCursor": "page-2", "hasNextPage": False}
            else:
                nodes = [
                    {
                        "name": "contains-aaaaaaaa-elsewhere",
                        "prefix": "refs/heads/jj-stack/",
                        "target": {"oid": "unrelated-target"},
                    }
                ]
                page_info = {"endCursor": "page-1", "hasNextPage": True}
            repo = {
                "suffix_0": {
                    "nodes": nodes,
                    "pageInfo": page_info,
                }
            }
        else:
            assert payload["variables"] == {
                "owner": "octo-org",
                "repo": "stacked-prs",
                "qualified_0": "refs/heads/jj-stack/current",
                "qualified_1": "refs/heads/jj-stack/missing",
            }
            assert "ref(qualifiedName: $qualified_0)" in query
            assert "ref(qualifiedName: $qualified_1)" in query
            repo = {
                "branch_0": {
                    "name": "jj-stack/current",
                    "prefix": "refs/heads/",
                    "target": {"oid": "current-target"},
                },
                "branch_1": None,
            }
        return httpxyz.Response(
            200,
            json={"data": {"repository": repo}},
            request=request,
        )

    async def run_test() -> tuple[dict[str, str], dict[str, str]]:
        async with _github_client(handler) as client:
            exact = await client.get_branch_targets(
                branches=("jj-stack/current", "jj-stack/missing"),
            )
            recovered = await client.find_branch_targets_by_suffix(
                branch_prefix="jj-stack/",
                suffixes=("-aaaaaaaa",),
            )
        return exact, recovered

    assert asyncio.run(run_test()) == (
        {"jj-stack/current": "current-target"},
        {"jj-stack/old-slug-aaaaaaaa": "old-target"},
    )
    assert len(suffix_queries) == 2


def test_github_client_detects_merge_queue_branch_rule() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {
            "owner": "octo-org",
            "repo": "stacked-prs",
            "branch": "main",
            "qualified": "refs/heads/main",
        }
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "mergeQueue": None,
                        "ref": {"rules": {"nodes": [{"type": "MERGE_QUEUE"}]}},
                    }
                }
            },
            request=request,
        )

    async def run_test() -> bool:
        async with _github_client(handler) as client:
            return await client.base_branch_uses_merge_queue(branch="main")

    assert asyncio.run(run_test())


def test_github_client_rejects_graphql_payload_missing_repo_data() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        return httpxyz.Response(
            200,
            json={"data": {}},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_prs_by_numbers(
                pr_numbers=(7,),
            )

    with pytest.raises(GithubClientError, match="missing repo data"):
        asyncio.run(run_test())


@pytest.mark.parametrize(
    "repo_payload",
    ({}, {"base_0": {}}),
)
def test_github_client_rejects_incomplete_pr_connection(
    repo_payload: dict[str, object],
) -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        return httpxyz.Response(
            200,
            json={"data": {"repository": repo_payload}},
            request=request,
        )

    async def run_test() -> None:
        async with _github_client(handler) as client:
            await client.get_open_prs_by_base_refs(
                base_refs=("jj-stack/seven",),
            )

    with pytest.raises(GithubClientError, match="invalid connection payload"):
        asyncio.run(run_test())


def test_github_client_batches_open_pr_lookup_by_head_ref_with_graphql() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {
            "owner": "octo-org",
            "repo": "stacked-prs",
            "ref_0": "jj-stack/nine",
            "ref_1": "jj-stack/seven",
        }
        assert "headRefName: $ref_0" in payload["query"]
        assert "headRefName: $ref_1" in payload["query"]
        assert "headRepositoryOwner" in payload["query"]
        assert "reviewDecision" in payload["query"]
        assert "states: [OPEN]" in payload["query"]
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "head_0": {
                            "nodes": [
                                {
                                    "baseRefName": "jj-stack/base",
                                    "body": None,
                                    "headRefName": "jj-stack/nine",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": None,
                                    "number": 9,
                                    "state": "OPEN",
                                    "title": "nine",
                                    "url": "https://github.test/octo-org/stacked-prs/pull/9",
                                }
                            ]
                        },
                        "head_1": {
                            "nodes": [
                                {
                                    "baseRefName": "main",
                                    "body": "body 7",
                                    "headRefName": "jj-stack/seven",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": None,
                                    "number": 7,
                                    "reviewDecision": "APPROVED",
                                    "state": "OPEN",
                                    "title": "seven",
                                    "url": "https://github.test/octo-org/stacked-prs/pull/7",
                                }
                            ]
                        },
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[str, str, str | None, str | None]:
        async with _github_client(handler) as client:
            prs = await client.get_open_prs_by_head_refs(
                head_refs=("jj-stack/seven", "jj-stack/nine"),
            )
        pr_7 = prs["jj-stack/seven"][0]
        pr_9 = prs["jj-stack/nine"][0]
        return (
            pr_7.head.ref,
            pr_9.state,
            pr_7.head.label,
            pr_7.review_decision,
        )

    assert asyncio.run(run_test()) == (
        "jj-stack/seven",
        "open",
        "octo-org:jj-stack/seven",
        "approved",
    )


def test_github_client_paginates_comments_and_skips_unavailable_revisions() -> None:
    queries: list[str] = []

    def handler(request: httpxyz.Request) -> httpxyz.Response:
        assert request.url.path == "/graphql"
        payload = json.loads(request.content.decode("utf-8"))
        queries.append(payload["query"])
        variables = payload["variables"]
        assert variables["owner"] == "octo-org"
        assert variables["repo"] == "stacked-prs"
        assert "pr_7: pullRequest(number: 7)" in payload["query"]
        assert "comments(first: 100" in payload["query"]
        next_page = "after: $comments_cursor_7" in payload["query"]
        if next_page:
            assert variables["comments_cursor_7"] == "comments-1"
            assert "timelineItems(" not in payload["query"]
        else:
            assert "itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT]" in payload["query"]
            assert "filteredCount" in payload["query"]
            assert "totalCount" not in payload["query"]
        pr_payload: dict[str, object] = {
            "comments": {
                "nodes": [
                    {
                        "body": (
                            "<!-- jj-stack-overview -->" if next_page else "ordinary comment"
                        ),
                        "databaseId": 71 if next_page else 70,
                    }
                ],
                "pageInfo": {
                    "endCursor": None if next_page else "comments-1",
                    "hasNextPage": not next_page,
                },
            }
        }
        if not next_page:
            pr_payload["timelineItems"] = {
                "filteredCount": 4,
                "nodes": [
                    {
                        "afterCommit": {"oid": "22222222"},
                        "beforeCommit": {"oid": "11111111"},
                    },
                    {
                        "afterCommit": {"oid": "33333333"},
                        "beforeCommit": None,
                    },
                    {
                        "afterCommit": None,
                        "beforeCommit": {"oid": "33333333"},
                    },
                    {
                        "afterCommit": {"oid": "55555555"},
                        "beforeCommit": {"oid": "44444444"},
                    },
                ],
            }
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "pr_7": pr_payload,
                    }
                }
            },
            request=request,
        )

    async def run_test() -> tuple[int | None, list[tuple[int, str, str, bool]]]:
        async with _github_client(handler) as client:
            comments, revisions = await client.find_issue_comments_and_revisions(
                body_markers=("<!-- jj-stack-overview -->",),
                pr_numbers=(7,),
                revision_limit=7,
            )
        comment = comments["<!-- jj-stack-overview -->"][7]
        return (
            comment.id if comment is not None else None,
            [
                (
                    revision.version,
                    revision.before_commit_id,
                    revision.commit_id,
                    revision.is_current,
                )
                for revision in revisions[7]
            ],
        )

    assert asyncio.run(run_test()) == (
        71,
        [
            (2, "11111111", "22222222", False),
            (5, "44444444", "55555555", True),
        ],
    )
    assert len(queries) == 2


def test_github_client_filters_batched_head_lookup_results_to_repo_owner() -> None:
    def handler(request: httpxyz.Request) -> httpxyz.Response:
        payload = json.loads(request.content.decode("utf-8"))
        assert payload["variables"] == {
            "owner": "octo-org",
            "repo": "stacked-prs",
            "ref_0": "jj-stack/seven",
        }
        return httpxyz.Response(
            200,
            json={
                "data": {
                    "repository": {
                        "head_0": {
                            "nodes": [
                                {
                                    "baseRefName": "main",
                                    "body": "forked",
                                    "headRefName": "jj-stack/seven",
                                    "headRepositoryOwner": {"login": "fork-user"},
                                    "mergedAt": None,
                                    "number": 6,
                                    "state": "OPEN",
                                    "title": "forked",
                                    "url": "https://github.test/octo-org/stacked-prs/pull/6",
                                },
                                {
                                    "baseRefName": "main",
                                    "body": "local",
                                    "headRefName": "jj-stack/seven",
                                    "headRepositoryOwner": {"login": "octo-org"},
                                    "mergedAt": None,
                                    "number": 7,
                                    "state": "OPEN",
                                    "title": "local",
                                    "url": "https://github.test/octo-org/stacked-prs/pull/7",
                                },
                            ]
                        }
                    }
                }
            },
            request=request,
        )

    async def run_test() -> list[int]:
        async with _github_client(handler) as client:
            prs = await client.get_open_prs_by_head_refs(
                head_refs=("jj-stack/seven",),
            )
        return [pr.number for pr in prs["jj-stack/seven"]]

    assert asyncio.run(run_test()) == [7]


def test_user_facing_reason_reports_repo_not_found_for_404_without_raw_detail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pretend a token is configured so the 404 reason omits the auth follow-up
    # hint, letting us assert the bare repo-not-found wording. The raw response
    # body (JSON, network phrasing) must never leak into the user-facing reason.
    monkeypatch.setattr("jj_stack.github.client.github_token_from_env", lambda: "token")
    error = GithubClientError(
        'GitHub request failed: 404 {"message":"Not Found","documentation_url":"x"}',
        status_code=404,
    )

    reason = error.user_facing_reason()

    assert reason == "repo not found or inaccessible"
    assert "documentation_url" not in reason
    assert "network" not in reason


def test_user_facing_reason_reports_auth_failure_for_401() -> None:
    error = GithubClientError("GitHub request failed: 401", status_code=401)

    assert error.user_facing_reason() == "auth failed - check GITHUB_TOKEN"


def test_user_facing_reason_reports_access_denied_for_403() -> None:
    error = GithubClientError("GitHub request failed: 403", status_code=403)

    assert error.user_facing_reason() == "access denied - check GITHUB_TOKEN and repo access"
