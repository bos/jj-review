# Testing philosophy

When adding, removing, or evaluating tests in this repo, optimize for tests
that protect real failures.

## What makes a test worthwhile

A test is worthwhile only if it protects at least one of:

- a user-visible behavior that would matter if broken
- a hard constraint from `jj`, GitHub, subprocess execution, or local
  persistence
- a realistic regression or failure mode
- a core invariant listed in [AGENTS.md](../../AGENTS.md)

Do not treat repo-authored docs, comments, or existing tests as sufficient
justification by themselves. They are hints, not proof that something is worth
testing.

Before adding a test, identify:

- what regression it would catch
- why that regression matters in practice
- why this is the right layer to test it

If you cannot answer those clearly, do not add the test.

## Choosing the right layer

Prefer tests at the narrowest layer that still exercises meaningful behavior.
Prefer one strong behavior test over many shallow plumbing tests.

Pick the layer that best protects the real risk:

- use integration tests for behavior that depends on the `jj`/GitHub/persistence
  boundary
- use unit tests for nontrivial domain logic and failure handling
- keep CLI smoke coverage, but do not exhaustively test parser forwarding or
  presentation glue

## Low-value test patterns

Avoid tests that primarily:

- pin exact wording, formatting, headings, or help output
- assert that a thin wrapper forwards arguments to a mocked helper
- restate private implementation details
- duplicate coverage already provided at a more meaningful layer
- snapshot generated text or scripts when only general behavior matters

## Deleting and consolidating tests

When in doubt, bias toward fewer, higher-signal tests. Removing or
consolidating low-value tests is encouraged when behavior remains well covered.

For file-by-file audits of unit tests, use
[docs/notes/unit-test-review-checklist.md](unit-test-review-checklist.md).
