# Implementation strategy

[design.md](design.md) defines product behavior. This document explains how the code separates
observation, policy, external effects, and storage.

## Source map

Paths below are relative to `src/jj_stack/`.

| Location | Responsibility |
|---|---|
| `cli.py`, `bootstrap.py`, `config.py` | Parse arguments and construct command context. |
| `jj/client.py`, `github/client.py` | Run subprocesses and HTTP requests; return typed results. |
| `stack/selected.py`, `stack/repo.py`, `stack/path.py` | Observe and select local paths. |
| `stack/change_state.py` | Classify a change's relationship to its PR and submitted commit. |
| `stack/trunk_evidence.py` | Establish whether submitted work reached fetched trunk. |
| `stack/convergence*.py`, `stack/global_convergence.py` | Plan updates after GitHub changes. |
| `commands/` | Coordinate each command's planning, mutations, and output. |
| `models/tracking.py`, `state/` | Validate, migrate, lock, and persist tracking data. |

## Authority and stored state

The DAG determines local topology. Tracking contains one `TrackedPR` per full change ID, pairing
`PRIdentity` (PR number and head branch) with `SubmittedBaseline` (the last submitted or adopted
commit). The GitHub repo comes from command context; it is not stored in each record. See
[tracking rules](design.md#tracking) for the meaning and allowed updates of these fields.

All workspaces for one `jj` repo share a state file and operation lock. The canonical `.jj/repo`
storage path identifies the repo, including when a workspace's `.jj/repo` is a pointer file.
`state/store.py` hashes that resolved path to form the storage location:

```text
${XDG_STATE_HOME:-~/.local/state}/jj-stack/repos/<repo-path-hash>/state.json
```

No tracking file is written into the working tree or `.jj/`. Moving the underlying repo storage
changes the lookup key; a new clone has separate tracking even when it uses the same remote.

`TrackingStore` validates complete records and writes them by atomic file replacement. Released
schemas migrate in memory before validation; read-only loads never rewrite the file. The next
tracking mutation persists the current schema. The top-level version is the only migration key;
[`state/migrations.py`](../../src/jj_stack/state/migrations.py) defines supported versions. Keep
migration at this boundary rather than retaining old models in command logic.

The OS operation lock serializes mutating jj-stack processes for the repo. Its holder file records
the command, PID, and start time for diagnostics. The lock does not coordinate other clients or
make GitHub, Git, and local storage transactional. No operation phase, replay plan, selector, or
transaction state is persisted.

## Observation, planning, and mutation

Commands separate five concerns:

1. Observe typed local commits, remote refs, GitHub objects, and tracking.
2. Classify those facts without side effects.
3. Validate selected targets and prerequisites before the first mutation.
4. Apply dependent mutations in order, planning later steps from completed results when needed.
5. Save acknowledged results and report completed work, including partial failure.

Shared observation code gathers facts; it must not introduce competing definitions of command
policy. Planning code decides what a command may do with those facts.

The shared `classify` function in `stack/change_state.py` takes a `ChangeObservation` and returns
a `ChangeState`. A fact the caller did not query is `Unobserved`, distinct from an absent PR or
branch. Stop states carry a shared explanation and repair; callers supply the command to rerun
and decide which states they can act on or tolerate. Callers must obtain the observations their
operation requires: an unobserved fact is not permission to mutate.

Trunk evidence is derived from fetched-trunk ancestry and included in the observation. The
classifier does not describe local conflicts, emptiness, divergence, or working copies; those
remain fields of `LocalCommit`. Selection and planning enforce stack-shape rules.

Batch or concurrently read independent facts. Re-observe when a preceding mutation invalidates
an input or an external API requires it. Bind irreversible writes to the observed identity and
version wherever the platform supports a conditional request or lease.

One submit moves its selected PR branches in an atomic Git push, with an exact lease for each ref
and expected absence for each new branch. There is no sequential fallback. GitHub updates and
tracking writes happen separately. Reruns use current observations and the saved PR record to
recognize completed work; they do not assume the previous command did nothing.

## External boundaries

The jj client invokes `jj` and Git as subprocesses. It reads machine-readable `jj` templates,
rather than display output. Direct Git access is limited to remote inspection and leased ref
mutation that `jj` does not expose with enough precision.

The client returns commit and bookmark facts. Stack observation distinguishes fetched submitted
snapshots from local rewrites and derives the reserved-bookmark immutability exception. That
subprocess configuration is passed explicitly to observations and mutations; observing a stack
does not change later client queries.

Setup and presentation reads may ignore the working copy. Fetches and rewrites preserve normal
`jj` snapshot and checkout behavior. PR branch refs are inspected without importing them into the
ordinary `jj` view. Commands that need a remote commit use a temporary ref and remove it
afterward; interruption can leave it for the repairs described under
[adoption](design.md#adoption-and-repair).

When a rebase must be checked before it changes the repo, the client uses
`--no-integrate-operation`. It inspects the candidate DAG with `--at-op` and integrates that exact
operation only after the caller's checks pass. Operation IDs exist only within the command and
are never stored for recovery.

GitHub transport owns authentication, pagination, bounded retries, batching, response validation,
and error decoding. It returns typed observations and mutation results. Stack topology,
selection, branch naming, and mutation eligibility belong outside the transport.

Configuration is read through `jj config`, preserving user, repo, workspace, `--config`, and
`--config-file` precedence. Python does not merge those scopes again. Supported settings belong
in the [configuration reference](../reference/configuration.md).

Serialized and untrusted data uses Pydantic models. In-process plans and results use typed
dataclasses where practical. Public `--json` output has its own
[schema](../json-output.schema.json); tracking and GitHub models are not public output formats.

## Documentation generation

The CLI parser and help text supply terminal help and the complete online command reference.
`jj-stack help --all-in-one` emits Markdown with semantic HTML classes. The website runs that
renderer from the same checkout it uses for the user-guide snapshot, then adds front matter,
CSS, navigation, and publication.

## Development and testing

The root [`justfile`](../../justfile) is the entry point for setup, CLI execution, checks,
generated scenarios, live qualification, website synchronization, and release artifacts. Its
recipes delegate to scripts shared with CI or owned by the website. Contributor commands belong
in [CONTRIBUTING.md](../../CONTRIBUTING.md); release gates belong in [releasing.md](releasing.md).

Local integration tests use real `jj` and Git repos with a FastAPI fake GitHub server. The fake's
branch and ancestry assertions use a real backing Git repo. Implement the GitHub behavior the
client needs, and document known differences beside the affected fake behavior and tests.

[testing-philosophy.md](testing-philosophy.md) defines which tests to retain;
[property-testing.md](property-testing.md) describes the generated harness. Complexity limits are
set in [`complexity-budget.toml`](../../complexity-budget.toml), enforced by
[`tools/check_complexity.py`](../../tools/check_complexity.py), and governed by the root
[complexity policy](../../AGENTS.md#complexity-control).
