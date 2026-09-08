"""Fixed regressions and generated sequences share the same actions and assertions."""

from collections.abc import Iterator

import pytest
from hypothesis import seed, settings
from hypothesis.database import DirectoryBasedExampleDatabase
from hypothesis.stateful import run_state_machine_as_test
from tests.run_submit_property_scenarios import EXAMPLES, SEED, SHARDS, STEPS
from tests.support.stack_edit_scenarios import StackEditOperation
from tests.support.stack_machine import StackMachine

pytestmark = pytest.mark.fixed_property


@pytest.fixture
def machine() -> Iterator[StackMachine]:
    machine = StackMachine()
    try:
        yield machine
        machine.model_matches()
    finally:
        machine.teardown()


def test_squashing_a_submitted_middle_change_preserves_its_orphan_pr(
    machine: StackMachine,
) -> None:
    machine.start(size=3, submitted=True)
    machine.apply_edit(0, StackEditOperation("squash_into_previous", "c2"))
    machine.submit_path(0)


def test_joining_submitted_stacks_preserves_both_sets_of_prs(machine: StackMachine) -> None:
    machine.start(size=2, submitted=True)
    machine.new_stack(2)
    machine.submit_path(1)
    machine.approve(machine.paths[1])
    machine.join_paths(1, 0)
    machine.submit_path(0)


def test_moving_between_stacks_refreshes_the_source_before_the_destination(
    machine: StackMachine,
) -> None:
    machine.start(size=3, submitted=True)
    machine.new_stack(2)
    machine.submit_path(1)
    machine.approve(machine.paths[1])
    machine.move_between(0, 1, 1, 1, False)


def test_submit_recovers_after_an_unacknowledged_push(machine: StackMachine) -> None:
    machine.start(size=3, submitted=False)
    machine.interrupted_submit(0, "after_remote_push", 0)


def test_a_closed_pr_blocks_publishing_an_inserted_change(machine: StackMachine) -> None:
    machine.start(size=3, submitted=True)
    machine.apply_edit(0, StackEditOperation("insert_after", "c1", new_label="c4"))
    machine.drift("closed_pr", "c2")
    machine.submit_path(0)


def test_cleanup_requires_sync_after_an_external_squash_merge(machine: StackMachine) -> None:
    machine.start(size=1, submitted=True)
    machine.server_merge(0, 1, "squash")
    machine.cleanup_before_sync(0)
    machine.sync_path(0)


def test_cleanup_of_a_closed_pr_allows_a_new_pr_for_the_same_change(
    machine: StackMachine,
) -> None:
    machine.start(size=1, submitted=True)
    old = machine.pr("c1").number
    machine.drift("closed_pr", "c1")
    machine.cleanup_label("c1")
    machine.submit_path(0)
    assert machine.pr("c1").number != old
    assert machine.fake.prs[old].state == "closed"
    assert machine.fake.prs[old].merged_at is None


def test_partial_rebase_merge_preserves_surviving_ids_and_reviews(machine: StackMachine) -> None:
    machine.start(size=4, submitted=True)
    machine.merge_path(0, 2, "rebase")


@pytest.mark.parametrize("shard", range(SHARDS))
def test_generated_commands(shard: int) -> None:
    @seed(SEED + shard)
    def factory() -> StackMachine:
        return StackMachine()

    run_state_machine_as_test(
        factory,
        settings=settings(
            max_examples=EXAMPLES,
            stateful_step_count=STEPS,
            deadline=None,
            database=DirectoryBasedExampleDatabase(".hypothesis/examples"),
        ),
    )
