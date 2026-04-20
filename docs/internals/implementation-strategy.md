# JJ-Native Stacked GitHub Review Implementation Strategy

This document describes how we intend to implement the stacked GitHub review
tool described in [JJ-Native Stacked GitHub Review
Design](./design.md).

It is intentionally pragmatic. The goal is to describe how we will build the
tool, how we will test it, and how we will stage the work into reviewable
commits.

Anything marked `XXX` is not fully cooked yet and should be treated as a draft
decision or an open question.

Non-blocking follow-up design questions and deferred architecture concerns
should be added to [Backlog](./backlog.md) rather than left implicit in code or
commit discussion.

## Relationship to the Design Doc

[JJ-Native Stacked GitHub Review Design](./design.md) is the canonical source
for product behavior and policy, including:

- the review-unit and stack model
- bookmark naming and saved-data semantics
- submit, status, relink, and cleanup behavior
- current command surface and scope
- fail-closed behavior when review identity is ambiguous

This document focuses on implementation choices that follow from that design:
repository layout, component boundaries, tooling, test strategy, and delivery
sequencing.

## Summary

We will build a Python client that maps a `jj` stack to GitHub's branch-based
pull request model.

The client will:

- shell out to `jj` and `git` rather than linking to `jj-lib`
- use the `uv` toolchain for development, execution, and dependency management
- use `ty` for static type checking
- use `pydantic` for structured local and remote data models
- use `httpx` for GitHub API traffic

We will test every feature first against a local fake GitHub server backed by a
real Git repository, and then against a genuine GitHub test repository in an
opt-in live test mode.

We will develop the tool the same way we want people to review with it:
logical, self-contained, well-described stacked commits.

## Goals

1. Build a useful tool quickly without painting ourselves into a corner.
2. Keep the `jj` DAG as the source of truth for stack topology.
3. Keep GitHub integration narrow, explicit, and easy to inspect in tests.
4. Prefer end-to-end feature slices over large batches of infrastructure work.
5. Make the local fake GitHub environment the default place to develop and
   debug behavior.
6. Continuously validate the fake environment against real GitHub behavior.

## Non-Goals

Product-level scope follows the design doc. Additional implementation
non-goals for the first pass:

- support for non-GitHub forges
- a daemon or long-running background sync process
- a GUI or web UI

Reviewer and label assignment are in scope for PR creation and update flows.

## Implementation Model

At a high level, each command should follow the same shape:

1. Read local `jj` and `git` state.
2. Compute the desired jj-review data.
3. Read relevant GitHub state.
4. Reconcile actual remote state with desired state.
5. Apply mutations in a controlled order.
6. Persist only minimal local jj-review data and user-authored overrides.

When a stale submit intent is present, submit should refresh remote bookmark
state before re-resolving the stack and repair any matching untracked review
bookmarks whose local and remote targets already agree. That keeps reruns
resumable after an interruption in the untracked-remote update path.

We should keep the code separated along those boundaries so that planning logic
can be tested without network or subprocess side effects.

Recent refactor slices:

- repo defaults now live directly under `[jj-review]` instead of a nested
  `[jj-review.repo]` table; unknown keys in that namespace are ignored, the
  old nested table is rejected with a direct migration error, and commands now
  read bookmark-prefix, reviewer, team-reviewer, and label defaults from the
  flattened config shape
- repo config now supports a configurable bookmark prefix for generated review
  branches; submit, status, import, land, close, and cleanup all honor that
  prefix for default naming, rediscovery, policy warnings, and review bookmark
  cleanup, while existing saved bookmark names remain pinned and are not
  renamed automatically when the config changes
- `land` now skips stack-summary comment lookups during its readiness/status
  inspection, because the command does not use that data to plan or dry-run a
  landing; that trims one GitHub issue-comment request per open PR from
  `land --dry-run` and the preflight path before execution
- status preparation now skips the first selected-stack discovery when a
  command will unconditionally fetch and then immediately re-resolve the stack,
  so `land` avoids one redundant round of `jj log` stack-walk queries before
  refresh
- `status` is now local-first for untracked stacks: it no longer creates
  bookmark-only saved state or does speculative GitHub PR discovery for
  never-tracked local changes, while `import` is the explicit recovery path
  for pre-existing remote review state
- `land` now preflights the selected stack's `base_parent` against the resolved
  `trunk()` and refuses stacks that fork from an older trunk ancestor (or a
  merged-side-branch boundary), pointing the operator at `cleanup --restack`
  or plain `jj rebase` instead of failing deep inside a non-fast-forward local
  trunk bookmark move
- default stack discovery now resolves `trunk()`, `@`, `@-`, and any merged
  side-branch parents in one `jj` query instead of separate trunk and
  default-head probes, trimming the subprocess cost of common implicit-stack
  commands while preserving merge-boundary parity with the explicit revset
  path
- explicit stack selection now resolves `trunk()`, merged side-branch
  boundaries, and the selected head revision in one `jj` query, so selectors
  like `@-`, a change ID, or a bookmark that all name the same revision take
  the same fast stack-discovery path instead of diverging by selector spelling
- integration coverage now smoke-tests empty repos, disconnected-root stack
  shapes, non-GitHub remotes, and merge-commit selections so core commands
  fail cleanly without tracebacks on realistic boundary cases
- `status` now renders GitHub target failures as ordinary warning lines with
  explicit `error:` wording instead of the hanging-indent status helper, so
  lookup failures and non-GitHub remotes no longer split awkwardly across
  lines
- CLI failures can now separate the diagnosis from the next-step guidance,
  rendering `Error:` on its own line and any actionable `Hint:` on the next
  line instead of tinting both as one long failure sentence
- top-level and subcommand `help` output now build on shared `ui` definition-
  list and prefixed-line primitives, so section headings, command labels, and
  option labels render through the Rich-backed console path instead of
  hand-wrapped `print()` output in `cli.py`
- shared console output now decodes ANSI-bearing native `jj` strings before
  printing them, so commands like `submit --dry-run` no longer leak raw escape
  sequences when repo color config forces native `jj` rendering
- applied action rows in `close`, `cleanup`, `land`, and `abort` now keep
  success emphasis on the status marker instead of tinting the full action
  description, and tracking-removal messages now use plain "review tracking"
  wording instead of internal "saved jj-review" phrasing
- multi-step GitHub progress bars now live in the shared `ui` layer and are
  reused by `status`, `submit`, `close`, `cleanup`, `import`, `unlink`, and
  `abort` only for otherwise-silent per-change GitHub work in interactive
  terminals
- `status` now uses Rich's native progress bar for GitHub inspection instead
  of `tqdm`, so interactive progress rendering stays within the shared Rich
  output stack and no longer needs a separate dependency.
- `close`, `cleanup`, `import`, `land`, `relink`, and `unlink` now skip the
  selected-revset, remote, and GitHub preamble lines so command output starts
  with the action summary or result instead of repeating stack-selection
  context.
- `close` now accepts `--pull-request` as a selector shortcut for one linked
  local change, prints the resolved change ID, and then runs the usual
  stack-based close flow for that selected change.
- `review_state` now routes its final line emission through the shared
  Rich-backed `ui` helpers instead of mixing direct `print()` calls into the
  status rendering path.
- `close` now routes its selected-stack summary, action rows, and stale-intent
  diagnostics through the shared Rich-backed `ui` helpers, with semantic
  bodies for bookmarks, change IDs, and revsets.
- `close` now keeps its execution state, resumable-intent setup, and per-revision
  cleanup context in explicit helpers instead of threading that orchestration
  through one long async path.
- `cleanup` now routes its CLI modes through separate helpers and keeps restack
  intent setup, policy warnings, and survivor-rebase planning in named phases.
- plain `cleanup` now defers remote and GitHub target resolution until stale
  remote-branch or stack-comment work is actually possible, so local-only and
  no-op runs avoid repo-wide remote discovery overhead.
- `cleanup` now retires stale interrupted cleanup intents after a successful
  rerun so `status` stops reporting already-resolved cleanup work.
