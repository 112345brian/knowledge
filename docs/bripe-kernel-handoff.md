# bripe_kernel data-pack integration — status as of 2026-09-26

## Why this file exists

We (Brian + Claude, in a `bripe-job-search` session) evaluated whether
`knowledge`'s SQLite `facts` table should integrate with `bripe-job-search`'s
new `bripe_kernel` data-pack plugin system instead of staying standalone.
Conclusion: **not yet, and not a drop-in fit as currently designed.** This
note is the handoff so a future session (in either repo) doesn't re-derive
the same investigation from scratch.

## What `bripe_kernel` is

Lives in `bripe-job-search` at `bripe_kernel/data_packs.py` (merged to `main`
2026-09-26, PR #826, formerly named `jobs4hippies` — renamed for the
layering-inversion reason you'd guess). It's a domain-neutral kernel for
discovering **versioned, declarative data packs** published as installed
Python distributions:

- A pack is an ordinary pip package. Its metadata declares one entry point in
  the `bripe_kernel.data_packs` group, pointing at a package containing a
  `kernel-pack.json` manifest (id, version, declared `host`/`host_api`, and a
  list of dataset files + capability strings).
- `discover_packs(host=..., host_api=...)` reads `importlib.metadata` entry
  points off *installed* distributions and validates the manifest. It
  **never calls `EntryPoint.load()`** — no executable plugin code, ever.
- A pack only participates if its manifest's declared `host` matches what the
  caller (a "host" application) requests. Right now exactly one host exists:
  `bripe_kernel.jobsearch`, implemented by `jobsearch/data_pack_host.py` in
  that repo. The kernel itself has zero job-search opinions — capability
  strings are opaque to it, checked only for shape (`<namespace>.<name>.vN`).
- Full docs: `docs/data-packs.md` and `docs/jobsearch-data-packs.md` in
  `bripe-job-search`.

## The three questions we asked, and the answers

**1. Scope — is this meant to host domains beyond job-search?**
Yes, by design (see `bripe-job-search` issue #650, the umbrella program) —
the mechanism is host-agnostic and the docs explicitly invite a second,
unrelated host to register its own id and discover a disjoint pack set. But
**no second host has ever been built**. `knowledge` would be the first thing
to actually test that "beyond job-search" claim, not a well-trodden path.

**2. Stability — is this close to merge-ready?**
Phase 0 (the host-contract kernel itself, `bripe_kernel/data_packs.py`) is
already merged to `main`, tested, and passing lint/type/dependency checks —
it's real, not a stale branch. But it's one piece of a 6-issue program
(`bripe-job-search` issues #645–#650, all still open on GitHub as of this
writing). Phases 1–3 — extracting `bripe_kernel` into its own installable
repo (#646), defining job-search's own capability schemas (#647), and
proving a second, independent reference pack (#649) — haven't started.
`bripe_kernel` still ships inside `bripe-job-search`'s own wheel; it isn't a
standalone package on PyPI or anywhere else yet.

**3. The static-package assumption — is it negotiable?**
No. `docs/data-packs.md`'s "Deliberate non-goals" states outright: "No
custom downloader, package registry, lockfile, or local pack store." Every
pack is discovered by *installed Python distribution version*, full stop —
there is no live-store or mutable-dataset adapter path in the code or
anywhere in the six-issue roadmap. Publishing a new version of a pack's data
means literally cutting a new pip-installable package version.

## Why that doesn't fit `knowledge` as-is

`knowledge`'s `facts` table (`schema.sql`, populated continuously via
`add_fact.py` and periodic vault re-ingests through `build.py`) is never
"released" — it's a live, in-place-updated SQLite store, not a versioned
artifact. To participate in `bripe_kernel` today, `knowledge` would need to
grow an entirely new **export/release step**: periodically snapshot the
`facts` table (or some subset/view of it) into a `kernel-pack.json` +
JSON dataset files, bump a version number, and `pip`-package + install that
snapshot — every time the underlying facts meaningfully change. That
snapshot step doesn't exist and isn't sketched anywhere; building it would be
on us, not something `bripe_kernel` gives you for free.

## Bottom line / when to revisit

Premature right now. Revisit when **both** are true:
- `bripe-job-search` issue #646 (extraction to a standalone, installable
  `bripe_kernel` repo) has landed — until then you'd be depending on a
  package that only exists inside another app's wheel.
- We've actually decided on and built the snapshot/release mechanism for
  turning `knowledge`'s live `facts` table into a versioned pack — this is
  the harder, unsolved half, and nothing upstream is going to solve it for
  us.

If picked back up, `add_fact.py`/`build.py` are the natural place to hook a
future `export-pack` step, analogous to how `knowledge.py build` already
produces a build artifact from source data today.
