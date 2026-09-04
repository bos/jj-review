---
title: How jj-stack compares with other tools
linkTitle: Compare tools
description: Understand how jj-stack differs from jj-spr, jj-gh, and gh stack.
navGroup: Look things up
weight: 115
---

## jj-stack is opinionated

On GitHub, a pull request is the head of a graph of commits. A GitHub stack builds on this: a
stack is a linear chain of PRs. This makes a stack an odd structure, a chain whose links are
themselves graphs of commits.

`jj-stack` takes a simpler stance: it requires each PR in a stack to be a single `jj` change.
Supporting multi-commit PRs in a stack would add complexity that exists mainly for backwards
compatibility with branch-based workflows, and I don't think it has merit of its own.

This is also why `jj-stack` manages the refs that keep the PRs in a stack alive. They're not
valuable, they're merely `git` plumbing getting in your way.

However, while these opinions make for a much nicer default experience, they close some doors:
if you genuinely want to produce weird stacks-of-DAGs that `gh stack` would handle, `jj-stack`
may prevent that. If you think naming your PR branches is a good use of your time, `jj-stack`
will get in your way! I can imagine a world in which these opinions are too narrow and should be
revised, so if there's enough pressure to rethink them, I may do so.

## How the tools differ

All of the tools below can turn local work into GitHub pull requests. They disagree about what
you should manage yourself and how much of the workflow the tool should own.

`jj-stack` manages a stack as a unit, and works with GitHub's native stack concept. The parent
order visible in `jj log` determines the PR order, and the same tool handles submission,
merging, local updates after a merge, and cleanup.

The alternatives stop at different boundaries. `jj-spr` concentrates on publishing reviewable
versions of individual changes. `jj-gh` provides individual GitHub commands that you can combine
with your own bookmark workflow. `gh stack` manages an explicit chain of Git branches.

Do not use more than one of these tools to update the same pull requests or PR branches. Each
tool makes different assumptions about who owns those branches.

## `jj-stack` and `jj-spr`

[`jj-spr`][jj-spr] is the closest comparison. Both tools let you amend and rebase a `jj` change
without opening a replacement pull request. Both create generated branches on GitHub and can
publish one pull request per change. Only `jj-stack` manages native GitHub stacks.

The important differences appear when you update, rearrange, or land that work.

### How reviewers see updates

Every time you revise a `jj` change, `jj-spr` creates a new synthetic incremental commit on the
PR branch, whose parent is the previous submitted version. The update message becomes that
commit's message. These commits exist for review; `jj-spr land` squash-merges the final result
into one commit on trunk.

This is a genuine append-only history. A reviewer can use GitHub's commit list to inspect each
submitted version and the difference from the version before it. However, it's very complicated
to maintain (requiring an entire parallel history with synthetic commits), and it means that
your stack's history on GitHub is potentially very different from your local history.

`jj-stack` keeps the PR branch simpler: it updates the branch to the current snapshot of your
change. GitHub records those updates as force-push events. For reviewers who want to understand
how a PR has evolved, `jj-stack` maintains a comment with links to the submitted commit and the
difference from the preceding version.

That comment approximates the useful part of `jj-spr`'s model, at much lower cost in complexity.
Reviewers still get versions and direct comparison links. `jj-stack` does not need to maintain a
second append-only commit graph or ask the author for an update-commit message.

However, `jj-stack`'s simpler approach does mean that the GitHub "Changes since your last
review" feature is always empty, and a reviewer has to rely on its PR history comment instead.
This is a frustrating shortcoming of GitHub, but I haven't found a way to enable it short of
copying `jj-spr`'s complexity.

### Dependent and independent changes

`jj-spr` calls its dependent-PR workflow a stack, but it does not create GitHub Stack objects or
use the GitHub Stacks API. It publishes related work in two ways:

- Its normal dependent mode uses ordinary PR base branches. For a change whose parent is not on
  trunk, it may create an additional generated branch containing the parent's current contents so
  that GitHub shows only the selected change in the PR.
- Its `--cherry-pick` mode publishes one change as though it were based directly on trunk. That is
  useful when changes happen to be arranged in a local chain but do not actually depend on one
  another. The pull requests can then land in any order.

Both modes create ordinary pull requests and branches. GitHub does not treat them as a native
stack.

`jj-stack` turns a chain of two or more changes into a native GitHub stack and lands it from the
bottom upward. If you rearrange, split, squash, or reorder a dependent stack with `jj`, the next
`jj-stack submit` updates the same pull requests and their GitHub stack to match.