- `land` now routes dry-run planning, resume validation, and execution through
  explicit helper phases instead of one deeply nested async path.
- `land` now renders its summary lines, resume notices, and action rows
  through the shared Rich-backed `ui` helpers, with inline semantic labels for
  bookmarks, change IDs, and revsets.
- `land` now accepts `--pull-request` as an alternate selector for the linked
  local change, so operators can land the consecutive ready prefix through a
  chosen PR instead of relying only on revset selection.
- `import` now shares the same repo-matched `--pull-request` number-or-URL
  parsing and validation helper as `land` and `close`, while keeping its
  existing GitHub-head-branch import flow.
- `submit` now prepares local stack inputs, resumable intent state, and
  per-revision bookmark push plans through separate helpers before touching
  GitHub.
- repo-scoped jj-review state now derives its storage path directly from the
  canonical `.jj/repo` location, so commands no longer thread optional
  `state_dir` plumbing or depend on `config-id` bootstrap before reads and
  writes
- `cleanup` stale-state checks now reuse the same selected-stack path semantics
  as stack discovery, so off-path sibling stacks no longer cause valid cached
  review state to be classified as stale.
- GitHub GraphQL pull request and review lookups now validate their JSON
  payloads through `pydantic` normalization at the model and helper boundary
  instead of repeating ad hoc nested `isinstance` checks in each response
  helper.
- shared revset-selection coverage now lives in dedicated `selection` unit
  tests so command entrypoint suites do not each repeat the same wrapper-only
  assertions
- machine-written persisted data now uses JSON plus `pydantic` validation for
  both repo state and resumable intent files, while TOML remains reserved for
  human-authored config
- human-authored config now loads from jj's user, repo, and workspace config
  scopes under the `jj-review` namespace instead of a separate path-matched
  config file
- `submit` now supports CLI label overrides alongside reviewer overrides, using
  the same repeated-flag and comma-separated parsing shape as `--reviewers`
- user-facing onboarding docs now live directly under `docs/`, while internal
  design and implementation notes live under `docs/internals/`; the root
  README is narrowed to the install path, five-minute quickstart, and links
  into task-oriented guides instead of mixing contributor notes with the full
  user workflow
- `abort` ships as a top-level support command that reads the outstanding intent
  file, retracts completed work for an interrupted submit (close PRs, delete
  remote branches, forget local bookmarks, clear saved state), and removes the
  intent file; for non-retractable intents it removes the file and explains what
  manual inspection may be needed; `--dry-run` previews the retraction plan
  without mutating anything
- `unlink` now routes its user-facing output through the shared Rich-backed
  `ui` helpers, keeping selected revsets, change IDs, and bookmarks semantic
  where that improves readability without adding extra abstraction
- interrupted `submit` state now records the selected remote and ordered commit
  IDs in addition to change IDs, plus the submitted GitHub repository
  coordinates; status/reporting treats that data as recovery metadata rather
  than an instruction to replay the original mutable selector
- interrupted `submit` recovery policy now lives in one dedicated helper module
  with exact-continuation-only semantics: only an exact recorded stack snapshot
  on the same recorded target counts as a continuation, while rewritten or
  otherwise different stacks stay as outstanding records until cleanup or a
  later matching submit clearly retires them
- submit recovery now retires superseded interrupted submit intents when a later
  successful submit covers the same bookmark identities on the same recorded
  remote, while `abort` uses that recorded remote for submit retraction and
  recorded repository identity for PR cleanup, fails closed when the named
  remote no longer points there, and still refuses automatic submit retraction
  once the recorded stack snapshot has been rewritten
- close intent reporting now uses the same recorded-stack model as submit, and a
  later successful close retires interrupted close intents when it clearly
  covers those changes; `close --cleanup` can supersede an older plain `close`,
  but plain `close` does not retire an older interrupted cleanup run
- a successful `close --cleanup` now retires interrupted `submit` intents only
  when the recorded bookmark artifacts on that submit's remote are gone and no
  live saved review state remains; retirement now also checks the recorded
  GitHub repository identity so repointed remote names fail closed, and closed
  cached PR metadata is treated as historical so `status` no longer reports
  stale submit records after that recovery path
- `close --cleanup` now ignores status-only saved bookmark pins when GitHub
  reports no pull request for a branch, so unsubmitted stacks that were merely
  inspected do not suddenly grow cleanup work
- fully untracked stacks now take the same close no-op fast path for
  `close --cleanup` as for plain `close`, skipping bookmark discovery when the
  selected stack has no saved review identity at all while still forcing the
  full cleanup path for stacks that retain any recorded review artifacts
- interrupted `cleanup --restack` state now records ordered commit IDs, reports
  the recorded stack by head change ID, and treats reruns as current-stack
  restacks rather than selector replay
- `doctor` now routes its output through the shared Rich-backed `ui` helpers and
  renders its check summary as a Rich table instead of hand-formatting padded
  ASCII columns
- the top-level CLI now accepts `--color=always|never|debug|auto` with the same
  override-vs-`ui.color` precedence as `jj`, and embedded native `jj log`
  rendering now honors that shared override instead of consulting `ui.color`
  separately

## Executable Surface

The product command surface should follow the design doc.

`land` now ships as a separate slice layered on top of the stable
submit/status/relink/cleanup lifecycle.

The tool itself should ship as a standalone executable, for example
`jj-review`.

During development inside this repo, the default invocation should be:

```text
uv run jj-review ...
```

Users may also configure `jj` aliases that delegate to the standalone
executable so that `jj review ...` works ergonomically. That alias layer should
be treated as convenience glue, not as a separate implementation surface.

For development workflows, the package may also be invoked as
`python -m jj_review`, but `uv run jj-review` should be the primary path.

Tests and packaging should target the standalone executable directly. Any `jj`
alias integration should stay thin and optional.

Packaging and release readiness are now part of the normal delivery surface:

- `pyproject.toml` carries the PyPI-facing metadata for the standalone package
- the repo root README documents the supported `uv` install, upgrade, editable
  tool-install, and built-wheel test flows
- distribution artifacts are built with `uv build`
- GitHub Actions publishes those artifacts through `uv publish`, using
  trusted-publishing environments for TestPyPI and PyPI rather than storing a
  long-lived upload token in repository secrets
- GitHub Actions and local compatibility probes install pinned `jj` release
  binaries with `tools/install-jj-release.sh` instead of building `jj` from
  source in CI

The standalone executable may also provide auxiliary shell-completion output
via `jj-review completion <bash|zsh|fish>`. That command is local CLI glue
only: it should render scripts from the argparse surface and should not
require repository bootstrap, saved local data, or GitHub access.

Top-level help should be curated rather than a flat dump of every subcommand.
Default `jj-review --help` and `jj-review help` output should group commands by
theme and keep advanced repair or shell-integration commands out of the default
view. The default top-level help should also hide advanced global options such
as `--repository`, `--config`, `--debug`, and `--time-output`. An explicit
`jj-review help --all` mode can expose the full command surface without
changing how the actual parser accepts commands. Command summaries and option
descriptions in help output should read as concise fragments rather than full
sentences, and should omit trailing periods. Each subcommand help page should
also start with a short descriptive paragraph explaining what the command does
and whether it only inspects state or may mutate jj-review data.

This slice now also treats `help` as hidden parser glue instead of a normal
top-level command listing. That keeps the default command list focused while
preserving `jj-review help` as an exact top-level-help entrypoint and
`jj-review help <command>` as the same command-specific help surface as
`jj-review <command> --help`.

Subcommand help now preserves blank-line paragraph breaks in command
descriptions and wrapped option prose so longer `--help` output stays readable
instead of collapsing into one paragraph.

`submit` now also supports explicit draft-state controls at the CLI boundary:
`--draft` / `--draft=new` creates newly opened PRs as drafts, `--draft=all`
also returns existing published PRs on the selected stack to draft, and
`--publish` marks existing draft PRs on the selected stack ready for review.

