# Release process

A release is built from a `v<version>`-tagged commit on `main`. The tag workflow builds and tests
both distributions, publishes them to PyPI, and creates the matching GitHub Release. A manual
workflow run publishes to TestPyPI unless it names an existing release tag to retry.

## Write the release notes

Create `release-notes/v<version>.md`, using the same version as `pyproject.toml`. This file is the
source for the GitHub Release body and must be part of the tagged commit. The production workflow
fails before publishing if the file is absent or empty. On a retry, it also fails if an existing
GitHub Release does not contain exactly the notes from the tagged commit.

Write for someone deciding whether to upgrade, not for someone reconstructing the commit history:

- Open with a short summary of the release's user-visible value.
- Group changes by user impact. Use headings such as `Breaking changes`, `Highlights`, `Fixes`,
  and `Documentation`, but omit empty sections.
- Put breaking changes first. State who is affected, what happens after upgrading, and the exact
  migration or workaround.
- Make each bullet describe one observable outcome in plain language. Include commands, version
  requirements, and links to the relevant guide when they help the reader act.
- Explain important fixes in terms of the symptom that is gone. Do not paste commit subjects,
  internal type names, or an automatically generated pull request list.
- End with a comparison link from the previous tag to the new tag, labeled `Full changelog`.

Do not word-wrap release-note prose. Keep each paragraph and list item on one physical line, even
when it exceeds the repository's usual 98-column limit. GitHub preserves those source line breaks
in Release bodies, which makes hard-wrapped notes render awkwardly.

Keep the notes self-contained even when they link to a pull request or issue. Proofread the
rendered Markdown and check every command and link before tagging. A release with no breaking
changes does not need a `Breaking changes` heading.

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
