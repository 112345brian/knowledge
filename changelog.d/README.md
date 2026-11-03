# Changelog fragments

One file per change, added in the same commit or pull request as the change itself, compiled into `CHANGELOG.md` at
release time by `towncrier build`. How to cut a release is in [docs/releasing.md](../docs/releasing.md).

**Filename**: `+<short-slug>.<type>.md`. The leading `+` marks an orphan fragment (no issue number). `<type>` is one of:

- `upgrade` — something a user must do when moving to this release (rendered under **Upgrading**)
- `feature` — new capability (**Added**)
- `change` — changed behavior (**Changed**)
- `fix` — bug fix (**Fixed**)
- `docs` — documentation only (**Documentation**)

A fragment with any other type fails to build. Order in the changelog is the order above.

**Content**: one or two sentences in the voice of the existing entries — what changed, not why (the why belongs in the
commit message). Example: `changelog.d/+export-gitignore.fix.md`

```
`db export` now writes an `export/.gitignore` for the large parts, so the first export no longer makes `add-fact`
refuse to run in the private repository.
```

**Release recipe** (the full version, with the pull-request step, is in [docs/releasing.md](../docs/releasing.md)):

```
uv run towncrier build --version X.Y.Z --yes    # compiles the fragments into CHANGELOG.md and deletes them
git add CHANGELOG.md changelog.d/
git commit -m "Update changelog for vX.Y.Z"
uv run bump-my-version bump patch               # (or minor / major) bumps the version, commits, tags vX.Y.Z
```

The changelog build and the version bump are separate commits on purpose: `bump-my-version` refuses a dirty working
tree, so the changelog has to land cleanly first.