Command target selection should stay conservative at the CLI boundary:

- `submit`, `close`, `land`, and `cleanup --restack` may omit `<revset>` and
  default to the stack headed by `@-`
- omitted selectors should never silently target the working-copy commit; `@`
  remains explicit user intent
- `relink` and `unlink` still require an explicit `<revset>` because they are
  repair-only commands
- `status` may omit `<revset>` and inspect the current stack by default
- `import` may omit explicit selector flags and default to the current stack
  headed by `@-`, while still rejecting multiple selector flags

## Proposed Repository Layout

Slice 1 establishes the initial scaffold using a clean layout.

Proposed shape:

```text
pyproject.toml
uv.lock
src/
  jj_review/
    __init__.py
    cli.py
    config.py
    ...
    models/
    commands/
    jj/
    git/
    github/
    planning/
tests/
  unit/
  integration/
  live/
  fixtures/
tools/
  fake_github/
docs/
  mental-model.md
  daily-workflow.md
  troubleshooting.md
  internals/
```

The package name is `jj_review` for now.

## Main Components

### CLI Layer

The CLI layer should be thin. It should:

- parse command arguments
- load configuration
- initialize logging
- build command dependencies
- render user-facing output and diagnostics
- for `submit`, avoid echoing explicit operator input back at them: only print a
  selected-change banner when the head came from the default omitted selector,
  and include short
  change IDs in the selected and trunk summaries
- for inspection-style commands such as `status` and `cleanup`, print resolved
  local context promptly; `status` may buffer remote inspection long enough to
  render capped summaries before the trunk/base row. Interactive TTY runs
  should show explicit progress during that GitHub inspection instead of
  leaving the operator with a silent wait. `cleanup` may still stream per-item
  results as remote inspection completes
- for successful live `submit` runs, print the top-of-stack URL after the
  submitted bookmark summary so the operator can jump straight to the stack in
  a browser

It should not contain stack planning logic.

Bootstrap failures such as missing config files, invalid config syntax, or bad
stack selection should be surfaced as targeted CLI diagnostics rather than
Python tracebacks.

### JJ Adapter

The `jj` adapter should wrap subprocess access to `jj` and expose typed
operations such as:

- resolve a revset
- inspect the working-copy/default submit target
- enumerate the linear review chain
- read bookmarks plus tracked and untracked remote bookmark state
- surface stale-workspace errors distinctly so commands can suggest
  `jj workspace update-stale`

The adapter should prefer machine-readable template output over parsing human
text.

### Git Adapter

The Git adapter should be narrower than the `jj` adapter. We mainly need it for:

- backing repo inspection in tests
- remote branch verification
- fake GitHub server internals
- a few compatibility checks where Git is the actual remote boundary

### Planning Layer

The planning layer should be pure or as close to pure as possible. Given typed
local and remote state, it should decide:

- which changes are reviewable
- which bookmark each change should use
- which PR each change should map to
- which remote mutations are required
- which operations are hard errors

Reviewability should be computed from `jj` state, not reimplemented as
tool-local policy. In practice, that means the planner should respect the
repo's configured `immutable_heads()` boundary via `jj`'s `immutable()` /
`mutable()` semantics.

This is where most correctness should live.

### GitHub Client

The GitHub client should be a thin `httpx` wrapper plus typed `pydantic`
models.

It should know how to:

- fetch PR state
- batch PR lookup by known head branch where that avoids one-request-per-PR
- create PRs
- update PRs
- assign reviewers and labels
- manage reviewer-facing stack metadata
- perform any endpoint-specific pagination or retry behavior

When endpoint semantics allow it, the client and command layers should prefer
batched or bounded-parallel GitHub work over one-request-per-item serial loops.
Ordering constraints should still be explicit at the command layer when the
visible result needs a specific sequence.

It should not decide stack topology or branch naming policy.

### Config and Saved jj-review Data

The design doc now distinguishes user-authored config from machine-written
jj-review data.

For now:

- config should live in jj's normal config scopes under the `jj-review`
  namespace
- repo-specific defaults should use jj's built-in user/repo/workspace
  precedence instead of path-based conditional matching
- machine-written jj-review data should live in
  `~/.local/state/jj-review/repos/<repo-id>/state.json`
- `<repo-id>` should come from hashing the canonical `.jj/repo` storage path so
  every workspace for the same repo shares one state location without an extra
  bootstrap step
- reads should treat a missing state file or missing intent files as empty
  state, and writes should create parent directories on demand and fail only if
  the filesystem refuses the write

That jj-review data remains minimal, optional, and non-authoritative. The
implementation should model it as a small, versioned JSON state file validated
through `pydantic`. Human-authored config stays in TOML; machine-written state
and resumable intent files should not use hand-rolled parsing or rendering.

## Data Model

We should define `pydantic` models early and use them consistently across both
the real client and the fake server.

Important model families:

- local stack models
- bookmark and remote branch models
- GitHub PR and comment models
- mutation plan models
- config and jj-review data file models

Important persisted records should mirror the design doc's minimal jj-review
data:

- per-change pinned bookmark and GitHub PR link
- per-change reviewer-facing stack summary comment identifier, if used for the
  selected head PR

Repo defaults used for resolution belong in config, not in machine-written
jj-review data.

Command output and planning results should use first-class typed models.
Rendered output should be derived from those models rather than carrying ad hoc
dicts or stringly typed intermediate state through the command layer.

## Default Repo Resolution

For now, the common case should be zero-config. The tool should prefer
repo-derived defaults and only require explicit configuration when the repo is
ambiguous. This section extends the design doc's trunk-resolution requirement
into a full repository-resolution order.

The resolution order should be:

- selected remote: `origin` if it exists, then the only remote if exactly one
  exists, otherwise fail
- trunk branch: the selected remote's default branch if it can be found, then
  one remote bookmark on the selected remote that points at `trunk()`,
  otherwise fail
- GitHub host/owner/repo: derive from the selected remote URL, otherwise fail

Ambiguity should be a hard stop, not something the tool guesses past.

## Documenting Changes Before Coding

When we discover a design bug or a behavioral ambiguity, write down the
intended fix before implementing it.

Use these documents with a clear split:

- update `docs/internals/design.md` first if the change affects product
  behavior,
  persistence boundaries, invariants, or user-visible semantics
- update `docs/internals/implementation-strategy.md` if the change is
  primarily about execution strategy, staging, or component boundaries
- use the commit message to summarize what landed, not as the primary place
  where the design decision lives

For small bug fixes, a short targeted edit to the relevant section is enough.
We do not need a new note for every issue. The important thing is that the
canonical docs reflect the intended behavior before code starts depending on a
new assumption.

## Authentication

For now, the tool should resolve GitHub credentials in this order:

- `GH_TOKEN`, if set
- `GITHUB_TOKEN`, if set
- `gh auth token --hostname <resolved-github-host>`, if `gh` is installed and
  authenticated
- otherwise fail with an explicit authentication error

The application client should continue to use `httpx` directly for GitHub API
calls. If we reuse `gh` credentials, we should do so only through the supported
`gh auth token` command, not by reading `gh` config files, keychain entries, or
other internal storage directly.

## Tooling Strategy

The implementation should standardize on:

- `uv` for environment and dependency management
- `uv run` for local command execution
- `uv tool run` only where it clearly improves ergonomics
- `./check.py` as the default local verification entrypoint
- `pyrefly` for static type checking
- `ruff` for linting and formatting
- `pytest` for the test runner

## Testing Strategy

Testing is the center of the implementation strategy, not an afterthought.

For every user-visible behavior:

1. write tests first
2. implement against the local fake GitHub server
3. verify against the live GitHub test repository
4. keep the live behavior as the final arbiter

We should have three layers of tests:

- unit tests for parsing, planning, and model behavior
- local integration tests against the fake GitHub server and a real backing Git
  repo
- opt-in live tests against a genuine GitHub repository

Local tests should be the default.

The default local verification command should be:

