# Documentation guidance

`docs/` contains user guides and references. `docs/internals/` contains contributor notes, with
additional [internal guidance](internals/AGENTS.md). The public vocabulary rules below apply to
user docs, built-in help, diagnostics, and other user-facing output.

## Audience and vocabulary

Assume readers know `jj`, Git, and GitHub. Use standard terms such as revset, bookmark, `@-`,
`trunk()`, change ID, and working copy without teaching them again.

Use product nouns consistently:

- A **pull request** or **PR** is the GitHub object.
- A **PR branch** is a Git branch intended to be a PR head. "Remote PR branch" is redundant.
- A **stack** is an ordered chain of local changes or GitHub pull requests. Say **local stack**
  or **GitHub stack** when the distinction matters. Use **stack membership** for which PRs belong
  to a stack.
- **Review** means human review activity: comments, approvals, requested changes, reviewers, and
  feedback. It is not a synonym for a PR, branch, or stack.
- A **saved pull request link**, shortened to **saved link**, connects a PR to a local change.
  The collection is **tracking data**. Use the verbs **link** and **relink**, rather than attach,
  adopt, or claim, in user guidance.
- A **direct merge** is one GitHub performs immediately rather than through a merge queue. Define
  it the first time a page or help text uses the term.

Name commands in full in hints and messages: `jj-stack relink`, not `relink`.

Describe concrete actions and effects instead of internal mechanisms:

- "The changes at the bottom of the stack that are ready", not "ready prefix".
- "Your remaining changes are still based on the old history", not "ancestry shape".
- "Set up local tracking for", not "materialize locally".
- "Failed command" or "interrupted command", not "outstanding incomplete operation".
- Describe what a recovery command does instead of calling it a "local-history repair path".

Do not expose internal record names or implementation phases in user instructions. When internal
design reasoning is needed, put it in `docs/internals/` and link to it.

## Help and references

Built-in `--help` is the flag reference. Its source lives in `src/jj_stack/cli.py` and
`src/jj_stack/commands/`, including command subpackages. Guides explain when and why to use a
command, rather than copying its option list. Apply the same vocabulary to command docstrings,
flag descriptions, and recovery hints.

## Where to edit

Follow the root [documentation policy](../AGENTS.md#documentation) and edit the closest source:

- `docs/troubleshooting.md` for recurring symptoms and recovery.
- `docs/guides/` when workflow steps or decisions change.
- `docs/reference/` for supported interfaces and settings.
- `docs/README.md` when navigation or the command overview changes.

For website synchronization, follow the root policy. Internal notes and agent instructions are
not part of the public documentation snapshot.
