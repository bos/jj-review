---
title: Pull request descriptions
linkTitle: PR descriptions
description: Set pull request titles, bodies, draft state, and stack overview text.
navGroup: Look things up
weight: 100
---

By default, `submit` will generate a pull request description from the `jj` change. On a
subsequent submit, if the PR description still matches the last automated PR description,
`submit` will refresh it from the current change description. Otherwise, `submit` will leave it
untouched.

For example, suppose a submit creates the title `Add caching`. You rename it on GitHub to
`Cache API requests`, then change the local description. The next submit keeps the GitHub text.

To replace text that jj-stack would otherwise leave alone, use `--describe` for a body, or use
`--describe-with` or `--edit` for titles and bodies.

## Default text

For each change, the subject becomes the pull request title and the remainder becomes its body. If
the description has no body, jj-stack tries the repo's pull request template and then falls
back to the subject.

When the body comes from the change description, jj-stack removes line wrapping inside Markdown
paragraphs while preserving lists, quotes, tables, code blocks, and explicit line breaks.

## Supply Markdown

Set one pull request body explicitly. The title still follows the normal update rule:

```console
jj-stack submit --describe <change-id>=body.md
```

Add a stack overview to the head pull request:

```console
jj-stack submit --describe stack=overview.md
```

Later submits preserve that overview, including edits made on GitHub, until you supply another
stack description. If the stack grows, jj-stack moves the overview to the new head pull request.

Relative paths resolve from the directory in which you invoke jj-stack.

## Edit every PR at once

```console
jj-stack submit --edit
```

The editor opens once with every planned title, body, and draft choice. If the edited document is
invalid or the editor exits with an error, nothing is changed locally or on GitHub.

jj-stack keeps the editor file until the whole submit succeeds and prints its path before
continuing. If submit fails, pass that file to `--resume-edit` when you retry:

```console
jj-stack submit --resume-edit /path/to/jj-stack-edit-….md
```

The retry inspects the local stack and GitHub again. The saved file supplies only the titles,
bodies, and draft choices, and must still name exactly the selected changes. A file passed to
`--resume-edit` is not removed automatically.

The editor comes from jj's `ui.editor`, then `$VISUAL`, then `$EDITOR`.

## Delegate to a helper

```console
jj-stack submit --describe-with <helper>
```

The executable receives `--pr <change-id>` once per change and `--stack <revset>` once for a
multi-change overview. It prints one JSON object:

```json
{"title": "add the API", "body": "Why this change exists.\n"}
```

Invalid or empty helper output stops the submit. A helper controls the text only; the order of
pull requests still comes from the order of the `jj` changes.