```text
./check.py
```

That script should run `uv sync --locked`, then run `ruff check`, `pyrefly
check`, and `pytest -n auto` by default, with randomized test order so hidden
cross-test coupling is more likely to fail fast during normal local runs.

When the full suite gets slow enough to justify it, `./check.py -n 4` should
override the default worker count, while `./check.py -n 1` should provide a
serial escape hatch without changing the environment bootstrap, lint, and
type-check steps.

The check runner should also support an explicit concurrency-observability mode:

```text
./check.py --pytest-concurrency-report
```

That mode should keep the same bootstrap, lint, and type-check flow, then run
pytest with a local plugin that measures per-test wall-clock occupancy,
reports average and peak active-test counts, and highlights the tests that
contribute the most concurrency debt when the suite drops below the requested
worker count.

Coverage should be available as an explicit local verification mode:

```text
./check.py --coverage
```

That mode should keep the same `uv sync --locked`, lint, and type-check steps,
then run pytest with branch coverage enabled, emit a terminal missing-lines
report, and write an HTML report to `htmlcov/index.html` for deeper inspection
of untested code paths.

Live tests should require an explicit flag and explicit credentials.

## Fake GitHub Server Strategy

The fake GitHub server is a core part of the product development strategy.

It should:

- expose only the endpoints we currently need
- model GitHub behavior closely enough to exercise real client logic
- be backed by a real Git repository
- allow tests to assert directly on backing Git state after API calls
- evolve incrementally as new client features require more GitHub behavior

This is not a general-purpose GitHub emulator. It is a purpose-built contract
test harness for this tool.

The fake server should copy the shape and behavior of real GitHub only as far
as needed for the current slice of functionality.

We will use FastAPI for the fake server unless Starlette later proves to offer
a clear concrete advantage for this test harness.

## Fake GitHub Server Rules

To keep the fake server useful, we should follow a few rules:

- every endpoint should correspond to a real GitHub endpoint we expect the
  client to call
- fake behavior should be written to match observed GitHub behavior, not our
  preferred behavior
- when real GitHub behavior is surprising, tests should document that surprise
- if the fake server knowingly diverges from GitHub, the divergence must be
  called out in the tests and in the server code

The fake server should own a real Git repo because many assertions are about the
actual remote branch state, not just JSON responses.

## Fake GitHub Parity Tests

We should have tests for the fake GitHub layer itself to verify that its
behavior actually matches GitHub for the subset of functionality we rely on.

These tests should compare observable behavior, not implementation details. For
example:

- creating a PR creates the expected remote refs and returns the expected shape
  of JSON
- updating a PR changes the same fields GitHub changes and leaves alone the
  same fields GitHub leaves alone
- comment creation and update behave like GitHub for the endpoints we use
- branch and PR visibility in API responses match GitHub for the scenarios we
  cover

Where practical, parity tests should run the same client action once against
the fake server and once against a live throwaway GitHub repo, then compare the
resulting normalized observations.

## Live GitHub Test Strategy

The live suite should exist from early on, even if it is small.

The purpose of the live suite is not exhaustive coverage. Its purpose is to
catch fake-server drift and real-forge edge cases early.

The live suite should:

- run only when explicitly requested
- create a throwaway test repository per run
- use a dedicated namespace for temporary branches and PR artifacts
- clean up after itself as aggressively as practical
- avoid touching anything outside its namespace

The first pass should use:

```text
uv run pytest tests/live --live-github
GITHUB_TOKEN=...
JJR_GITHUB_TEST_REMOTE=origin
```

The live suite may use the `gh` CLI for throwaway repo setup and teardown if
that makes the tests materially simpler. We will not use `gh` in the main
application client.

## Development Workflow

Because we are building a stacked review tool, we should build it using stacked
review discipline.

That means:

- every implementation slice should be logically self-contained
- every commit should have a clear purpose and description
- tests for the slice should land with the slice
- any code change must pass its relevant tests before the commit is created
- docs should move with behavior, not weeks later

We should prefer a sequence like:

1. targeted design or strategy note update when behavior or assumptions change
2. failing tests
3. minimal implementation
4. cleanup/refactor if needed
5. final docs sync if user-facing behavior or usage changed

rather than:

1. large framework commit
2. large feature commit
3. delayed tests
4. delayed design clarification
5. delayed docs

## Delivery Plan

We should implement the tool in vertical slices.

### Slice 1: Initial Scaffold

Status: complete.

Deliver:

- `uv` project setup
- basic CLI skeleton
- logging and config bootstrap
- test runner setup
- fake server test harness bootstrap

Done when:

- `./check.py` works locally
- a trivial fake-server integration test passes

### Tooling Follow-Up: Coverage Reporting

Status: complete.

Implemented after the initial scaffold:

- `./check.py --coverage` now reuses the standard local verification flow while
  running pytest with branch coverage enabled
- the coverage run emits both a terminal missing-lines summary and an HTML
  report under `htmlcov/`

Done when:

- coverage-enabled local verification works without bypassing bootstrap, lint,
  or type checking
- developers can inspect uncovered lines from the terminal report or the HTML
  artifact

### Packaging and Distribution Readiness

Status: complete.

Deliver:

- PyPI-facing package metadata in `pyproject.toml`
- repo-root install and release documentation
- a GitHub Actions workflow that builds once, then publishes the exact built
  artifacts to TestPyPI or PyPI with `uv publish`

Implemented in the first vertical cut:

- the package metadata now includes the project README, keywords, classifiers,
  and repository URLs needed for a reasonable PyPI presentation
- the repo root now documents the supported `uv` flows for in-repo development,
  editable tool installs, built-wheel smoke tests, and PyPI installation
- `.github/workflows/release.yml` now runs `./check.py`, builds the sdist and
  wheel with `uv build`, and publishes the already-built artifacts through
  trusted publishing

Done when:

- `uv build` produces releasable sdist and wheel artifacts from the repo root
- the repo documents both local development and PATH-based testing flows
- the release workflow can publish the same built artifacts to TestPyPI or PyPI

### Slice 2: Local Stack Discovery

Status: complete.

Deliver:

- typed `jj` command wrapper
- linear stack discovery from a selected head back to `trunk()`
- path-local validation of the selected stack, so off-path reviewable children
  are treated as separate stacks instead of blocking selected-stack commands
- rejection of unsupported graph shapes
- fail-closed handling for `trunk()` resolving to `root()`
- rejection of immutable revisions while walking the stack

Done when:

- stack discovery behavior is covered by unit and integration tests
- unsupported shapes fail with explicit diagnostics

### Slice 3: Bookmark Resolution and Saved jj-review Data

Status: complete.

Deliver:

- bookmark naming policy
- bookmark pinning in machine-written jj-review data
- minimal jj-review data model and persistence
- separation between human config and machine-written jj-review data

Done when:

- tests prove "generate once, then pin"
- subject changes do not churn bookmark names
- config and jj-review data no longer live in a workspace-root sidecar file
- repo ID lookup failures fall back to generated bookmarks without persisted
  state

### Slice 4: Remote Branch Sync

Status: complete.

Deliver:

- push/move bookmarks
- detect tracked-remote and remote branch state
- verify actual Git remote state in tests

Done when:

- tests assert on the backing Git repo after client actions
- no-op detection respects topology changes as well as content changes,
  including matching untracked remote bookmarks
- submit can update an existing untracked remote bookmark without creating a
  local bookmark conflict first

### Slice 5: PR Create and Update

Status: core complete.

Deliver:

- PR lookup
- PR creation
- PR updates
- trunk branch resolution
- assign configured reviewers and labels

Implemented in a follow-up:

- `submit` now also supports `--dry-run`, which resolves the stack, bookmark
  actions, push actions, and PR actions through the normal submit path while
  skipping local, remote, GitHub, saved-data, and intent-file mutations
- `submit --dry-run` now also skips stack-comment GitHub reads because the
  dry-run plan does not surface stack-comment actions and does not update
  cached comment IDs, which trims avoidable per-PR latency from planning
