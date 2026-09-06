---
title: How jj-stack compares with other tools
linkTitle: Compare tools
description: Choose a GitHub PR workflow that fits how you use jj or Git.
navGroup: Look things up
weight: 115
---

## Choose the workflow you want

`jj-stack` is for a linear stack of `jj` changes, with one pull request per change. You edit the
history with `jj`; `jj-stack` handles PR branches, submission, merging, and updates after a merge.
If you want several local commits in one PR or prefer to manage PR bookmarks yourself, another
workflow may fit better.

| Tool | How you organize the work |
| --- | --- |
| `jj-stack` | One `jj` change per PR; local parent order determines the stack. |
| [`jj-spr`][jj-spr] | Publish `jj` changes with an incremental commit for each PR update. |
| [`jj-gh`][jj-gh] | Use bookmarks for PRs and choose individual GitHub helper commands. |
| [`gh stack`][gh-stack] | Build a stack of named Git branches, with one PR per branch. |

Use one tool to update a given set of PRs and PR branches. Switching between tools on the same
PRs can leave their tracking data inconsistent.

## `jj-stack` and `jj-spr`

Both tools keep a change linked to its PR when you amend or rebase it. The main difference is
how they publish revisions for review.

`jj-spr` adds a commit to the PR branch for each update, preserving the previous submitted
versions in that branch's history. Reviewers can inspect the incremental diffs in GitHub's commit
list. `jj-spr land` squash-merges the final result and fetches it; you rebase locally afterward.
See the [`jj-spr` workflow][jj-spr].

`jj-stack` updates the PR branch to your current local commit. GitHub records the force-push,
and a [PR history comment](guides/review-a-stack.md#review-revisions) links to recent versions and
their diffs from the preceding version. Use those links to review revisions. `jj-stack merge`
supports merge, rebase, or squash according to your configuration and the repo's rules. When
GitHub completes the merge immediately, it also updates your local stack. After a queued or
externally initiated merge, run `jj-stack sync`.

For independent changes arranged in a local chain, `jj-spr` offers `--cherry-pick` to publish them
as separate PRs against trunk. With `jj-stack`, put independent work in
[separate local stacks](guides/multiple-stacks.md). You can combine those stacks in a local
megamerge for testing without submitting the merge change itself.

## `jj-stack` and `jj-gh`

`jj-gh` provides GitHub commands through `jj` aliases. It can create and edit PRs, show PR and CI
status in `jj log`, fetch PRs from forks, enable auto-merge, and update PR bases after you
rearrange local history. See the [`jj-gh` commands][jj-gh].

Its bookmark workflow gives you control over which commits belong in each PR. For example,
`jj pr restack` lets you inspect and adjust the proposed PR bases before it updates GitHub.

`jj-stack` makes the PR boundaries follow your changes: each change is one PR. A single
`jj-stack submit` updates the selected stack, including its GitHub stack grouping. Use `jj-gh`
when you want individual GitHub helpers around your own bookmark workflow; use `jj-stack` when
you want submission and merging to follow a linear chain of changes.

## `jj-stack` and `gh stack`

Both tools create native GitHub stacks, so reviewers use GitHub's stack navigation and review
controls with either tool.

`gh stack` organizes work as named Git branches. A branch can contain several commits, and
`gh stack` creates one PR per branch. Its commands and interactive editor help you add,
reorder, rebase, and combine those branches. See the [`gh stack` guide][gh-stack].

With `jj-stack`, you make those edits using `jj` commands such as `jj split`, `jj squash`, and
`jj arrange`. There is no separate list of stack branches to maintain: submitting reads the
current local history and updates GitHub to match.

Choose `gh stack` for a Git branch workflow or multi-commit PRs. Choose `jj-stack` if you already
use `jj` and want each change to remain the unit you edit, submit, and merge.

## Sources

The linked project documentation was checked on September 6, 2026. These tools are changing;
check their current guides for requirements and detailed command behavior.

[jj-spr]: https://github.com/jennings/jj-spr
[jj-gh]: https://github.com/mrjones2014/jj-gh
[gh-stack]: https://github.com/github/gh-stack
