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
    "knowledge": "import subprocess\n",
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
    (tree / "knowledge.py").write_text(
        "".join(f"import {d}\n" for d in _tach_modules(_read(TACH_TOML))["knowledge"]["depends_on"] if d != "paths")
        + "import subprocess\n")
    rc, out = _check(tree, "tach")
    assert rc != 0 and "paths" in out, out