- `submit --dry-run` now also stays fully local for never-tracked stacks when
  the trunk remote bookmark is already unambiguous, deriving `new PR` actions
  and the trunk branch from local jj state instead of paying GitHub repository
  and pull-request discovery round trips just to confirm an all-new plan
- submit preparation now reuses one batched `jj bookmark list --all-remotes`
  snapshot across bookmark rediscovery, per-revision planning, and local-only
  dry-run decisions instead of spawning one extra bookmark query per revision
- submit now also pre-renders the native `jj log` rows for the final stack
  summary in parallel before printing, so large stacks no longer pay one `jj`
  subprocess startup per displayed row
- `submit` now accepts `--draft=all` to return already-published PRs on the
  selected stack to draft while keeping plain `--draft` as the conservative
  "new PRs only" mode
- `submit` now accepts `--reviewers` and `--team-reviewers` as one-shot
  overrides for the configured reviewer defaults
- `submit` now accepts `--re-request` to request review again from users
  whose latest review on the pull request is `APPROVED` or
  `CHANGES_REQUESTED`, while leaving still-pending review requests alone
- `submit` now accepts `-d` / `--describe-with` to invoke one external helper
  as `helper --pr <change_id>` for each pull request and `helper --stack
  <selected-revset>` once for stack-comment prose; the helper returns JSON
  `title` / `body` fields and invalid helper output aborts submit before any
  local, remote, or GitHub mutation
- stack helper invocation is skipped when the selected stack contains only one
  change, because no stack summary comment will be created in that case
- the submit CLI now prints the selected revset and remote promptly, then
  renders the final ordered review summary once the submit phases complete,
  instead of trying to stream per-revision mutation progress inline
- submit and status now share the same native `jj log` row rendering helper,
  so submit shows the stack tip-first with concise submit-result text on the
  first rendered line and the resolved trunk row beneath the stack
- the per-change submit summary now renders created PRs as `PR #n` in live
  output and `new PR` in dry-run output; updates append plain-text `pushed`
  or `already pushed` status ahead of the PR result when relevant
- top-level CLI failures now print with an `Error:` prefix for clearer command-
  line diagnostics while preserving plain `Interrupted.` for Ctrl-C handling
- explicit missing revsets now preserve jj's own wording, for example
  `Error: Revision \`xporz\` doesn't exist`, instead of surfacing the wrapped
  `jj log ... failed:` command string

Done when:

- submit works end-to-end against the fake server
- a minimal live GitHub submit test passes

### Slice 6: Reviewer-Facing Stack Metadata

Status: complete.

Deliver:

- dedicated bot comment support
- comment creation immediately after PR creation
- regeneration on every submit
- caching of comment identifiers if needed

Implemented with one dedicated PR comment only on the selected head change
when the selected stack contains more than one change, marked so `submit` can
rediscover it when saved comment IDs are missing. The comment body is
regenerated from the current submitted stack on every run and is never used as
the source of truth for topology. Re-submitting a taller selected stack moves
that managed comment to the new selected head, and single-review-unit submits
remove any older managed stack comment that no longer applies.

Single-review-unit submits are treated as plain PRs rather than stacks: they
skip the stack summary comment entirely, and `--describe-with` does not invoke
the stack helper for that case.

Default PR descriptions now also fall back to the commit subject when the
commit message has no body. That keeps GitHub PR pages from starting with a
blank opening comment for one-line commit descriptions while preserving the
existing "title from subject, body from remaining description" mapping when a
real body is present.

The same stack summary comment now also accepts optional generated
introductory text from `submit --describe-with`; that prose is rendered ahead
of the standard full-stack navigation block so the product still owns only one
reviewer-facing stack summary comment for the selected head PR. That block
renders the full stack from top to bottom, bolds the selected head PR title,
links the other PR titles, and shows a plain resolved trunk line beneath the
bottom-most PR.

The CLI-facing submit summary lines now use shared Rich row helpers for the
selected change, fallback trunk row, and top-of-stack URL so hanging-indent
behavior stays aligned with cleanup and abort.

When `submit` invokes a stack helper, it now also writes a temporary input file
with the already-generated PR title/body pairs plus compact per-PR diffstat
context and points the helper at that file via `JJ_REVIEW_STACK_INPUT_FILE`.
That lets example helpers summarize the stack from reviewer-facing PR metadata
instead of replaying the full raw stack patch into the model.

The repo now also includes three no-dependency example helpers in `scripts/`:

- `describe_with_prompt.py` for interactive local entry on a TTY
- `describe_with_claude.py` for Claude Code
- `describe_with_codex.py` for Codex CLI

Done when:

- tests prove the stack metadata is regenerated from current `jj` state
- tests prove stack metadata is not used as topology source

### Slice 7: Status and Relink

Status: done.

Implemented in the first vertical cut:

- `status` now reports local bookmark resolution together with any discovered
  remote and GitHub PR link, while still falling back to local-only output when
  the repo is not configured well enough for remote inspection
- `status` now prints the selected revset and remote immediately from local
  state, then streams per-change summaries in display order once GitHub
  inspection starts instead of waiting for a fully buffered status object
- local stack discovery now fetches head ancestors and their immediate
  children in bulk `jj log` queries instead of walking one parent at a time,
  which significantly reduces status startup latency on deeper stacks
- `status` now renders the stack's base parent as a footer row beneath the
  stack, using the same native `jj log` rendering path as the rest of the
  stack; when the bottom selected change sits directly on trunk that footer is
  `trunk()`, and when the stack forks from a recent trunk ancestor the footer
  is that fork-point parent instead
- the CLI now supports `--time-output` as a global debugging aid that prefixes
  printed lines with elapsed time from process start
- `status` now inspects per-change GitHub PR link with bounded concurrency on
  one shared client with bounded concurrency
- `status` now batches initial PR discovery by known head branch through the
  GitHub GraphQL lookup path instead of issuing one REST list call per change
- `status` now derives repo-level GitHub availability from the first real PR
  lookup instead of blocking on a separate repository probe before streaming
  output
- local-only and fallback `status` rendering now batches bookmark-state reads
  into one `jj bookmark list` call instead of reloading one bookmark at a time
- `status` now also supports `--fetch` / `-f` to refresh remote bookmark
  observations first when the user wants a freshly fetched view before live
  GitHub inspection
- `status` advisory and interrupted-operation notices now use the shared `ui`
  rendering path, so selected revsets, change IDs, and command examples wrap
  through Rich instead of manual 80-column fills
- submit and `status` now persist each change's last-known PR state, and
  `status` uses that saved state to render more informative offline fallback
  summaries
- successful live `status` runs now refresh the saved PR link too, so a later
  offline run can still show last-known review identity for previously
  inspected changes
- that `status` saved-data refresh is now bidirectional: live observations
  update open and closed PR state, and clear the saved PR link when GitHub reports
  that the branch no longer has a PR
- `status` now also distinguishes merged PRs from merely closed ones and
  derives a lightweight review decision for open PRs from GitHub reviews so
  the stack summary can show approval and change-request state
- `status` now batches that review-decision lookup across open PRs through the
  GitHub GraphQL path instead of issuing one review-list request per PR
- `status` now treats ambiguous GitHub PR link and ambiguous stack summary
  comments as incomplete inspection, so the command exits non-zero instead of
  presenting those cases as healthy output
- `status` now also prints explicit repair guidance for stale or ambiguous PR
  link so operators who bounced between machines can rerun `status --fetch`
  and use `relink` intentionally instead of guessing
- `status` now also treats remote-resolution and GitHub-target fallback output
  as incomplete inspection, so local-only summaries exit non-zero when live
  inspection could not be completed
- GitHub client list endpoints now follow pagination links through one shared
  helper so status and relink do not silently truncate multi-page remote state
- `relink` now resolves one explicit PR number or URL against the configured
  repository, verifies that the PR is open on a same-repository head branch,
  pins that branch locally for the selected change, and persists the PR
  link so a later submit can update the relinked review intentionally
