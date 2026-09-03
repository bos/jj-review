---
title: JSON output
description: Read stable stack and pull request data from scripts and agents.
navGroup: Look things up
weight: 105
---

`jj-stack view --json` and `jj-stack list --json` print structured versions of the
normal command output. The JSON schema uses the same user-facing concepts as the text
output: stacks, rows, changes, PR branches, pull requests, and status.

The checked-in schema is
[json-output.schema.json](https://github.com/bos/jj-stack/blob/main/docs/json-output.schema.json).
Integration tests validate real command output against that file.

Command failures and incomplete GitHub inspection still use the normal CLI contract:
stderr explains the problem and the process exit code says what kind of problem it was
(see [Automation and agents](automation.md#exit-codes)). In particular, `view --json` and
`list --json`
print a valid payload and exit 10 when the report is incomplete. The JSON payload is not
an error-reporting format.

## Change objects

Stack changes use this shape:

```json
{
  "change_id": "zvlyxwvksmry...",
  "branch": "jj-stack/add-json-output-zvlyxwvk",
  "subject": "add json output",
  "status": "open",
  "pr": {
    "checks": "passed",
    "number": 12,
    "url": "https://github.com/octo-org/example/pull/12"
  }
}
```

`current: true` is present when that change is the current working-copy change. It is
omitted otherwise.

`branch` is present only when `jj-stack`'s tracking data links the change to a PR branch. An
unsubmitted change has no `branch` field, because the branch name is not chosen until submit. An
orphan row always has one, because the tracking data is all that identifies it.

`pr` is present when `jj-stack` knows which pull request belongs to the change. It holds the pull
request number, its URL when GitHub reported the pull request, and the combined result of its
checks when GitHub reported one. Use the change's `status` field for the pull request's state and
review decision.

Within `pr`, `number` is always present. `url` is absent when live GitHub state was unavailable,
so a change whose status is `submitted`, and every orphan row, carries `number` alone.

`checks` is `passed`, `failed`, or `pending`; `pending` includes checks that GitHub expects but
has not started.

Known change statuses are:

- `unsubmitted`: no PR has been submitted for this change
- `submitted`: submitted before, but live GitHub status is unavailable
- `open`: open, non-draft PR with no review decision to report
- `queued`: open PR waiting in GitHub's merge queue
- `draft`: open draft PR
- `approved`: open PR whose latest review decision is approved
- `changes_requested`: open PR with requested changes
- `merged`: PR is merged and local cleanup may be needed
- `closed`: PR is closed without being merged
- `missing`: tracking data names a PR, but GitHub did not report that PR for the branch
- `ambiguous`: more than one matching PR was found
- `branch_moved`: the open PR's branch was updated outside jj-stack; it is at neither this change
  nor the last submitted commit
- `divergent`: multiple visible commits exist for the same change
- `unknown`: GitHub lookup failed for this change

## `view --json`

`view --json` returns the stack or stacks you asked it to inspect:

```json
{
  "stacks": [
    {
      "selector": "PR 12",
      "changes": [
        {
          "change_id": "zvlyxwvksmry...",
          "branch": "jj-stack/add-json-output-zvlyxwvk",
          "subject": "add json output",
          "status": "open",
          "pr": {
            "checks": "passed",
            "number": 12,
            "url": "https://github.com/octo-org/example/pull/12"
          }
        }
      ]
    }
  ]
}
```

`selector` is present only when the stack came from an explicit selector such as a
revset argument or `--pull-request`.

## `list --json`

`list --json` returns the same row model as the text table. Stack rows contain their
changes, so clients can derive the head change, change count, and PR list directly from
the `changes` array.

```json
{
  "rows": [
    {
      "type": "stack",
      "current": true,
      "subject": "add json output",
      "status": "1 approved, open, checks pending",
      "changes": [
        {
          "change_id": "rlvmnowlqpsu...",
          "branch": "jj-stack/add-the-model-rlvmnowl",
          "subject": "add the model",
          "status": "approved",
          "pr": {
            "checks": "passed",
            "number": 11,
            "url": "https://github.com/octo-org/example/pull/11"
          }
        },
        {
          "change_id": "zvlyxwvksmry...",
          "branch": "jj-stack/add-json-output-zvlyxwvk",
          "subject": "add json output",
          "status": "open",
          "pr": {
            "checks": "pending",
            "number": 12,
            "url": "https://github.com/octo-org/example/pull/12"
          }
        }
      ]
    },
    {
      "type": "orphan",
      "change_id": "kkkkkkkkkkkk...",
      "branch": "jj-stack/old-change-kkkkkkkk",
      "subject": "local change missing",
      "status": "orphan",
      "pr": {
        "number": 7
      }
    }
  ]
}
```

`current: true` on a stack row means that the current working-copy change is part of
that stack. It is omitted for other stack rows.

A stack row's `status` is a human-readable summary of the changes below it, as in the
`1 approved, open, checks pending` above. Its wording is not a stable machine-readable vocabulary,
and for a single-change stack it can look exactly like a change status. Scripts should inspect the
`changes` array and use each change's documented `status` value instead. An orphan row always
uses `"status": "orphan"`.
