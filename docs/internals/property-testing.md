# Generated integration testing

The generated harness targets failures that emerge when the local `jj` DAG, PR branches,
GitHub, and local tracking disagree. Follow the [testing philosophy](testing-philosophy.md)
when extending it; product rules belong in [design.md](design.md).

## Model and coverage

[The property tests](../../tests/property/test_submit_property_scenarios.py) run fixed regressions
and Hypothesis stateful searches through [`StackMachine`](../../tests/support/stack_machine.py).
Each example uses a fresh copy of a cached repository, real `jj` commands, and the fake GitHub
server. The machine owns expected paths, change IDs, file contents, and submitted records;
the [edit model](../../tests/support/stack_edit_scenarios.py) predicts local order changes.

Keep client commands, server events, and recovery actions separate so Hypothesis can interleave
them. For example, local edits can happen between a server merge and client sync, or between a
failed submit and its retry.

Assert observable outcomes: content and PR identity survive supported rewrites and recovery,
publication follows the local DAG, unrelated work stays unchanged, and unsafe operations stop
without unintended mutation. Predict outcomes from the model and external actions, independently
of production planning code. Preconditions select applicable actions; they must not exclude
unsafe attempts whose refusal is worth testing.

Shared-file edits use single-line replacements. Generated moves preserve the order of changes
that edit the same file, keeping conflict expectations independent of `jj`'s merge algorithms.
Fake GitHub's review preservation does not establish how every repository policy treats reviews.

## Running and reproducing searches

`just check` includes fixed regressions and a small generated search. CI runs a larger search.
To explore locally:

```console
just property --steps 40 --random-seed
```

The positional count sets examples per shard; `--steps` sets the maximum actions per example.
Shards are independent seeded searches. [The runner](../../tests/run_submit_property_scenarios.py)
defaults to all available CPUs, five examples per shard, and four shards per worker. pytest
redistributes pending searches as workers finish. Use `-n` and `--shards` to override parallelism.
Smaller shards improve scheduling without shortening sequences; a running search and its shrinking
stay on one worker. Example counts are search budgets, not counts of unique histories.

The runner prints a reproduction command with the seed and resolved settings. Pass pytest
arguments after `--`, for example `-- -k generated_commands` to omit fixed regressions.
Each shard saves examples separately under `.hypothesis/` so workers do not all replay and shrink
the same saved failure. Keep the Hypothesis version when replaying encoded inputs; the database
is not permanent regression coverage.

Do not deduplicate by final topology: different histories can reach the same path. When a search
finds a bug, retain a reduced representative through the shared actions or a focused test at a
narrower boundary. Consolidate overlapping coverage and stay within the complexity budgets.