- `relink` now also fails closed on GitHub lookup errors instead of surfacing
  uncaught transport exceptions through the CLI
- `relink` now also refuses to steal an already-bound local bookmark from
  another revision when saved local data is missing or stale
- slice coverage now exercises `status --fetch` as a real remote-rediscovery
  path and covers explicit `relink` failure cases such as missing PRs, closed
  PRs, cross-repository heads, and missing remote head branches

Deliver:

- `status`
- explicit `relink`

Done when:

- damaged link fails closed in `submit`
- `relink` can attach an existing PR intentionally

### Slice 8: Cleanup

Status: done.

Implemented in the first vertical cut:

- `cleanup` now reports repo-scoped saved-data cleanup actions before it
  mutates anything, including stale saved change records, removable stack
  summary comments on stale PRs, and stale remote pull request branches
- `cleanup` now performs the safe subset of those actions by default, while
  `cleanup --dry-run` keeps the same plan rendering without mutating; the
  mutating path prunes saved change entries that no longer resolve to
  supported local review stacks, deletes stack summary comments only for
  unlinked or detached review stacks, and deletes stale remote pull request
  branches only when the remote branch is unambiguous and no local bookmark
  still owns it
- stale saved entries now avoid extra GitHub stack-summary-comment inspection
  unless saved local data suggests comment cleanup could still produce an
  action, such as a saved stack summary comment or a missing remote branch
  that suggests the PR may now be unlinked
- cleanup now overlaps the remaining GitHub stack-summary-comment inspection with
  bounded concurrency while still applying any resulting mutations in the
  original saved-entry order
- remote-branch cleanup remains conservative and fail-closed: conflicted
  remote branches and still-present local bookmarks are surfaced as blocked
  cleanup items instead of being deleted automatically
- the fake GitHub server and GitHub client now support stack-comment deletion
  so cleanup can exercise reviewer-facing metadata removal end-to-end in the
  default integration suite

Deliver:

- stale saved-data cleanup
- stale reviewer-facing metadata cleanup
- conservative remote branch cleanup

Done when:

- cleanup reports planned actions clearly
- ambiguous remote deletions are not automatic

### Slice 9: Submit Throughput

Status: done.

Implemented in the first vertical cut:

- `submit` now batches pull-request discovery by head branch through the
  GitHub GraphQL lookup path instead of issuing one REST list call per review
  unit
- `submit` now batches ordinary `jj git push --bookmark ...` updates into one
  remote push when the selected pull request branches can use the normal tracked
  bookmark path, while still handling untracked remote-bookmark lease updates
  conservatively one branch at a time
- once remote pull request branches are in place, submit now syncs PR
  create/update
  work with bounded concurrency, stops launching new PR work after the first
  failure, drains already-started tasks, checkpoints each successful in-flight
  PR sync, and reconciles configured reviewers and labels when saved-data
  checkpointing failed partway through so reruns can converge instead of getting
  stuck half-finished
- submit-side stack-comment inspection and upsert planning now run with
  bounded concurrency, stop launching new work after the first failure, and
  checkpoint successful in-flight comment updates before surfacing the error
- the fake GitHub server now implements the GraphQL head-ref lookup path so
  the default integration suite exercises the same batched submit discovery
  flow as the real client

Deliver:

- batched submit PR discovery
- batched ordinary submit pushes
- bounded-concurrency submit PR sync
- bounded-concurrency submit stack-comment inspection

Done when:

- submit no longer performs one PR lookup request per change
- submit preserves fail-closed PR link checks under batched discovery
- submit still checkpoints saved local data after each completed PR sync

### Slice 10: Merged PR Reconciliation

Status: done.

Deliver:

- persist each change's last submitted local `commit_id` in saved local data on
  successful `submit`
- teach `status` and `status --fetch` to inspect the selected local stack even
  after fetching merged PR branches has created immutable or divergent side
  revisions
- render merged changes on the selected stack as cleanup needed instead of
  treating normal
  fetched GitHub merge state as a broken stack
- make `status` explain why cleanup is needed, warn that descendant submit
  operations still follow the old local ancestry, and print the exact
  `cleanup --restack` next step
- diagnose merged PRs whose base branch matches `review/*` as a GitHub policy
  problem instead of presenting them as a mysterious stack failure
- add `cleanup --restack` as the explicit opt-in local rewrite path for merged
  ancestors
- let default `cleanup --restack` perform only rebases of remaining changes
  whose destination is `trunk()`
- require `--allow-nontrunk-rebase` or manual `jj rebase` before restacking
  surviving descendants onto another surviving local review base
- keep using the selected local stack rather than fetched branch-tip commits for
  merged non-trunk PRs
- leave merged or side-copy artifacts alone unless some later cleanup pass can
  prove they are stale and removable

Done when:

- merge commit, squash merge, and rebase merge all show a usable status view
  after fetch because inspection follows the selected local stack instead of
  failing on fetched branch artifacts
- the default status output tells the operator what `cleanup needed` means and
  what command to run next instead of making them infer the repair flow
- `cleanup --restack` restores one linear local stack by removing merged
  changes from active local ancestry, while blocking non-trunk rebases of
  remaining changes unless the operator opts in explicitly
- fetched branch-tip commits for merged non-trunk PRs are treated as fetched
  saved GitHub observations, not as the canonical continuation of the local stack
- automatic local rewrites fail closed only when the selected stack or PR
  link is truly ambiguous, or when removing a merged stack change would
  discard unpublished local edits
- tests cover the common fetched-merge case, safe restacking of remaining
  changes, and the
  refusal cases that still require human intervention

### Landing

`land` is now implemented as its own slice because merge policy, branch
protection, and partial-stack semantics materially expand the product surface.

The CLI contract should stay consistent with the rest of the tool:

- `jj review land [--dry-run] [--pull-request <pr> | <revset>]`
- mutate by default, with `--dry-run` available for inspection
- local-path-first target selection, with `--pull-request` as an alternate way
  to select the linked local change that defines the landing head

The first implementation decision must be the landing unit. The design doc now
defines that as the consecutive changes from `trunk()` that can be landed now.
The implementation should preserve that exact contract instead of accepting
arbitrary PR subsets.

The default landing boundary is now readiness-based instead of merely
open-PR-based. In the first readiness slice, that means the prefix stops at the
first PR that is draft, unapproved, or has changes requested, while still
failing closed on ambiguous linkage. A narrow
`--bypass-readiness` flag may ignore only those readiness gates; it must not
skip linkage safety or trunk push policy.

The command also needs explicit phase boundaries so retries are idempotent:

1. resolve the selected local stack, the first change that blocks landing,
   trunk target, and GitHub PR link
2. if `--dry-run` is set, stop after rendering that computed plan
3. replay the changes that can be landed now onto trunk locally in `jj`,
   preserving them as a stack of commits, then push the resulting trunk tip
   with a lease
4. only after that succeeds, update saved jj-review data, finalize the exact
   landed PRs, and forget the landed local `review/*` bookmarks when they
   still point at the landed commits
5. leave broader saved-data pruning and stale-review cleanup to `cleanup`

Error handling should stay specific instead of collapsing everything into one
generic recovery path:

- link problems should point to `status --fetch` / `relink`
- local ancestry repair should point to `cleanup --restack`
- land now performs the local "stack must be based on trunk()" check before
  status preparation refreshes remote state, so obviously unlandable stacks
  fail fast instead of paying the normal remote refresh cost first
- policy or branch-protection failures should stop immediately with no fallback
- interrupted runs should either resume exact post-push bookkeeping or
  recompute from the current stack instead of assuming an earlier preview is
  still authoritative

This slice is now in place with the current implementation:

- preview output clearly identifies the changes that can be landed now, the
  target trunk, and the first change that blocks landing on the selected stack
- default `land` selection now uses the ready prefix rather than the merely
  open prefix, so draft, unapproved, and changes-requested PRs block the
  landing boundary just like closed or ambiguous PR state already did