In `jj-stack`, instead of a `--cherry-pick` option, independent work belongs in separate local
stacks. You *can* stack stacks by basing one on another, but I think this quickly becomes
unwieldy. A [megamerge](guides/multiple-stacks.md#combine-independent-stacks-locally) of
independent stacks is usually a better approach.

### Local metadata

`jj-spr` records its pull request URL and options such as cherry-pick mode in the `jj` change
description. It can also copy a title and description in either direction between the local
change and GitHub with `diff --update-message` and `amend`.

`jj-stack` leaves change descriptions as the text you wrote and stores PR links separately in
repo-local tracking data. In another checkout, `jj-stack checkout` can discover a GitHub stack,
fetch its submitted commits, restore those links, and put you on the selected change.

### Interrupted updates and unexpected branch changes

`jj-spr diff --all` processes changes one at a time. Each individual push is atomic across that
PR's head and base branches, and the append-only head means an update does not discard an older
submitted commit. If a later change fails, however, earlier pull requests may already have been
updated.

`jj-stack submit` first checks the complete selected stack. It refuses to overwrite a PR branch
that moved since `jj-stack` last saw it, then updates all selected PR branches in one push that
succeeds or fails as a whole. When it creates or edits the GitHub pull requests, this still takes
separate API calls, so an interruption can leave some of those calls incomplete. Rerunning the
command reads GitHub again and finishes the work.

### Landing and cleanup

`jj-spr land` always squash-merges one pull request. It checks the pull request state, optional
approval requirement, GitHub's mergeability result, and the exact PR head passed to the merge
request. After every landing, it requires you to fetch and rebase your local working copy by
hand. A remaining chain of dependent PRs needs more manual rebasing and another `diff --all`.

`jj-stack merge` uses the repo's configured merge, rebase, or squash method. It verifies that
the local changes still match what was submitted, then merges ready pull requests from the
bottom of the stack. For a direct merge, it then fetches trunk, removes the now-duplicated local
changes, rebases what remains, updates the remaining pull requests, and deletes unused branches.
Its `sync` command performs the same local update after a merge or stack rebase completed
through GitHub, including merges that finish later through a merge queue.

`jj-spr` offers commands to list or close pull requests, copy GitHub text back into a change,
and find orphaned generated branches. It leaves the post-merge `jj` work and the remaining
dependent PR updates for you to deal with.

### Current completeness, tests, and documentation

At the time of writing, `jj-spr` has a working central workflow and some unfinished edges. The
`patch` command exits with a not-implemented message, landing contains the limitations described
above, and post-merge rebasing is manual.

Its guides are quite thorough, but its command reference is less complete than the executable
help: it omits some exposed commands and options. Its tests cover local `jj` and Git behavior,
configuration, message handling, and dependent-PR branch construction, but do not exercise a
complete GitHub submission and landing lifecycle.

`jj-stack` covers a broader lifecycle and documents the ordinary workflows, every command,
failure recovery, automation, and some troubleshooting. Its test suite drives submissions,
GitHub responses, partial failures, merges, local synchronization, and cleanup through a
faithful simulated GitHub repository, in addition to testing the smaller components and a range
of generated stack histories.

## `jj-stack` and `jj-gh`

[`jj-gh`][jj-gh] is a set of focused GitHub helpers installed as `jj` aliases. It creates and
edits pull requests, adds PR and CI information to `jj log`, fetches pull requests into local
bookmarks, retries failed CI, enables auto-merge, and updates PR bases after local history
changes.

Its basic unit is a bookmarked revision. `jj pr create <rev>` uses a bookmark already on that
revision or creates one while pushing. The pull request contains all commits between its base
bookmark and the selected revision, so one PR can deliberately contain several local commits.
After a rebase changes the relationship between bookmarks, `jj pr restack` shows a plan and
updates the PR base branches. You continue to decide when and how to push the bookmarks.

`jj-stack` instead makes each non-empty change one pull request, hides generated PR branches
from normal local work, and updates the complete stack with one `submit`. It also takes
responsibility for ordered merges, external merges, native GitHub stack rebases, continuation in
another checkout, and cleanup.

The extra `jj-gh` commands remain useful precisely because it is *not* trying to own that complete
lifecycle. Its pull-request-aware log, fork-fetching, CI retry, auto-merge, and multi-commit PR
workflows are capabilities that `jj-stack` does not replace.

## `jj-stack` and `gh stack`

Both [`gh stack`][gh-stack] and `jj-stack` create native GitHub stacks. The review and merge
experience on GitHub is therefore the same. Their local models are different.

`gh stack` represents a stack as an ordered chain of named Git branches. A branch, and therefore
a pull request, may contain several commits. You use `gh stack` commands and its modification UI
to create, reorder, and rebase that branch chain.

`jj-stack` represents the stack as the parent order of mutable `jj` changes. One change is one
pull request. You use ordinary `jj` commands to edit and rearrange the history, then submit the
result. The generated PR branches are remote-only and normally hidden.

The submission behavior follows from those models. `gh stack` currently works upward one layer
at a time: push a branch, find or create its pull request, update it, and continue. If a higher
layer fails, lower layers may already have changed. `jj-stack` reads the selected stack first
and updates all of its PR branches together in one all-or-nothing push before it creates or
edits pull requests. That normally requires fewer serial network round trips for a larger stack,
although I haven't tried a head-to-head benchmark.

## Research notes

The `jj-gh` and `jj-spr` comparisons were checked on August 27, 2026, against [commit
`3236585f`][jj-gh-revision] and [commit `998ee83d`][jj-spr-revision]. The `gh stack` comparison
was rechecked on September 3, 2026, against [commit `2bd699a`][gh-stack-revision]. These tools
are evolving, so recheck their current documentation before relying on a detail that affects
your workflow.

[gh-stack]: https://github.com/github/gh-stack
[gh-stack-revision]: https://github.com/github/gh-stack/commit/2bd699a
[jj-gh]: https://github.com/mrjones2014/jj-gh
[jj-gh-revision]: https://github.com/mrjones2014/jj-gh/commit/3236585f
[jj-spr]: https://github.com/LucioFranco/jj-spr
[jj-spr-revision]: https://github.com/LucioFranco/jj-spr/commit/998ee83d
