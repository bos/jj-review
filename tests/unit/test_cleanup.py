from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import jj_stack.commands.cleanup.stale as stale_module
from jj_stack.jj.client import JjClient
from jj_stack.stack.pr_facts import (
    duplicate_pr_claim_change_ids,
)
from tests.support.change_helpers import make_change
from tests.support.contexts import fake_command_context
from tests.support.tracking import make_pr_identity

BRANCH = "jj-stack/feature-aaaaaaaa"


def test_duplicate_claim_facts_reject_shared_prs_and_branches() -> None:
    identity = make_pr_identity(head_ref=BRANCH)
    same_pr = identity.model_copy(update={"head_ref": "jj-stack/other-bbbbbbbb"})
    same_branch = identity.model_copy(update={"pr_number": 2})

    assert duplicate_pr_claim_change_ids({"saved": identity, "same-pr": same_pr}) == frozenset(
        {"saved", "same-pr"}
    )
    assert duplicate_pr_claim_change_ids(
        {"saved": identity, "same-branch": same_branch}
    ) == frozenset({"saved", "same-branch"})


def test_local_cleanup_observations_flag_changes_outside_current_stacks(
    monkeypatch,
) -> None:
    live_change = make_change(
        change_id="live-change",
        commit_id="live-commit",
        description="live\n",
    )
    stale_change = make_change(
        change_id="stale-change",
        commit_id="stale-commit",
        description="stale\n",
    )

    class FakeJjClient:
        def query_commits_by_change_ids(self, change_ids):
            assert change_ids == ("live-change", "stale-change")
            return {
                "live-change": (live_change,),
                "stale-change": (stale_change,),
            }

    monkeypatch.setattr(
        stale_module,
        "observe_repo_paths",
        lambda **_kwargs: SimpleNamespace(
            paths=(SimpleNamespace(stack=SimpleNamespace(changes=(live_change,))),)
        ),
    )

    observations = stale_module.local_cleanup_observations(
        change_ids=("live-change", "stale-change"),
        context=fake_command_context(jj_client=cast(JjClient, FakeJjClient())),
    )

    assert observations["live-change"] == stale_module.LocalCleanupObservation(
        has_mutable_copy=True,
        stale_reason=None,
    )
    stale_observation = observations["stale-change"]
    assert stale_observation.has_mutable_copy
    assert stale_observation.stale_reason is not None