- `land` also stops on local changes that still carry unresolved conflicts,
  even after an otherwise valid rebase, so the ready prefix remains something
  that can actually be replayed onto trunk
- `land` compares each landable change's local diff against the diff of the
  commit currently on its `review/*` branch: tree-equivalent rebases trigger
  a pre-land refresh push that realigns the PR branch with the local commit,
  while content-divergent rebases (conflict resolution, amends) block with a
  pointer to `submit` and re-request review
- blocked `land` diagnostics now check unlinked, ambiguous, and missing review
  state before rewrite drift, so the command points at `relink` or
  `status --fetch` when the PR link itself is the real problem instead of
  defaulting to a misleading `submit` rerun
- `land` stack preparation now re-resolves the selected stack after its
  remote-state fetch can move `trunk()`, so the command stops on the actual
  "stack is no longer based on current trunk" condition instead of surfacing a
  bogus local bookmark mismatch against pre-fetch state
- `land` now picks the repair command for that trunk-drift case: it points at
  `cleanup --restack` when the selected stack already contains merged review
  changes, and otherwise gives a concrete `jj rebase -s <bottom> -d 'trunk()'`
  command for whole-stack drift
- after a pre-land refresh push succeeds, `land` re-queries the affected PR
  approval decisions and stops before the trunk push if any approval was
  dismissed by the refresh (e.g., because the repo policy dismisses stale
  approvals); the refreshed `review/*` branch stays in place for the
  operator to re-request review and retry
- `land --dry-run` surfaces the planned refresh-then-push sequence without
  mutating any remote state
- `land --bypass-readiness` may still select the open prefix for exceptional
  cases, but only by bypassing readiness checks; linkage and trunk-protection
  checks still fail closed
- blocked `land` output does not advertise `--bypass-readiness`; operators may
  discover that override from help, but normal failure guidance stays focused
  on the blocking state itself
- land now constructs the landed trunk history locally in `jj`, preserving the
  landed changes as multiple commits, then updates trunk with a leased push
- if the trunk push fails or is interrupted after the local bookmark move, the
  local trunk bookmark is restored before the command exits so a rejected
  remote update does not silently rewrite local trunk state
- the local-trunk-first landing path is intentional, so trunk protection and
  trunk checks gate the push while `review/*` protection only prevents direct
  review-branch merges
- review-only `review/*` branches are not themselves merged directly
- successful `land` now forgets local `review/*` bookmarks for the exact
  landed prefix by default, while `--skip-cleanup` retains those local
  bookmarks for exceptional cases
- surviving descendants above the landed changes are left for follow-up
  `cleanup --restack` and `submit`, rather than being silently retargeted or
  restacked during `land`
- post-land PR finalization for the landed changes happens bottom-to-top, even
  if surrounding GitHub lookups or other independent work use batching or
  bounded parallelism
- interrupted applies now resume from persisted land intent data, including the
  already-landed trunk target, the landed change prefix, and per-PR completion
  checkpoints, so reruns can finish post-push bookkeeping without
  rediscovering the original landing set, while stale pre-push intents are
  ignored when the current landable prefix has changed
- interrupted `land` messaging now identifies the recorded landing stack by
  head change ID plus selector origin, instead of presenting resume notices as
  bare `land on <revset>` replay
- exact post-landing bookkeeping is limited to the changes landed in that run,
  including local bookmark cleanup for that landed prefix, while broader
  stale-state cleanup remains a separate `cleanup` concern

### Stack Import

Cross-machine bootstrap and remote-stack materialization are implemented
separately from both read-only refresh and local ancestry repair.

The CLI contract is:

- `jj review import [--fetch] [--pull-request <pr> | --revset <revset>]`
- omitting selector flags defaults to the current stack headed by `@-`
- no overloaded positional selector that could mean either a revset or a PR
- no implicit workspace motion in the default mode

The product-level split is:

- `status --fetch` refreshes remote observations and GitHub PR state without
  mutating local bookmarks or the workspace
- `import` sets up saved local jj-review data for one exact stack
- `cleanup --restack` remains the local-history repair path after merges or
  other ancestry damage

The implementation uses explicit rules for what `import` may mutate:

- without `--fetch`, use only locally available commits and a remembered
  pull-request match for the selected stack
- with `--fetch`, refresh remote bookmark state and, for `--pull-request`,
  fetch only the needed branches for the selected stack so an existing
  reviewed stack can be bootstrapped on a new machine
- refresh saved data only for the selected stack
- create or refresh local bookmarks only when the target is
  exact and unambiguous
- fail closed if any imported revision would require inventing a new generated
  bookmark rather than reusing exact remote identity
- `--revset` imports without a selected remote fail closed when the selected
  stack would need generated bookmark identity; only exact saved or discovered
  bookmark names may be imported
- do not rewrite commits, restack descendants, or mutate GitHub state
- do not update the current workspace to the fetched tip automatically
- when `--fetch` imports a remote-selected stack, print the fetched tip commit
  so the operator can `jj new` from there if desired

This slice is done when:

- a user can bootstrap an existing review stack on a new machine from an
  explicit PR selector with `--fetch`
- remote-only pull request branches can be imported into saved local data
  with `--fetch` and without inventing topology from saved data
- bookmark conflicts, ambiguous PR matches, and unsupported stack
  shapes fail with targeted recovery guidance
- rerunning `import` on an already-imported stack reports that local jj-review
  tracking is already up to date instead of claiming the stack is empty
- import output always reports GitHub availability explicitly, even when no
  selected remote or repository target is available
- default-current-stack import failures are explicit when the current stack has
  no matching remote pull request
- default-current-stack import rejects that missing-link case from fetched
  bookmark state before waiting on GitHub inspection
- import distinguishes a missing saved remote bookmark from a stale saved
  bookmark target so the repair path is easier to diagnose
- import prints a brief progress note before live GitHub inspection so deep
  stacks do not look hung while status resolution is in flight
- stale saved local data is refreshed only when fetched link for the exact
  selected stack is unambiguous; otherwise import fails closed with targeted
  conflict guidance
- `import --revset` does not synthesize bookmark names when no remote is
  selected
- remote-selected import without `--fetch` fails with targeted guidance to
  rerun with `import --fetch` when the necessary branch is not already
  present locally

Backlog should keep repo-scoped `sync` as a separate question. This slice
solves explicit import/materialization, not whole-repo refresh policy.

### Close

`close` is implemented. The normal user-facing "stop review for this stack"
flow closes the open PRs `jj-review` is already tracking for the selected local
stack, and `close --cleanup` extends that with conservative cleanup of pull
request branches, local bookmarks, stack summary comments, and stale saved data
entries when the tool can verify they belong to the stack.

The CLI contract is:

- `jj review close [--cleanup] [--dry-run] [--pull-request <pr> | <revset>]`

The product split should stay explicit:

- `close` operates on the selected local stack and closes the open PRs
  `jj-review` is already tracking there
- `close --cleanup` performs conservative branch and metadata cleanup after
  the PR close succeeds
- `--pull-request <pr>` is only an alternate way to select the linked local
  change whose stack should be closed; it is not a GitHub-first mode

The `close` slice needs clear apply-phase and verification rules:

- mutate by default, with `--dry-run` available for inspection
- without `--cleanup`, close open PRs and retire active local jj-review data
  only, while skipping already-merged or already-closed PRs on the stack
- with `--cleanup`, delete remote pull request branches, forget local
  bookmarks, delete stack summary comments, and prune stale jj-review metadata
  only when the tool can verify they belong to the selected stack on the
  configured target remote
- controlled blocked exits retire their close intent instead of leaving a
  stale "interrupted" notice behind, while still checkpointing any earlier
  saved-data updates that already succeeded on the same path
- when a PR has already disappeared, saved stack-summary-comment cleanup must
  re-check comment identity by comment ID before deleting anything
- fail closed on ambiguous link or ambiguous branch identity instead of
  guessing what should be deleted
