# Generated integration testing

The generated harness targets failures that emerge only when the local `jj` DAG, PR
branches, GitHub, and local tracking disagree. It supplements focused tests; it is not a second
product specification.

## Harness structure

[The property test module](../../tests/property/test_submit_property_scenarios.py) collects the
fixed and opt-in cases. Under `tests/support/`, `stack_edit_scenarios.py` models local edits,
`submit_property_scenarios.py` defines scenario families and generation, and
`submit_property_harness.py` replays them against the CLI and checks the result. The shared fake
GitHub implementation is in `tests/support/fake_github.py`.

Generated scenarios use real `jj` commands, a real Git repo, the CLI entry point, and the
fake GitHub server. A pure model may predict the result, but it cannot replace replay through
those boundaries. The expensive bugs are incorrect PR identity, branch targets, bases, or
recovery. Presentation details and private request ordering are not the purpose of this harness.

The harness must check these properties where applicable:

- A surviving change keeps its PR and review history across rewrites. The fake-server scenarios
  check approval preservation; they do not establish how every GitHub repo policy treats reviews.
- A new change receives a new PR. An abandoned change keeps its PR, branch, and tracking until the
  user closes it and runs cleanup.
- PR branches and PR bases match the current `jj` DAG after a successful submit.
- A successful update never transiently closes, merges, reopens, or replaces an existing PR.
- Drift classified as unsafe stops submit before local rewrites, pushes, GitHub mutations, or
  tracking writes, and reports the expected error category. Supported external changes follow
  their ordinary update rules.
- `view` reports a reachable drifted state or a targeted selection error instead of crashing.
- Retrying an interrupted submit reaches the intended state without duplicate PRs or lost PR
  identity.

The scenario families cover stack edits, joins, moves, failed-submit retries, external drift, and
sequences of completed commands. Merge and sync enumerate the transitions they test in a separate
family instead of expanding the general stack-edit generator.

## Reproducibility and size

Each generated case has a stable ID, a compact operation trace, expected abstract state, and a
risk category. De-duplication may discard an equivalent final state only when the orphaned
pull requests, rewritten changes, and risk category also match.

Generation uses explicit seeds, stable sorting, bounded stack sizes and trace lengths, and no
hash-order dependence. Every pytest worker must collect the same cases. If generation cannot find
the requested number of unique cases within its attempt limit, it returns the cases found instead
of looping indefinitely.

`just check` runs a small fixed set. CI also runs a bounded pool with a printed random seed.
For a larger local pool:

```console
just property 500
```

The runner prints a complete reproduction command with the resolved seed, family counts, worker
count, and extra pytest arguments. Use `just property --help` for syntax. The positional count
controls stack-edit cases; other families have separate counts, so it is not the total test count.
Pytest arguments follow a literal `--`, for example:

```console
just property 500 --seed 8675309 -n 1 -- -x -k CASE_ID
```

Replace `CASE_ID` with the failed case's pytest ID. Preserve the original seed and family counts
from the printed reproduction command so collection includes the same scenario.

When a generated scenario catches a bug, keep a reduced representative in the fixed set, or
add a focused test if the failure belongs at a narrower boundary. Consolidate overlapping cases
and stay within the checked-in test and code-size limits.
