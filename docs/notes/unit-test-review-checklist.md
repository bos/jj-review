# Unit test review checklist

Use this checklist when reviewing existing unit tests or deciding whether a new
unit test belongs in this repo.

## Core questions

For each unit test, ask:

- What real regression would this catch?
- Why would that matter to a user or to a hard system constraint?
- Is unit level the narrowest layer that still matches the real risk?
- Does the test assert a meaningful outcome, or only an internal branch or
  helper call?
- Would a failure be easy to understand from the test name and assertions?

If you cannot answer those clearly, the test should be renamed, moved, or
deleted.

## Right layer

Prefer unit tests when they protect:

- nontrivial domain logic
- policy decisions with clear user or system consequences
- failure handling that is hard to cover precisely at a higher layer
- small parsing or transformation contracts with real compatibility impact

Prefer a higher-level test when the unit test mostly checks:

- orchestration within one command path
- whether one helper forwards flags or kwargs to another helper
- whether an internal predicate returns `True` or `False` without showing the
  user-visible consequence
- ordering or formatting details that only matter inside one implementation

## Signals of a strong unit test

A strong unit test usually:

- names the policy or contract, not just the setup
- explains why the behavior matters through its assertions
- uses only as much mocking as needed
- fails in a way that quickly points to the broken rule
- covers a meaningful edge case, invariant, or failure mode

## Signals of a weak unit test

A weak unit test often:

- mirrors the code structure one branch at a time
- asserts exact wording, formatting, or incidental ordering
- validates private helpers that have no meaningful contract of their own
- recreates behavior already covered more clearly at a higher layer
- needs a long setup just to prove an internal boolean or forwarded argument

## Naming

Test names should explain the rule being protected.

Prefer names like:

- `test_cleanup_skips_stack_comment_lookup_when_open_pr_still_has_remote_branch`
- `test_status_reports_divergent_stack_with_targeted_jj_guidance`

Avoid names that only enumerate setup details without stating the policy or
reason the behavior matters.

## Outcomes

Each reviewed unit test should end up in one of four buckets:

- keep as-is
- rename for clarity
- move up a layer and replace with a higher-level test
- delete
