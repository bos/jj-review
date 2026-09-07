from __future__ import annotations

from jj_stack.models.github import GithubPR, GithubStack


def _graphql_pr_payload(
    review_decision: object,
    *,
    check_rollup_state: object = None,
) -> dict[str, object]:
    return {
        "autoMergeRequest": None,
        "baseRefName": "main",
        "headRefName": "jj-stack/feature-1",
        "headRefOid": "head-commit-id",
        "headRepositoryOwner": {"login": "octo-org"},
        "mergeQueueEntry": None,
        "id": "PR_1",
        "number": 1,
        "reviewDecision": review_decision,
        "state": "OPEN",
        "statusCheckRollup": (
            None if check_rollup_state is None else {"state": check_rollup_state}
        ),
        "title": "feature 1",
        "url": "https://github.test/octo-org/stacked-prs/pull/1",
    }


def test_graphql_pr_statuses_normalize_known_states_and_drop_unknown() -> None:
    approved = GithubPR.model_validate(
        _graphql_pr_payload("APPROVED", check_rollup_state="SUCCESS")
    )
    changes = GithubPR.model_validate(
        _graphql_pr_payload("CHANGES_REQUESTED", check_rollup_state="FAILURE")
    )
    errored = GithubPR.model_validate(_graphql_pr_payload(None, check_rollup_state="ERROR"))
    pending = GithubPR.model_validate(_graphql_pr_payload(None, check_rollup_state="PENDING"))
    expected = GithubPR.model_validate(_graphql_pr_payload(None, check_rollup_state="EXPECTED"))
    unknown = GithubPR.model_validate(
        _graphql_pr_payload("REVIEW_REQUIRED", check_rollup_state="FUTURE_STATE")
    )

    assert approved.review_decision == "approved"
    assert approved.check_rollup_status == "passed"
    assert approved.head.sha == "head-commit-id"
    assert changes.review_decision == "changes_requested"
    assert changes.check_rollup_status == "failed"
    assert errored.check_rollup_status == "failed"
    assert pending.check_rollup_status == "pending"
    assert expected.check_rollup_status == "pending"
    assert unknown.review_decision is None
    assert unknown.check_rollup_status is None


def test_github_stack_splits_history_and_reports_a_merged_member_above_an_active_one() -> None:
    historical = {
        "head": {"ref": "jj-stack/one", "sha": "head-one"},
        "merged_at": "2026-07-23T12:00:00Z",
        "number": 1,
    }
    active = {
        "head": {"ref": "jj-stack/two", "sha": "head-two"},
        "merged_at": None,
        "number": 2,
    }

    stack = GithubStack.model_validate({"number": 7, "pull_requests": [historical, active]})

    assert stack.historical_pr_numbers == (1,)
    assert stack.active_pr_numbers == (2,)
    assert stack.has_merged_prefix
    reversed_stack = GithubStack.model_validate(
        {"number": 7, "pull_requests": [active, historical]}
    )
    assert not reversed_stack.has_merged_prefix


def test_github_stack_defaults_missing_merge_state_to_active() -> None:
    stack = GithubStack.model_validate(
        {
            "number": 7,
            "pull_requests": [
                {"head": {"ref": "jj-stack/one", "sha": "head-one"}, "number": 1},
                {"head": {"ref": "jj-stack/two", "sha": "head-two"}, "number": 2},
            ],
        }
    )

    assert stack.active_pr_numbers == (1, 2)
    assert stack.historical_pr_numbers == ()
