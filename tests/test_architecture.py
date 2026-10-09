"""Architecture enforcement (#36): tach module boundaries + import-linter contracts, run as tests.

Positive tests run both tools on the real tree. Negative tests prove every rule can fail: each
copies the real config (with the `#planned:` entries switched on, so the planned layout is
exercised too) into a temp dir holding an EMPTY module per configured module, adds one offending
import, and asserts the tool reports it. Real modules are never touched.

See README "Architecture enforcement" and tests/arch_check.py (why import-linter needs the
generated `kn` shadow package).
"""
import os
import re
import sys
import tomllib

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import arch_check as ac  # noqa: E402

REPO = ac.REPO
TACH_TOML = os.path.join(REPO, "tach.toml")
PYPROJECT = os.path.join(REPO, "pyproject.toml")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _tach_modules(text):
    return {m["path"]: m for m in tomllib.loads(text)["modules"]}


def _il_contracts(text):
    return {c["id"]: c for c in tomllib.loads(text)["tool"]["importlinter"]["contracts"]}


# --------------------------------------------------------------------- the real tree


def test_tach_check_passes():
    rc, out = ac.run_tach(REPO)
    assert rc == 0, "tach check failed (uv run tach check):\n" + out


def test_import_linter_passes():
    rc, out = ac.run_import_linter(REPO, PYPROJECT)
    assert rc == 0, "import-linter contracts broken (uv run python tests/arch_check.py):\n" + out


def test_git_only_runs_inside_private_git():
    problems = ac.scan_git_usage(REPO)
    assert problems == [], "git must only be run by private_git.py:\n" + "\n".join(problems)


def test_numbered_scripts_are_listed_and_modelled_by_tach():
    on_disk = sorted(s for s in ac.top_level_stems(REPO) if not s.isidentifier())
    assert on_disk == sorted(ac.NUMBERED_SCRIPTS_EXCLUDED), (
        "numbered scripts changed: update NUMBERED_SCRIPTS_EXCLUDED in tests/arch_check.py and "
        "add/remove the module in tach.toml. " + ac.NUMBERED_REASON)
    modules = _tach_modules(_read(TACH_TOML))
    for s in on_disk:
        assert s in modules and modules[s]["layer"] == "pipeline", f"{s} must be a pipeline module in tach.toml"


def test_every_module_is_classified_in_both_configs():
    """A new top-level module must pick a layer in tach.toml AND appear in the import-linter
    contracts (tach's root_module=forbid catches the first; this catches the second)."""
    contracts = _il_contracts(_read(PYPROJECT))
    up = contracts["libraries-never-import-upward"]
    classified = {m.split(".", 1)[1] for m in up["source_modules"] + up["forbidden_modules"]}
    importable = set(ac.importable_stems(REPO))
    assert importable == classified, (
        f"modules missing from import-linter contracts: {sorted(importable - classified)}; "
        f"stale entries: {sorted(classified - importable)}")
    tach = _tach_modules(_read(TACH_TOML))
    assert importable <= set(tach), f"modules missing from tach.toml: {sorted(importable - set(tach))}"


@pytest.mark.parametrize("enable", [False, True], ids=["live", "with-planned"])
def test_configs_agree_on_what_a_library_is(enable):
    """The library list is written in three places (tach layer=library, the two import-linter
    contracts); they must not drift apart."""
    t, p = _read(TACH_TOML), _read(PYPROJECT)
    if enable:
        t, p = ac.enable_planned(t), ac.enable_planned(p)
    tach_libs = {n for n, m in _tach_modules(t).items() if m["layer"] == "library"}
    c = _il_contracts(p)
    up = {m.split(".", 1)[1] for m in c["libraries-never-import-upward"]["source_modules"]}
    assert up == tach_libs
    ty = {m.split(".", 1)[1] for m in c["typer-only-in-knowledge"]["source_modules"]}
    serving = {n for n, m in _tach_modules(t).items() if m["layer"] == "serving"}
    assert ty == tach_libs | serving | {"cli_parity"}
    layered = c["libraries-layered"]["layers"]
    in_layers = {x.strip() for row in layered for x in row.replace(":", "|").split("|")}
    assert in_layers == tach_libs


def test_planned_entries_are_uncommented_once_the_module_exists():
    """Every `#planned:` line that mentions a not-yet-existing module must be activated when that
    module's file lands, so merging a module cannot silently skip its layer and import rules."""
    planned = {name for _, name in ac.planned_modules(_read(TACH_TOML))}
    landed = sorted(n for n in planned if os.path.exists(os.path.join(REPO, n + ".py")))
    stale = []
    for path in (TACH_TOML, PYPROJECT):
        for lineno, line in enumerate(_read(path).splitlines(), 1):
            if line.startswith(ac.PLANNED_PREFIX):
                for name in landed:
                    if re.search(r'"(?:kn\.)?' + name + r'"', line) or f'path = "{name}"' in line:
                        stale.append(f"{os.path.basename(path)}:{lineno}: {name}")
    assert not stale, (
        "module file now exists: delete the '#planned: ' prefix on these lines and set depends_on / "
        "layer to its real imports:\n" + "\n".join(stale))


# --------------------------------------------------------------------- negative tests


# The documented subprocess exceptions must exist in the tree or import-linter reports the
# ignore_imports line as unmatched (a stale exception is an error, as it should be).
BASELINE_CODE = {
    "private_git": "import subprocess\n",
    "script_runner": "import subprocess\n",
    # THE one CLI -> serving exception (launcher), exactly as written in the real cli_inbox.py.
    "cli_inbox": "import inbox  # tach-ignore inbox\n",
}


