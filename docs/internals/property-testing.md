# Generated integration testing

The generated harness targets failures that emerge when the local `jj` DAG, PR branches,
GitHub, and local tracking disagree. It supplements focused tests; it is not a second product
specification.

## Actions and assertions

[The property tests](../../tests/property/test_submit_property_scenarios.py) run fixed regressions
and Hypothesis searches through the same
[`StackMachine`](../../tests/support/stack_machine.py). The machine owns the expected local
paths, change IDs, file contents, and submitted records. The small
[edit model](../../tests/support/stack_edit_scenarios.py) predicts order changes independently of
`jj`. Actions execute real `jj` commands and the CLI against cached repositories and the shared
fake GitHub server.

Client commands and server events are separate actions. A server merge updates GitHub without
syncing the local repository. Hypothesis can choose subsequent commands and events using the
state left by earlier actions. Local edits, joins, cross-stack moves, interrupted submits, PR
closure and reopening, orphan cleanup, branch deletion, external refs, metadata changes,
approvals, merges, native GitHub rebases, and sync share the same assertions. Surviving changes
can be amended between a server merge and sync. Trunk advances change file contents, including
repeated updates to the same file. Joins, cross-stack moves, failed submits, retries, and explicit
relinks are separate steps, so local edits and server events can intervene before recovery.
Submitting a cross-stack move in the wrong order must stop without mutation.

Both submitted and unsubmitted setups use the normal PR-branch fetch exclusion. Native rebase
actions require a changed base; the model does not assume GitHub rewrites a stack that is already
up to date.

Check these properties at the boundaries where they apply:

- A surviving change keeps its PR and review history across rewrites. Fake-server approval
  preservation does not establish how every GitHub repository policy treats reviews.
- A new change receives a new PR. An abandoned change retains its PR, branch, and tracking until
  explicit cleanup makes it eligible for removal.
- PR branches, submitted commits, and PR bases match the selected `jj` path after publication.
- Updating a stack does not transiently close, merge, reopen, or replace an existing PR, or alter
  an unrelated PR or branch.
- Unsafe drift stops submit with the expected diagnosis and preserves local history, remote
  refs, GitHub state, and tracking. `view` still produces a report or targeted diagnostic.
- An interrupted submit can recover without duplicate PRs or lost links, including explicit
  relink when GitHub created a PR that the client did not acknowledge.
- Merge and sync preserve surviving change IDs and reviews, and remove eligible merged artifacts.
- Each surviving change retains its modeled file additions and contents after rewriting, moving,
  squashing, or syncing. Merges preserve both the submitted contents and unrelated work on trunk.
- A native GitHub rebase can be reconciled while preserving local change IDs. If trunk advances
  again, sync refuses that stale rebase without rewriting local work or PR branches.

The test model must predict these outcomes from the actions taken, rather than asking production
planning code what to expect. Preconditions select applicable actions; deliberately unsafe
command attempts must remain available where refusal is the behavior under test.

## Running and reproducing searches

`just check` runs six fixed regressions and a small generated search. CI runs a larger search
with a printed random seed. To explore more sequences locally:

```console
just property 20 --steps 30 --shards 4 --random-seed
```

The positional count is the number of Hypothesis examples per shard. Each example starts with a
fresh copy of a cached repository and runs up to `--steps` actions. Shards are independent seeded
searches, distributed over pytest workers selected by `-n` (default `auto`). Example counts are
search budgets, not counts of unique histories; Hypothesis may replay inputs or discard draws.

The runner prints a reproduction command containing the seed, budgets, and worker count. To
select just the generated tests, pass pytest arguments after a literal `--`:

```console
just property 20 --steps 30 --shards 4 --seed 8675309 -- -k generated_commands
```

Hypothesis shrinks failures into short action sequences and saves examples in `.hypothesis/`,
which is ignored by source control. Keep the seed and settings when reproducing a search, and
keep the Hypothesis version when replaying its encoded inputs. The example database speeds up
iteration but is not permanent regression coverage.

Do not de-duplicate by final topology: different histories and contents can reach the same
abstract path. When a generated sequence catches a bug, retain a reduced representative through
the shared actions, or add a focused test if the failure belongs at a narrower boundary.
Consolidate overlapping cases and stay within the checked-in test and code-size limits.
