# Cutting a release

[towncrier](https://towncrier.readthedocs.io) turns the pending fragments in `changelog.d/` into a dated `CHANGELOG.md`
entry, and [bump-my-version](https://callowayproject.github.io/bump-my-version/) bumps the version everywhere it is
written, commits, and creates an annotated tag. Both are configured in `pyproject.toml` (`[tool.towncrier]`,
`[tool.bumpversion]`); this page is the day-to-day usage. (bump-my-version is the maintained successor of bump2version,
which was archived in 2021.)

## Day to day: write a fragment with the change

Whenever a change is worth a line in the changelog, add one small file to `changelog.d/` in the same commit:

```
changelog.d/+<short-slug>.<type>.md
```

`<type>` is `upgrade`, `feature`, `change`, `fix` or `docs`; [changelog.d/README.md](../changelog.d/README.md) says what
each means and shows an example. The file holds one or two plain sentences. It sits there until the next release
compiles it, so whoever made the change writes the entry while they still remember what it does.

## Cutting a release

Documentation first: the README and `docs/` must describe the release before it is versioned (`knowledge.py docs check`
and the tests in `tests/test_docs_links.py` and `tests/test_version.py` catch some of this, not all of it).

```bash
git switch -c release/X.Y.Z origin/main

# 1. Compile the fragments into CHANGELOG.md (this also deletes them).
uv run towncrier build --version X.Y.Z --yes
git add CHANGELOG.md changelog.d/
git commit -m "Update changelog for vX.Y.Z"

# 2. Bump the version in pyproject.toml, mcp_rules.py and uv.lock; commit "Release vX.Y.Z"; tag vX.Y.Z.
uv run bump-my-version bump patch          # or minor / major, or --new-version X.Y.Z

# 3. Open a pull request, wait for CI, merge it.
git push origin release/X.Y.Z

# 4. Push the tag and publish the release, with the changelog entry as its notes.
git push origin vX.Y.Z
gh release create vX.Y.Z --verify-tag --title "vX.Y.Z" --notes-file <(sed -n '/^## X.Y.Z - /,/^## /{/^## X.Y.Z - /d;/^## /d;p}' CHANGELOG.md)
```

The tag is made on the release commit, which is in `main`'s history once the pull request is merged (a merge commit
follows it). The changelog and the bump are separate commits because `bump-my-version` refuses to run on a dirty tree:
that is a safety check against bundling unrelated work into a release, not a formality.

### Preview before committing to anything

```bash
uv run towncrier build --draft --version X.Y.Z      # prints the entry, changes nothing
uv run bump-my-version bump patch --dry-run -v      # prints every file it would change, the commit and the tag
```

## If something looks wrong

- **The commit or tag message is `Bump version: ...`.** The config keys are `message` and `tag_message`, not
  `commit_message`; a wrong key is accepted silently and falls back to the tool's default. `uv run bump-my-version show message`
  prints what is configured.
- **`bump-my-version` replaced `current_version = ...` inside its own config.** A search such as `version = "0.2.0"`
  also matches that line. The searches in `pyproject.toml` start with a newline (`"\nversion = ..."`) for this reason.
- **`git working directory is not clean`.** Commit or stash first; do not set `allow_dirty = true`, which would also hide
  an accidental dirty tree.
- **A fragment is ignored or the build fails.** Its type must be one of the configured ones, and its name must end
  `.<type>.md`.
- **`UnicodeEncodeError` printing an arrow (Windows).** A console-encoding problem, not a config one: run with
  `PYTHONUTF8=1`.
- **`tests/test_version.py` fails after a bump.** `pyproject.toml`, `uv.lock` and `mcp_rules.SERVER_VERSION` must agree
  and `CHANGELOG.md` must have the entry; the bump updates the first three, so a failure means the tool's file list is
  out of date.