@pytest.fixture
def tree(tmp_path):
    """Synthetic project: real configs with planned entries ON, one empty file per tach module."""
    d = tmp_path / "proj"
    d.mkdir()
    tach_text = ac.enable_planned(_read(TACH_TOML))
    (d / "tach.toml").write_text(tach_text)
    (d / "pyproject.toml").write_text(ac.enable_planned(_read(PYPROJECT)))
    # Baseline = the maximal legal graph: every module imports exactly what tach.toml lets it
    # import (tach `exact = true` also rejects declared-but-unused dependencies), plus the
    # documented subprocess / lazy-cycle exceptions.
    for name, mod in _tach_modules(tach_text).items():
        lines = [f"import {dep}" for dep in mod["depends_on"]]
        lines.append(BASELINE_CODE.get(name, ""))
        (d / f"{name}.py").write_text("\n".join(lines) + "\n")
    return d


def _check(tree, tool):
    if tool == "tach":
        return ac.run_tach(str(tree))
    return ac.run_import_linter(str(tree), str(tree / "pyproject.toml"))


def _offend(tree, module, code):
    p = tree / f"{module}.py"
    p.write_text((p.read_text() if p.exists() else "") + code + "\n")


def test_baseline_full_config_passes_on_empty_tree(tree):
    """Planned entries enabled + an empty file per module: both tools pass. If this fails the
    planned lines themselves are malformed, and the negative tests below prove nothing."""
    for tool in ("tach", "import-linter"):
        rc, out = _check(tree, tool)
        assert rc == 0, f"{tool}:\n{out}"