- reruns should be idempotent, so a second `close` or `close --cleanup`
  performs only the remaining safe work

Done when:

- `close` can preview and then close the open PRs `jj-review` is already
  tracking for one selected local stack
- `close --cleanup` can also delete pull request branches and retire local
  review artifacts without crossing ambiguous identity boundaries
- close reruns skip already-finished PRs and only perform any remaining safe
  cleanup work

### Unlink

`unlink` is implemented as the low-level repair-oriented inverse of `relink`.

The CLI contract should be:

- `jj review unlink <revset>`

The state model needs to stay explicit about what is durable operator intent
versus saved local data:

- clearing saved PR fields is not enough
- unlink writes a durable unlinked marker for the selected change
- rerunning unlink is idempotent and should succeed as a no-op
- unlinking a change with no active tracking should fail instead of
  creating unlinked state for a never-linked change

`unlink` keeps the unlinked-state precedence rule. Once a change is explicitly
unlinked, that unlinked record must override every other proof that the change
is still being tracked:

- local bookmarks
- saved PR link
- discovered GitHub PR link for the same head branch

That means the implementation cannot treat a preserved local bookmark as
sufficient proof of active tracking once unlinked state exists.

This slice is now in place with the current implementation:

- `unlink` detaches one explicitly selected local change without mutating
  GitHub
- unlink clears active PR and stack-summary-comment link, preserves any known
  bookmark, and records durable unlinked state in saved local data
- rerunning unlink for an already-unlinked change succeeds as a no-op, while
  unlinking a never-linked change fails with targeted guidance
- `status --fetch` surfaces unlinked bookmarks and unlinked PRs without
  repopulating active tracked state
- `import` may restore local bookmark state for unlinked changes, but it keeps
  the durable unlinked marker and does not repopulate active PR tracking
- `submit` now refuses unlinked changes until `relink` clears the unlinked
  marker, and `relink` reactivates the link when it succeeds
- `land` blocks unlinked changes as not safely landable through the normal
  `jj-review` flow
- cleanup treats unlinked state as a valid reason to remove stack summary
  comments and continues to prune unlinked markers once their `change_id` no
  longer resolves locally

Done when:

- unlinking one selected change clears active link and records unlinked state
- `status --fetch` surfaces unlinked state without repopulating active link
- `status` reports preserved local bookmarks as unlinked bookmarks when
  unlinked state still exists
- `submit` refuses to reuse unlinked state until `relink` clears it
- `land` rejects unlinked changes as not safely landable
- cleanup prunes unlinked markers whose `change_id` no longer resolves in
  visible history

### Status Rendering Follow-up

Status: done.

- `status` summary sections now render each displayed revision through a
  direct `jj log` call instead of rebuilding commit lines inside
  `jj-review`
- the base-parent footer now uses that same native `jj log` rendering path, so
  status no longer special-cases the base row format or conflates it with the
  actual resolved `trunk()`
- the first rendered `jj log` line now carries the appended review status
  suffix, while any additional lines from the user's configured log template
  stay unchanged
- embedded `jj log` rendering now resolves `ui.color` once up front and maps
  `auto` against `jj-review`'s actual stdout TTY, so color output matches the
  user's intent even though `jj` itself is writing into a subprocess pipe
- `ReviewStatusRevision` now carries the exact `commit_id` needed for that
  per-revision native rendering path

### Cleanup Follow-up

Status: done.

- `cleanup` now plans local `review/*` bookmark removal for stale tracked
  changes, instead of only pruning saved state and then reporting remote
  review-branch deletion as blocked
- `cleanup` now also forgets orphaned local `review/*` bookmarks from older
  runs when they are no longer reviewable or no longer belong to any
  supported review stack, even if the saved local jj-review state entry is
  already gone
- when that local bookmark forget is safe and planned, the paired remote
  review-branch deletion is now planned in the same pass instead of blocked on
  the still-present local bookmark
- `cleanup` now batches planned remote review-branch deletions into one push,
  batches planned local bookmark forgets into one `jj bookmark forget`, and
  refreshes remembered remote state with one fetch after those mutations
- stale local change detection now resolves cached `change_id`s in bulk and
  checks supported-stack membership from one ancestor/child graph walk instead
  of running separate `jj` stack discovery for each cached change
- `cleanup` now preserves reviewer-facing stack summary comments on closed PRs;
  comment cleanup is limited to explicitly unlinked or otherwise detached
  review stacks where the comment no longer describes the live branch linkage
- local bookmark cleanup stays conservative: conflicted bookmarks remain
  blocked, and bookmarks that no longer point at the last submitted commit
  stay blocked rather than being forgotten automatically

## Error Handling Strategy

Errors should be explicit and actionable.

The user-visible fail-closed cases are defined in the design doc. The
implementation should classify them cleanly and surface targeted recovery
actions.

We should distinguish between:

- user/actionable errors
- unsupported-shape errors
- remote state conflicts
- fake-server parity failures
- tool bugs

When possible, diagnostics should point to the exact recovery action:

- `jj review status --fetch`
- `jj review relink`
- `jj review close`
- `jj rebase`
- `jj review cleanup`
- `jj workspace update-stale`

Unreadable or partially written machine-written jj-review data should be
treated as missing saved data with one warning, then commands should fall back to
rediscovery where the design allows that to happen safely.

## Observability

We should make the tool easy to debug without making normal output noisy.

Recommended defaults:

- concise user-facing output by default
- debug logging behind a flag
- request/response logging in debug mode with token redaction
- enough plan logging to explain why a change is being created, updated,
  skipped, or rejected
- a shared Rich-backed `ui` module now exists for migrated command output and
  future styled rendering, while legacy `print`-based commands still use the
  existing `--time-output` shim until their output paths are moved over
- Rich-authored output now loads jj's effective `colors.*` config through
  `jj config list --include-defaults colors -T ...`, resolves matching label
  sets with the same parent-label inheritance jj documents for composite color
  rules, and exposes those styles to future Rich rendering without inventing a
  second semantic color system
- `--time-output` now uses those imported jj semantic styles for its elapsed
  prefix, labeling it as `prefix` plus `timestamp` so existing jj color
  customization carries over to Rich-authored timing output
- abort action output now renders status markers and messages as structured
  Rich rows instead of plain strings, which preserves hanging indents under
  terminal wrapping and allows per-status semantic styling for the marker and
  message text
- interrupted-submit recovery now treats the recorded remote name plus GitHub
  repository identity as part of the recovery key: cleanup retirement, abort
  retraction, and rerun guidance all fail closed when the current target no
  longer matches the recorded submit target, while later submits only count as
  exact continuations when that recorded target still matches
- close cleanup retirement also treats a surviving local `review/*` bookmark as
  live cleanup state, so an interrupted submit record now stays visible until
  both local and remote review artifacts for that recorded submit are gone
- cleanup and cleanup --restack now use the same structured Rich row pattern
  for streamed action output, including semantic highlighting for bookmarks and
  short change IDs in the action text
- CLI-authored status markers now avoid square-bracket tags in favor of
  parenthetical change IDs and plain status labels so Rich markup can be
  enabled later without escaping repo-authored output strings

Tests should primarily assert on typed plan objects. Snapshot tests should be
used sparingly for user-facing rendered output where the exact textual shape is
part of the contract.

## Definition of Done

A feature slice is done only when all of the following are true:

- tests were written first or at least before the behavior was finalized
- the local default suite passes
- relevant live GitHub tests pass
- docs are updated if user-visible behavior changed
- the implementation lands as a logical stacked-review-quality commit

Any commit that changes code must be made only after the relevant tests for that
change are passing.

## Bottom Line

We should optimize for a tight loop:

- write a failing test
- implement the smallest real slice against the fake GitHub server
- verify the slice against real GitHub
- land it as a clean stacked commit

If we keep the `jj` DAG as the source of truth, keep the GitHub layer narrow,
and keep the fake server honest by regularly checking it against real GitHub,
the implementation should stay understandable and correct as it grows.
