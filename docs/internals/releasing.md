# Release process

A release is built from a `v<version>`-tagged commit on `main`. The tag workflow builds and tests
both distributions, publishes them to PyPI, and creates the matching GitHub Release. A manual
workflow run publishes to TestPyPI unless it names an existing release tag to retry.

## Write the release notes

Create `release-notes/v<version>.md`, using the same version as `pyproject.toml`. This file is the
source for the GitHub Release body and must be part of the tagged commit. The production workflow
fails before publishing if the file is absent or empty. On a retry, it also fails if an existing
GitHub Release does not contain exactly the notes from the tagged commit.

### Choose what belongs

Read the changes since the previous tag and the affected user documentation before drafting. Use
the commit history to find candidates, not as the outline for the notes. A change belongs only
when it gives someone a concrete reason to upgrade or requires them to act:

- a new user workflow or capability;
- a change to installation requirements, a command, configuration, or a documented
  machine-readable interface;
- a fix for a symptom users could recognize, especially one that blocked recovery or risked losing
  work; or
- a substantial new guide that helps users complete a workflow.

Omit internal refactors, dependency substitutions, test and CI work, routine hardening, and small
normalization or diagnostic changes unless their user-visible consequence is important. Do not
give a bullet to a detail merely because it took significant engineering work. Collapse several
commits that solve the same user problem into one outcome. Prefer a short set of meaningful notes
over a comprehensive inventory.

### Write from the user's situation

Write for someone deciding whether to upgrade, not for someone reconstructing the commit history:

- Open with one or two sentences that name the release's theme and its most important user-visible
  benefits. Describe problems solved, not mechanisms added.
- Group changes by user impact. Use headings such as `Breaking changes`, `Highlights`, `Fixes`,
  and `Documentation`, but omit empty sections.
- Put breaking changes first. State which users are affected, what stops working after upgrading,
  how they can recognize the situation, and the exact migration or workaround.
- Lead each bullet with a situation or outcome the reader can recognize. Add the old symptom or
  risk when it explains why the change matters, and give an exact command when the reader must
  act.
- Use the same ordinary `jj`, Git, and GitHub vocabulary as the user guides. Do not make readers
  understand implementation terms such as classifiers, remote heads, survivors, leases,
  convergence, or mutations. A public command, option, configuration key, or JSON value may be
  named exactly when it is relevant to that audience.
- Make each bullet describe one observable outcome. Combine implementation changes that produce
  the same outcome, and split unrelated outcomes rather than joining them into a grab bag.
- Explain important fixes in terms of the symptom that is gone. Do not paste commit subjects,
  internal type names, or an automatically generated pull request list.
- End with a comparison link from the previous tag to the new tag, labeled `Full changelog`.

For example, do not write “`sync` detects moved survivor branches before rewriting local
history.” Write “If a remaining PR branch changed on GitHub, `sync` now stops before rebasing
your local changes and tells you how to recover.” Omit an item such as “configured reviewer
values are normalized like command-line values” unless that change breaks a real workflow; if it
does, put it under `Breaking changes` with the affected audience and migration.

### Edit for value and clarity

For every bullet, answer “Who cares?” and “What can they now do, or what must they do?”
Delete the bullet if the answers are not clear from its text. Check that the opening and first
few bullets capture the strongest reasons to upgrade; minor fixes must not crowd out the
release's main value. Read the result as someone familiar with `jj` and Git but unfamiliar with
the jj-stack source. Add missing context and replace unexplained internal nouns.

Do not word-wrap release-note prose. Keep each paragraph and list item on one physical line, even
when it exceeds the repository's usual 98-column limit. GitHub preserves those source line breaks
in Release bodies, which makes hard-wrapped notes render awkwardly.

Keep the notes self-contained even when they link to a pull request or issue. Fact-check every
claim against the released behavior, proofread the rendered Markdown, verify every command in a
safe environment or against `--help`, and check every link before tagging. A release with no
breaking changes does not need a `Breaking changes` heading.

## Qualify the candidate

Set the intended version in `pyproject.toml`, finish the release changes and release notes, and
run the release gates:

```console
just release-check
```

The live test requires a `gh` login that can create and delete a private repo, push to it, and
manage its pull requests.

Check the website snapshot and production build:

```console
just website-check
cd ../website
just check
```

If the snapshot is out of date, run `just website` from the jj-stack checkout, review and commit
the website change, then rerun the checks. Push the release changes to `main` before creating the
tag. A manual run of the release workflow is the optional TestPyPI smoke test.

## Publish the tag

Create the version tag and push it explicitly:

```console
jj tag set v0.1.0 -r main
jj git push --tag v0.1.0
```

Use the version from `pyproject.toml` in place of `0.1.0`. After the workflow succeeds, verify the
GitHub Release contains the authored notes and downloadable distributions. PyPI has no separate
per-version release-notes field; its project page exposes the GitHub Releases page through the
standard `Changelog` project link. Verify that link along with the package page, publish the
already-checked website with `just publish`, and verify the live quick start and install command.

Never move or reuse a published version tag. A failed workflow can be rerun against the same tag;
a source change requires a new version and tag.