# (id, module that gets the offending code, code, [(tool, [substrings expected in its output])])
CASES = [
    ("lib-imports-cli", "privacy", "import knowledge",
     [("tach", ["privacy", "knowledge"]), ("import-linter", ["kn.privacy", "kn.knowledge", "BROKEN"])]),
    ("lib-lazy-imports-cli", "privacy", "def f():\n    import knowledge",
     [("tach", ["privacy", "knowledge"]), ("import-linter", ["kn.privacy", "kn.knowledge", "BROKEN"])]),
    ("lib-imports-cli-registry", "clock", "from cli_parity import X",
     [("tach", ["clock", "cli_parity"]), ("import-linter", ["kn.clock", "kn.cli_parity"])]),
    ("lib-imports-serving", "modes", "import inbox",
     [("tach", ["modes", "inbox"]), ("import-linter", ["kn.modes", "kn.inbox"])]),
    ("lib-imports-pipeline", "modes", "import build",
     [("tach", ["modes", "build"]), ("import-linter", ["kn.modes", "kn.build"])]),
    ("lib-imports-typer", "clock", "import typer",
     [("import-linter", ["never import typer", "kn.clock", "typer", "BROKEN"])]),
    ("lib-imports-typer-lazy", "review", "def f():\n    import typer",
     [("import-linter", ["kn.review", "typer"])]),
    ("lib-imports-click", "paths", "from click import echo",
     [("import-linter", ["kn.paths", "click"])]),
    ("registry-imports-typer", "cli_parity", "import typer",
     [("import-linter", ["kn.cli_parity", "typer"])]),
    ("cli-imports-serving", "knowledge", "import inbox",
     [("tach", ["knowledge", "inbox"]), ("import-linter", ["never imports the serving layers", "kn.inbox"])]),
    ("cli-module-imports-serving", "cli_lifecycle", "import mcp_server",
     [("tach", ["cli_lifecycle", "mcp_server"]), ("import-linter", ["kn.cli_lifecycle", "kn.mcp_server"])]),
    ("serving-imports-pipeline", "inbox", "import build",
     [("tach", ["inbox", "build"]), ("import-linter", ["never imports the pipeline", "kn.build"])]),
    ("serving-imports-claims-audit", "inbox", "import claims_audit",
     [("tach", ["inbox", "claims_audit"]), ("import-linter", ["only through modes", "kn.claims_audit"])]),
    ("serving-imports-knowledge-queries", "mcp_server", "from knowledge import cmd_search",
     [("tach", ["mcp_server", "knowledge"]), ("import-linter", ["only through modes", "kn.knowledge"])]),
    ("serving-uses-raw-sqlite", "inbox", "import sqlite3",
     [("import-linter", ["only through modes", "sqlite3"])]),
    ("serving-imports-revisions", "mcp_server", "import revisions",
     [("tach", ["mcp_server", "revisions"]), ("import-linter", ["kn.mcp_server", "kn.revisions"])]),
    # ---- the inbox (real module, #36): it may import review and lifecycle and the stdlib, nothing else
    ("inbox-imports-revisions", "inbox", "import revisions",
     [("tach", ["inbox", "revisions"]), ("import-linter", ["only through modes", "kn.inbox", "kn.revisions"])]),
    ("inbox-imports-revisions-lazily", "inbox", "def f():\n    import revisions",
     [("tach", ["inbox", "revisions"]), ("import-linter", ["kn.inbox", "kn.revisions"])]),
    ("inbox-from-imports-revisions", "inbox", "from revisions import parse_timestamp",
     [("tach", ["inbox", "revisions"]), ("import-linter", ["kn.inbox", "kn.revisions"])]),
    ("inbox-imports-privacy", "inbox", "import privacy",
     [("tach", ["inbox", "privacy"]), ("import-linter", ["only through modes", "kn.inbox", "kn.privacy"])]),
    ("inbox-imports-the-git-helper", "inbox", "from private_git import commit_private_change",
     [("tach", ["inbox", "private_git"]), ("import-linter", ["only through modes", "kn.inbox", "kn.private_git"])]),
    ("inbox-imports-add-fact", "inbox", "import add_fact",
     [("tach", ["inbox", "add_fact"]), ("import-linter", ["kn.inbox", "kn.add_fact"])]),
    ("inbox-imports-facts-batch", "inbox", "import facts_batch",
     [("tach", ["inbox", "facts_batch"]), ("import-linter", ["kn.inbox", "kn.facts_batch"])]),
    ("inbox-imports-migrate-memory", "inbox", "import migrate_memory",
     [("tach", ["inbox", "migrate_memory"]), ("import-linter", ["kn.inbox", "kn.migrate_memory"])]),
    ("inbox-imports-normal-db", "inbox", "import normal_db",
     [("tach", ["inbox", "normal_db"]), ("import-linter", ["kn.inbox", "kn.normal_db"])]),
    ("inbox-lazy-sqlite", "inbox", "def f():\n    import sqlite3",
     [("import-linter", ["only through modes", "kn.inbox", "sqlite3"])]),
    ("inbox-imports-typer", "inbox", "import typer",
     [("import-linter", ["never import typer", "kn.inbox", "typer"])]),
    ("inbox-imports-the-cli-app", "inbox", "import knowledge",
     [("tach", ["inbox", "knowledge"]), ("import-linter", ["kn.inbox", "kn.knowledge"])]),
    ("inbox-imports-a-cli-module", "inbox", "import cli_lifecycle",
     [("tach", ["inbox", "cli_lifecycle"])]),
    ("inbox-imports-cli-inbox", "inbox", "import cli_inbox",
     [("tach", ["inbox", "cli_inbox"])]),
    # ---- the CLI -> serving exception is exactly the one launcher edge
    ("cli-lifecycle-imports-inbox", "cli_lifecycle", "import inbox",
     [("tach", ["cli_lifecycle", "inbox"]), ("import-linter", ["never imports the serving layers", "kn.cli_lifecycle", "kn.inbox"])]),
    ("cli-migrate-imports-inbox", "cli_migrate", "import inbox",
     [("tach", ["cli_migrate", "inbox"]), ("import-linter", ["kn.cli_migrate", "kn.inbox"])]),
    ("cli-facts-batch-imports-inbox-lazily", "cli_facts_batch", "def f():\n    import inbox",
     [("tach", ["cli_facts_batch", "inbox"]), ("import-linter", ["kn.cli_facts_batch", "kn.inbox"])]),
    ("cli-parity-imports-inbox", "cli_parity", "import inbox",
     [("tach", ["cli_parity", "inbox"]), ("import-linter", ["kn.cli_parity", "kn.inbox"])]),
    ("cli-inbox-imports-mcp-server", "cli_inbox", "import mcp_server",
     [("tach", ["cli_inbox", "mcp_server"]), ("import-linter", ["kn.cli_inbox", "kn.mcp_server"])]),
    ("cli-inbox-imports-the-pipeline", "cli_inbox", "import build",
     [("tach", ["cli_inbox", "build"])]),
    ("cli-inbox-imports-unlisted-library", "cli_inbox", "import revisions",
     [("tach", ["cli_inbox", "revisions"])]),
    # ---- libraries never import the new CLI / serving modules or each other upward
    ("lib-imports-cli-lifecycle", "review", "import cli_lifecycle",
     [("tach", ["review", "cli_lifecycle"]), ("import-linter", ["kn.review", "kn.cli_lifecycle", "BROKEN"])]),
    ("lib-imports-cli-migrate", "migrate_memory", "import cli_migrate",
     [("tach", ["migrate_memory", "cli_migrate"]), ("import-linter", ["kn.migrate_memory", "kn.cli_migrate"])]),
    ("lib-imports-cli-facts-batch", "facts_batch", "import cli_facts_batch",
     [("tach", ["facts_batch", "cli_facts_batch"]), ("import-linter", ["kn.facts_batch", "kn.cli_facts_batch"])]),
    ("lib-imports-cli-inbox", "lifecycle", "def f():\n    import cli_inbox",
     [("tach", ["lifecycle", "cli_inbox"]), ("import-linter", ["kn.lifecycle", "kn.cli_inbox"])]),
    ("lifecycle-imports-inbox", "lifecycle", "import inbox",
     [("tach", ["lifecycle", "inbox"]), ("import-linter", ["kn.lifecycle", "kn.inbox"])]),
    ("lifecycle-imports-pipeline", "lifecycle", "import build",
     [("tach", ["lifecycle", "build"]), ("import-linter", ["kn.lifecycle", "kn.build"])]),
    ("lifecycle-imports-typer", "lifecycle", "import typer",
     [("import-linter", ["kn.lifecycle", "typer"])]),
    ("lifecycle-subprocess", "lifecycle", "import subprocess",
     [("import-linter", ["kn.lifecycle", "subprocess"])]),
    ("lifecycle-imports-paths", "lifecycle", "import paths",
     [("tach", ["lifecycle", "paths"])]),
    ("review-imports-lifecycle-cycle", "review", "import lifecycle",
     [("tach", ["review", "lifecycle"]), ("import-linter", ["form a DAG", "kn.review", "kn.lifecycle"])]),
    ("revisions-imports-lifecycle", "revisions", "import lifecycle",
     [("tach", ["revisions", "lifecycle"]), ("import-linter", ["kn.revisions", "kn.lifecycle"])]),
    ("lifecycle-imports-facts-batch-sibling", "lifecycle", "import facts_batch",
     [("tach", ["lifecycle", "facts_batch"]), ("import-linter", ["kn.lifecycle", "kn.facts_batch"])]),
    ("facts-batch-imports-lifecycle-sibling", "facts_batch", "import lifecycle",
     [("tach", ["facts_batch", "lifecycle"]), ("import-linter", ["kn.facts_batch", "kn.lifecycle"])]),
    ("migrate-memory-imports-facts-batch-sibling", "migrate_memory", "import facts_batch",
     [("tach", ["migrate_memory", "facts_batch"]), ("import-linter", ["kn.migrate_memory", "kn.facts_batch"])]),
    ("modes-imports-lifecycle", "modes", "import lifecycle",
     [("tach", ["modes", "lifecycle"]), ("import-linter", ["kn.modes", "kn.lifecycle"])]),
    ("facts-batch-imports-revisions-unlisted", "facts_batch", "import revisions",
     [("tach", ["facts_batch", "revisions"])]),
    ("migrate-memory-imports-privacy-unlisted", "migrate_memory", "import privacy",
     [("tach", ["migrate_memory", "privacy"])]),
    # ---- cli_* modules stay inside their allowlists
    ("cli-lifecycle-imports-revisions", "cli_lifecycle", "import revisions",
     [("tach", ["cli_lifecycle", "revisions"])]),
    ("cli-migrate-imports-lifecycle", "cli_migrate", "import lifecycle",
     [("tach", ["cli_migrate", "lifecycle"])]),
    ("pipeline-imports-lifecycle", "build", "import lifecycle",
     [("tach", ["build", "lifecycle"])]),
    ("numbered-script-imports-inbox", "04_ingest_facts", "import inbox",
     [("tach", ["04_ingest_facts", "inbox"])]),
    ("paths-from-non-allowlisted-lib", "modes", "import paths",
     [("tach", ["modes", "paths"])]),
    ("paths-lazy-from-import", "claims_audit", "def f():\n    from paths import PRIVATE_DATA_DIR",
     [("tach", ["claims_audit", "paths"])]),
    ("paths-from-serving", "inbox", "import paths",
     [("tach", ["inbox", "paths"])]),
    ("paths-from-script-not-allowlisted", "10_seed_artist_members", "import paths",
     [("tach", ["10_seed_artist_members", "paths"])]),
    ("private-git-from-non-allowlisted-module", "claims_audit", "import private_git",
     [("tach", ["claims_audit", "private_git"])]),
    ("subprocess-in-library", "review", "import subprocess",
     [("import-linter", ["import subprocess", "kn.review", "subprocess"])]),
    ("subprocess-in-serving", "inbox", "import subprocess",
     [("import-linter", ["kn.inbox", "subprocess"])]),
    ("subprocess-lazy-in-pipeline", "build", "def f():\n    import subprocess",
     [("import-linter", ["kn.build", "subprocess"])]),
    ("library-cycle-via-upward-import", "revisions", "import review",
     [("tach", ["revisions", "review"]), ("import-linter", ["form a DAG", "kn.revisions", "kn.review"])]),
    ("library-cycle-lazy", "private_git", "def f():\n    import add_fact",
     [("tach", ["private_git", "add_fact"]), ("import-linter", ["kn.private_git", "kn.add_fact"])]),
    ("independent-siblings-import-each-other", "privacy", "import private_git",
     [("tach", ["privacy", "private_git"]), ("import-linter", ["kn.privacy", "kn.private_git"])]),
    ("lib-typechecking-import-of-cli", "modes", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import knowledge",
     [("import-linter", ["kn.modes", "kn.knowledge"])]),
    ("numbered-script-imports-cli", "01_seed_sources", "import knowledge",
     [("tach", ["01_seed_sources", "knowledge"])]),
    ("numbered-script-imports-unlisted-library", "03_ingest_measurements", "import add_fact",
     [("tach", ["03_ingest_measurements", "add_fact"])]),
    # ---- hexagonal: the domain (privacy) never touches infrastructure or its own adapter
    ("domain-imports-sqlite", "privacy", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.privacy", "sqlite3", "BROKEN"])]),
    ("domain-lazy-imports-os", "privacy", "def f():\n    import os",
     [("import-linter", ["kn.privacy", "os"])]),
    ("domain-imports-pathlib", "privacy", "from pathlib import Path",
     [("import-linter", ["kn.privacy", "pathlib"])]),
    ("domain-imports-subprocess", "privacy", "import subprocess",
     [("import-linter", ["kn.privacy", "subprocess"])]),
    ("domain-imports-its-adapter", "privacy", "import privacy_store",
     [("tach", ["privacy", "privacy_store"]), ("import-linter", ["kn.privacy", "kn.privacy_store"])]),
    ("domain-imports-clock", "privacy", "import clock",
     [("tach", ["privacy", "clock"]), ("import-linter", ["kn.privacy", "kn.clock"])]),
    ("domain-modes-imports-sqlite", "modes", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.modes", "sqlite3"])]),
    ("domain-modes-imports-its-adapter", "modes", "import modes_store",
     [("tach", ["modes", "modes_store"]), ("import-linter", ["kn.modes", "kn.modes_store"])]),
    ("domain-modes-imports-revisions", "modes", "import revisions",
     [("tach", ["modes", "revisions"])]),   # revisions is a domain module now: only the tach allowlist stops it
    ("domain-claims-audit-imports-clock", "claims_audit", "import clock",
     [("tach", ["claims_audit", "clock"]), ("import-linter", ["kn.claims_audit", "kn.clock"])]),
    ("domain-claims-audit-imports-sqlite", "claims_audit", "import sqlite3",
     [("import-linter", ["kn.claims_audit", "sqlite3"])]),
    ("domain-timestamps-imports-os", "timestamps", "import os",
     [("import-linter", ["kn.timestamps", "os"])]),
    ("adapter-modes-store-imports-upward", "modes_store", "import lifecycle",
     [("tach", ["modes_store", "lifecycle"]), ("import-linter", ["kn.modes_store", "kn.lifecycle"])]),
    ("serving-imports-claims-store", "inbox", "import claims_store",
     [("tach", ["inbox", "claims_store"]), ("import-linter", ["only through modes", "kn.claims_store"])]),
    ("domain-revisions-imports-sqlite", "revisions", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.revisions", "sqlite3"])]),
    ("domain-revisions-imports-os", "revisions", "def f():\n    import os",
     [("import-linter", ["kn.revisions", "os"])]),
    ("domain-revisions-imports-add-fact", "revisions", "import add_fact",
     [("tach", ["revisions", "add_fact"]), ("import-linter", ["kn.revisions", "kn.add_fact"])]),
    ("domain-revisions-imports-clock", "revisions", "import clock",
     [("tach", ["revisions", "clock"]), ("import-linter", ["kn.revisions", "kn.clock"])]),
    ("domain-revisions-imports-its-adapter", "revisions", "import revisions_store",
     [("tach", ["revisions", "revisions_store"]), ("import-linter", ["kn.revisions", "kn.revisions_store"])]),
    ("domain-review-rules-imports-review-store", "review_rules", "import review_store",
     [("tach", ["review_rules", "review_store"]), ("import-linter", ["kn.review_rules", "kn.review_store"])]),
    ("domain-review-rules-imports-subprocess", "review_rules", "import subprocess",
     [("import-linter", ["kn.review_rules", "subprocess"])]),
    ("domain-fact-rules-imports-sqlite", "fact_rules", "import sqlite3",
     [("import-linter", ["kn.fact_rules", "sqlite3"])]),
    ("adapter-revisions-store-imports-review", "revisions_store", "import review",
     [("tach", ["revisions_store", "review"]), ("import-linter", ["kn.revisions_store", "kn.review"])]),
    ("serving-imports-revisions-store", "inbox", "import revisions_store",
     [("tach", ["inbox", "revisions_store"]), ("import-linter", ["only through modes", "kn.revisions_store"])]),
    ("serving-imports-review-store", "inbox", "import review_store",
     [("tach", ["inbox", "review_store"]), ("import-linter", ["only through modes", "kn.review_store"])]),
    ("domain-new-fact-imports-sqlite", "new_fact", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.new_fact", "sqlite3"])]),
    ("domain-new-fact-imports-clock", "new_fact", "import clock",
     [("tach", ["new_fact", "clock"]), ("import-linter", ["kn.new_fact", "kn.clock"])]),
    ("domain-new-fact-imports-uuid", "new_fact", "import uuid",
     [("import-linter", ["kn.new_fact", "uuid"])]),
    ("domain-new-fact-imports-its-adapter", "new_fact", "import add_fact_store",
     [("tach", ["new_fact", "add_fact_store"]), ("import-linter", ["kn.new_fact", "kn.add_fact_store"])]),
    ("domain-lifecycle-rules-imports-sqlite", "lifecycle_rules", "import sqlite3",
     [("import-linter", ["kn.lifecycle_rules", "sqlite3"])]),
    ("domain-lifecycle-rules-imports-use-case", "lifecycle_rules", "import lifecycle",
     [("tach", ["lifecycle_rules", "lifecycle"]), ("import-linter", ["kn.lifecycle_rules", "kn.lifecycle"])]),
    ("domain-lifecycle-rules-imports-review", "lifecycle_rules", "import review",
     [("tach", ["lifecycle_rules", "review"]), ("import-linter", ["kn.lifecycle_rules", "kn.review"])]),
    ("adapter-add-fact-store-imports-upward", "add_fact_store", "import add_fact",
     [("tach", ["add_fact_store", "add_fact"]), ("import-linter", ["kn.add_fact_store", "kn.add_fact"])]),
    ("adapter-lifecycle-store-imports-lifecycle", "lifecycle_store", "import lifecycle",
     [("tach", ["lifecycle_store", "lifecycle"]), ("import-linter", ["kn.lifecycle_store", "kn.lifecycle"])]),
    ("serving-imports-lifecycle-store", "inbox", "import lifecycle_store",
     [("tach", ["inbox", "lifecycle_store"]), ("import-linter", ["only through modes", "kn.lifecycle_store"])]),
    ("serving-imports-add-fact-store", "inbox", "import add_fact_store",
     [("tach", ["inbox", "add_fact_store"]), ("import-linter", ["only through modes", "kn.add_fact_store"])]),
    ("domain-facts-batch-rules-imports-sqlite", "facts_batch_rules", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.facts_batch_rules", "sqlite3"])]),
    ("domain-facts-batch-rules-imports-use-case", "facts_batch_rules", "import facts_batch",
     [("tach", ["facts_batch_rules", "facts_batch"]), ("import-linter", ["kn.facts_batch_rules", "kn.facts_batch"])]),
    ("domain-migrate-memory-rules-imports-os", "migrate_memory_rules", "import os",
     [("import-linter", ["kn.migrate_memory_rules", "os"])]),
    ("domain-migrate-memory-rules-imports-clock", "migrate_memory_rules", "import clock",
     [("tach", ["migrate_memory_rules", "clock"]), ("import-linter", ["kn.migrate_memory_rules", "kn.clock"])]),
    ("domain-migrate-memory-rules-imports-glob", "migrate_memory_rules", "import glob",
     [("import-linter", ["kn.migrate_memory_rules", "glob"])]),
    ("adapter-migrate-memory-store-imports-use-case", "migrate_memory_store", "import migrate_memory",
     [("tach", ["migrate_memory_store", "migrate_memory"]), ("import-linter", ["kn.migrate_memory_store", "kn.migrate_memory"])]),
    ("domain-normal-rules-imports-sqlite", "normal_rules", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.normal_rules", "sqlite3"])]),
    ("domain-normal-rules-imports-tempfile", "normal_rules", "import tempfile",
     [("import-linter", ["kn.normal_rules", "tempfile"])]),
    ("domain-normal-rules-imports-adapter", "normal_rules", "import normal_db",
     [("tach", ["normal_rules", "normal_db"]), ("import-linter", ["kn.normal_rules", "kn.normal_db"])]),
    ("domain-leak-rules-imports-sqlite", "leak_rules", "import sqlite3",
     [("import-linter", ["kn.leak_rules", "sqlite3"])]),
    ("domain-leak-rules-imports-leak-test", "leak_rules", "import leak_test",
     [("tach", ["leak_rules", "leak_test"]), ("import-linter", ["kn.leak_rules", "kn.leak_test"])]),
    ("serving-imports-migrate-memory-store", "inbox", "import migrate_memory_store",
     [("tach", ["inbox", "migrate_memory_store"]), ("import-linter", ["only through modes", "kn.migrate_memory_store"])]),
    ("service-imports-adapter", "review_service", "import revisions_store",
     [("tach", ["review_service", "revisions_store"]), ("import-linter", ["never an adapter", "kn.review_service", "kn.revisions_store"])]),
    ("service-lazy-imports-adapter", "lifecycle_service", "def f():\n    import lifecycle_store",
     [("tach", ["lifecycle_service", "lifecycle_store"]), ("import-linter", ["kn.lifecycle_service", "kn.lifecycle_store"])]),
    ("service-imports-git-adapter", "lifecycle_service", "import private_git",
     [("tach", ["lifecycle_service", "private_git"]), ("import-linter", ["kn.lifecycle_service", "kn.private_git"])]),
    ("service-imports-its-facade", "add_fact_service", "import add_fact",
     [("tach", ["add_fact_service", "add_fact"]), ("import-linter", ["kn.add_fact_service", "kn.add_fact"])]),
    ("service-imports-clock", "facts_batch_service", "import clock",
     [("tach", ["facts_batch_service", "clock"]), ("import-linter", ["kn.facts_batch_service", "kn.clock"])]),
    ("service-imports-sqlite", "migrate_memory_service", "import sqlite3",
     [("import-linter", ["never an adapter", "kn.migrate_memory_service", "sqlite3"])]),
    ("service-imports-subprocess", "review_service", "def f():\n    import subprocess",
     [("import-linter", ["kn.review_service", "subprocess"])]),
    ("service-imports-uuid", "add_fact_service", "import uuid",
     [("import-linter", ["kn.add_fact_service", "uuid"])]),
    ("ports-imports-sqlite", "ports", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.ports", "sqlite3"])]),
    ("ports-imports-an-adapter", "ports", "import revisions_store",
     [("tach", ["ports", "revisions_store"]), ("import-linter", ["kn.ports", "kn.revisions_store"])]),
    ("ids-adapter-imports-upward", "ids", "import add_fact",
     [("tach", ["ids", "add_fact"]), ("import-linter", ["kn.ids", "kn.add_fact"])]),
    ("cli-imports-sqlite", "knowledge", "import sqlite3",
     [("import-linter", ["reach infrastructure only through use cases", "kn.knowledge", "sqlite3"])]),
    ("cli-imports-subprocess", "knowledge", "def f():\n    import subprocess",
     [("import-linter", ["kn.knowledge", "subprocess"])]),
    ("cli-imports-os", "cli_lifecycle", "import os",
     [("import-linter", ["kn.cli_lifecycle", "os"])]),
    ("cli-imports-write-adapter", "cli_migrate", "import add_fact_store",
     [("tach", ["cli_migrate", "add_fact_store"]), ("import-linter", ["kn.cli_migrate", "kn.add_fact_store"])]),
    ("cli-imports-ids-adapter", "knowledge", "import ids",
     [("tach", ["knowledge", "ids"]), ("import-linter", ["kn.knowledge", "kn.ids"])]),
    ("cli-imports-clock", "cli_facts_batch", "import clock",
     [("tach", ["cli_facts_batch", "clock"]), ("import-linter", ["kn.cli_facts_batch", "kn.clock"])]),
    ("inbox-imports-os", "inbox", "import os",
     [("import-linter", ["kn.inbox", "os"])]),
    ("serving-imports-fact-queries", "inbox", "import fact_queries",
     [("tach", ["inbox", "fact_queries"]), ("import-linter", ["only through modes", "kn.fact_queries"])]),
    ("subprocess-in-the-cli-app", "knowledge", "import subprocess",
     [("import-linter", ["import subprocess", "kn.knowledge", "subprocess"])]),
    ("rules-edit-service-imports-adapter", "rules_edit_service", "import privacy_store",
     [("tach", ["rules_edit_service", "privacy_store"]), ("import-linter", ["never an adapter", "kn.rules_edit_service", "kn.privacy_store"])]),
    ("script-runner-imports-upward", "script_runner", "import knowledge",
     [("tach", ["script_runner", "knowledge"]), ("import-linter", ["kn.script_runner", "kn.knowledge"])]),
    ("domain-artist-rules-imports-sqlite", "artist_rules", "import sqlite3",
     [("import-linter", ["never touch the file system", "kn.artist_rules", "sqlite3"])]),
    ("domain-fact-ingest-rules-imports-os", "fact_ingest_rules", "import os",
     [("import-linter", ["kn.fact_ingest_rules", "os"])]),
    ("domain-fact-ingest-rules-imports-add-fact", "fact_ingest_rules", "import add_fact",
     [("tach", ["fact_ingest_rules", "add_fact"]), ("import-linter", ["kn.fact_ingest_rules", "kn.add_fact"])]),
    ("domain-source-ingest-rules-imports-glob", "source_ingest_rules", "import glob",
     [("import-linter", ["kn.source_ingest_rules", "glob"])]),
    ("domain-music-ingest-rules-imports-pathlib", "music_ingest_rules", "from pathlib import Path",
     [("import-linter", ["kn.music_ingest_rules", "pathlib"])]),
    ("domain-measurement-rules-imports-sqlite", "measurement_rules", "import sqlite3",
     [("import-linter", ["kn.measurement_rules", "sqlite3"])]),
    ("domain-measurement-rules-imports-paths", "measurement_rules", "import paths",
     [("tach", ["measurement_rules", "paths"]), ("import-linter", ["kn.measurement_rules", "kn.paths"])]),
    ("domain-build-rules-imports-shutil", "build_rules", "import shutil",
     [("import-linter", ["kn.build_rules", "shutil"])]),
    ("domain-build-rules-imports-clock", "build_rules", "import clock",
     [("tach", ["build_rules", "clock"]), ("import-linter", ["kn.build_rules", "kn.clock"])]),
    ("domain-backfill-rules-imports-lock", "backfill_rules", "import add_fact_store",
     [("tach", ["backfill_rules", "add_fact_store"]), ("import-linter", ["kn.backfill_rules", "kn.add_fact_store"])]),
    ("domain-seed-rules-imports-subprocess", "seed_rules", "import subprocess",
     [("import-linter", ["kn.seed_rules", "subprocess"])]),
    ("ingest-script-imports-a-store", "07_ingest_concerts", "import revisions_store",
     [("tach", ["07_ingest_concerts", "revisions_store"])]),
    ("build-imports-a-use-case", "build", "import review_service",
     [("tach", ["build", "review_service"])]),
    ("adapter-imports-upward", "privacy_store", "import add_fact",
     [("tach", ["privacy_store", "add_fact"]), ("import-linter", ["kn.privacy_store", "kn.add_fact"])]),
    ("serving-imports-privacy-adapter", "inbox", "import privacy_store",
     [("tach", ["inbox", "privacy_store"]), ("import-linter", ["only through modes", "kn.privacy_store"])]),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_violation_is_reported(tree, case):
    _id, module, code, expectations = case
    _offend(tree, module, code)
    for tool, needles in expectations:
        rc, out = _check(tree, tool)
        assert rc != 0, f"{tool} did not fail for {_id}:\n{out}"
        for n in needles:
            assert n in out, f"{tool} output for {_id} lacks {n!r}:\n{out}"


def _repo_py_files():
    return [os.path.join(REPO, f) for f in sorted(os.listdir(REPO)) if f.endswith(".py")]


def test_the_only_tach_ignore_is_the_cli_inbox_launcher_import():
    """Inventory of source-level exceptions: tach's inline `# tach-ignore` is how the one
    CLI -> serving edge is allowed, so there must be exactly that one, naming only `inbox`."""
    found = []
    for path in _repo_py_files():
        for line in _read(path).splitlines():
            if "tach-ignore" in line:
                found.append((os.path.basename(path), line.strip()))
    assert found == [("cli_inbox.py", "import inbox  # tach-ignore inbox")], found
    assert "tach-ignore" not in _read(TACH_TOML).replace("`# tach-ignore`", "").replace("# tach-ignore inbox", "")


def test_exception_lists_are_exactly_the_documented_ones():
    """Every ignore_imports line is an exception someone has to justify; this pins the full list."""
    contracts = _il_contracts(_read(PYPROJECT))
    ignores = {cid: c.get("ignore_imports", []) for cid, c in contracts.items() if c.get("ignore_imports")}
    assert ignores == {
        "cli-never-imports-serving": ["kn.cli_inbox -> kn.inbox"],
        "subprocess-allowlist": ["kn.private_git -> subprocess", "kn.script_runner -> subprocess"],
        "libraries-layered": ["kn.leak_test -> kn.normal_db"],
    }, ignores
    assert "ignore_imports" not in contracts["domain-has-no-infrastructure"]
    assert "ignore_imports" not in contracts["use-cases-depend-on-ports"]
    assert "ignore_imports" not in contracts["serving-reads-facts-through-modes"]
    assert "ignore_imports" not in contracts["serving-never-imports-pipeline"]


def test_domain_list_is_a_ratchet_and_the_domain_is_pure_at_the_source_level():
    """Hexagonal architecture: the domain list may grow but never shrink or gain an exception.
    Each domain module must also parse to stdlib-only imports with no open()/print-to-disk calls,
    which catches what import-linter cannot see (importlib, __import__, builtins.open)."""
    import ast
    domain = {m.split(".", 1)[1] for m in _il_contracts(_read(PYPROJECT))["domain-has-no-infrastructure"]["source_modules"]}
    assert {"privacy", "modes", "claims_audit", "timestamps", "revisions", "review_rules", "fact_rules", "new_fact", "lifecycle_rules",
               "facts_batch_rules", "migrate_memory_rules", "normal_rules", "leak_rules", "ports",
               "artist_rules", "fact_ingest_rules", "source_ingest_rules", "music_ingest_rules", "seed_rules",
               "measurement_rules", "build_rules", "backfill_rules"} <= domain, "a module was removed from the domain list; migrate it, do not drop it"
    for name in sorted(domain):
        tree = ast.parse(_read(os.path.join(REPO, name + ".py")))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"open", "__import__", "exec", "eval"}, f"{name}.py:{node.lineno} calls {node.func.id}()"
                assert node.func.id != "print", f"{name}.py:{node.lineno}: the domain does not print"
            if isinstance(node, ast.Attribute) and node.attr in {"import_module"}:
                raise AssertionError(f"{name}.py:{node.lineno}: dynamic import in the domain")


def test_use_case_list_is_pinned_and_matches_the_ports_test():
    """The *_service modules are the use cases; each must be under the use-cases-depend-on-ports contract."""
    contracts = _il_contracts(_read(PYPROJECT))
    listed = {m.split(".", 1)[1] for m in contracts["use-cases-depend-on-ports"]["source_modules"]}
    on_disk = {n for n in ac.importable_stems(REPO) if n.endswith("_service")}
    assert listed == on_disk, f"a *_service module is outside the contract: {sorted(on_disk ^ listed)}"
    assert listed == {"review_service", "lifecycle_service", "add_fact_service", "facts_batch_service",
                      "migrate_memory_service", "rules_edit_service"}


def test_inbox_imports_only_review_lifecycle_and_the_standard_library():
    """Direct check on the real file, independent of both tools: serving holds no write logic."""
    import ast
    tree = ast.parse(_read(os.path.join(REPO, "inbox.py")))
    local = set(ac.importable_stems(REPO))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.add(node.module.split(".")[0])
    assert imported & local == {"lifecycle", "review"}, imported & local
    assert "sqlite3" not in imported


def test_unregistered_module_is_reported_by_tach(tree):
    """root_module = forbid: a new .py that is not in tach.toml fails, so adding a module forces
    choosing its layer and allowed imports."""
    (tree / "brand_new.py").write_text("import privacy\n")
    rc, out = _check(tree, "tach")
    assert rc != 0 and "brand_new" in out, out


def test_git_scan_flags_knowledge_running_git(tree):
    """knowledge.py may import subprocess (import-linter allows it), so a git call hidden there is
    caught by the AST scan instead."""
    _offend(tree, "knowledge", 'import subprocess\nsubprocess.run(["git", "status"])')
    problems = ac.scan_git_usage(str(tree))
    assert any(p.startswith("knowledge.py") for p in problems), problems


@pytest.mark.parametrize("code", [
    'import subprocess\nsubprocess.call("git commit -am x", shell=True)',
    'import os\nos.system("ls")',
    'CMD = ["git", "-C", "x", "log"]',
])
def test_git_scan_flags_variants(tree, code):
    _offend(tree, "add_fact", code)
    assert ac.scan_git_usage(str(tree)), code


def test_git_scan_allows_private_git(tree):
    _offend(tree, "private_git", 'import subprocess\nsubprocess.run(["git", "status"])')
    assert ac.scan_git_usage(str(tree)) == []


def test_shadow_rewrites_lazy_and_aliased_imports(tmp_path):
    """The shadow package must see function-level, aliased, multi-name and from-imports."""
    src, out = tmp_path / "src", tmp_path / "out"
    src.mkdir()
    out.mkdir()
    (src / "a.py").write_text("import os, b as bee\nfrom b import x\ndef f():\n    import c\n    from c import y\n")
    (src / "b.py").write_text("")
    (src / "c.py").write_text("")
    ac.make_shadow(str(src), str(out))
    text = (out / "kn" / "a.py").read_text()
    assert "import os, kn.b as bee" in text and "from kn.b import x" in text
    assert "import kn.c" in text and "from kn.c import y" in text


def test_unused_allowlist_entry_is_reported_by_tach(tree):
    """`exact = true`: a depends_on entry that is not imported any more fails, so the allowlists
    (including the `paths` one) cannot silently stay wider than the code."""
    (tree / "add_fact.py").write_text(
        "".join(f"import {d}\n" for d in _tach_modules(_read(TACH_TOML))["add_fact"]["depends_on"] if d != "paths"))
    rc, out = _check(tree, "tach")
    assert rc != 0 and "paths" in out, out
