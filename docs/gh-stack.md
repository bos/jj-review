---
title: jj-stack and gh stack
linkTitle: Compare with gh stack
description: Choose the tool whose local model matches the repo you work in.
navGroup: Look things up
weight: 115
---

Both tools create native GitHub stacks: one pull request per change, based on the one
below it. They differ in what defines the stack locally.

| Topic | `jj-stack` | `gh stack` |
|---|---|---|
| What defines the order | The parent order of `jj` changes | An ordered list of Git branches |
| Unit for review | One mutable `jj` change | One branch with one or more commits |
| PR branches | Remote-only and normally hidden | Ordinary local and remote branches |
| Restructure | Standard `jj` history editing | `gh stack` commands and its modify UI |
| Refresh PRs | Submit after a `jj` rewrite | Rebase higher branches, then push or submit |
| Continue work elsewhere | Adopt PRs and edit the change you chose | Create branches and switch |

## GitHub access and performance

Both tools spend much of their time waiting for GitHub and the Git remote. The GitHub API is far
from fast: a single call can take 800ms or longer. Both tools try to reduce GitHub round trips,
but they organize their approaches differently.

Before changing anything, `jj-stack` checks that every selected change still matches the PR and
branch it expects. It groups stack-wide PR and branch lookups into batched GraphQL requests, and
it runs independent checks concurrently. For example, it can inspect PRs, branches, repo
settings, and GitHub stack membership in the same round of network requests.

During `submit`, `jj-stack` sends all PR branch updates in one atomic push, so all branch
updates succeed together or none will. Exact checks on the old branch targets also prevent the
push from overwriting a branch that moved after the initial inspection. Once the branches are
ready, independent PR creates and updates run concurrently with a bound on the number of active
requests.

`gh stack` also avoids some unnecessary waiting. It uses bounded concurrency when refreshing PRs
and a GraphQL batch request for some reads, such as the PR titles shown by `merge`. Its current
`submit` implementation processes the stack one layer at a time: push one branch, find or create
its PR, apply any PR updates, and then repeat for the next branch. This produces a consistent
chain of PR branches, but it is not the only way to do so. `jj-stack` computes every branch target
first and reaches the same final branch state with one atomic push. The per-layer `gh stack`
approach creates a longer chain of network round trips as the stack grows. If a higher layer
fails, lower branches may already have been pushed successfully.

### What this means in practice

`jj-stack` should often feel faster when submitting a stack with several changes. Read-only
commands will often need fewer network round trips. The difference may be small for a short
stack, and `gh stack` may start faster for local or one-change work because it is a Go binary.

These are expectations based on how the tools organize their work, not results from a published
head-to-head benchmark. Actual speed will depend on your repo and stack, the network, GitHub,
and the phase of the moon.

## Which should I use?

- In a `jj` repo, use `jj-stack` and author the stack as mutable changes.
- In a Git repo, `gh stack` manages the stack as an explicit branch chain.

`gh stack link` is useful when another tool already manages one stable branch per pull request and
you only need to tell GitHub that the pull requests form a stack. In a `jj` workflow, `jj-stack`
also remembers which change belongs to each pull request.

Do not use both tools to update the same pull requests or PR branches. They organize local work
differently, and `jj-stack` will stop if another tool moves one of its branches unexpectedly.

The GitHub review and merge experience is the same. After merging on GitHub or in another client,
update the local `jj` stack with:

```console
jj-stack sync <head-change-id>
```


The implementation details above were checked on August 24, 2026 against `gh stack` [commit
`4f9188e`](https://github.com/github/gh-stack/commit/4f9188e). If `gh stack` changes later, some
details may no longer match.
