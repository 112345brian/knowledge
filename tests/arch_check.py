"""Helpers for architecture enforcement (#36): run tach and import-linter, and build the
`kn` shadow package that lets import-linter see this flat-module repo.

Why a shadow package: import-linter (via grimp) only analyses *packages* (directories with an
__init__.py). `root_packages = ["add_fact", ...]` is rejected ("'add_fact' is a module, not a
package"), and this repo is deliberately flat. So `make_shadow()` copies every importable
top-level module into `<tmp>/kn/` and rewrites the *project-internal* import statements
(`import privacy` -> `import kn.privacy`, `from revisions import x` -> `from kn.revisions import x`).
The shadow copy is only ever parsed, never executed. Every import statement (top-level, lazy
inside a function, TYPE_CHECKING) is rewritten, so lazy imports are seen too.

The ETL steps and helpers live in ingest/ (a plain directory, listed as a second tach source root); the
shadow flattens it together with the repo root, so every module is `kn.<name>`.

Run both tools without pytest:   uv run python tests/arch_check.py
"""
import ast
import os
import re
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN = os.path.dirname(sys.executable)
PLANNED_PREFIX = "#planned:"
SHADOW_PKG = "kn"

# The ETL steps and helpers live in ingest/ (a plain directory on the source path, not a package), so their
# names are bare module names like every other module. Everything below treats the repo root and ingest/ as one
# flat namespace.
PACKAGE_DIRS = ("ingest", "client")   # the sub-packages whose modules are flattened into the shadow


def _source_files(src_dir):
    """{module stem: path} for the root directory and ingest/."""
    found = {}
    for d in (src_dir, *(os.path.join(src_dir, p) for p in PACKAGE_DIRS)):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.endswith(".py") and f != "__init__.py":
                    if f[:-3] in found:
                        raise ValueError(f"module name {f[:-3]!r} exists in both {os.path.dirname(found[f[:-3]]) or '.'} "
                                         f"and {d}: the flat kn.<name> shadow needs unique names")
                    found[f[:-3]] = os.path.join(d, f)
    return found


def top_level_stems(src_dir):
    return sorted(_source_files(src_dir))


def importable_stems(src_dir):
    return [s for s in top_level_stems(src_dir) if s.isidentifier()]


def enable_planned(text):
    """Turn every `#planned:` line into a live line (used by the negative tests so the planned
    layout is itself exercised, and by tests that check the planned entries are well formed)."""
    out = []
    for line in text.splitlines():
        if line.startswith(PLANNED_PREFIX):
            line = line[len(PLANNED_PREFIX):]
            if line.startswith(" "):
                line = line[1:]  # `#planned:     "x",` -> `    "x",` (indentation kept)
        out.append(line)
    return "\n".join(out) + "\n"


def planned_modules(text):
    """[(lineno, module)] for the `#planned: path = "name"` declarations in tach.toml."""
    found = []
    for n, line in enumerate(text.splitlines(), 1):
        m = re.match(r'^#planned:\s*path = "([a-z_]+)"', line)
        if m:
            found.append((n, m.group(1)))
    return found


def _shadow_name(name, pkg):
    """The shadow name of an imported module: `ingest.x` and `x` are both `kn.x` (the shadow is flat)."""
    for package in PACKAGE_DIRS:
        if name.startswith(package + "."):
            return f"{pkg}.{name[len(package) + 1:]}"
    return f"{pkg}.{name}"


def _rewrite_internal_imports(source, names, pkg=SHADOW_PKG):
    tree = ast.parse(source)
    lines = source.encode().split(b"\n")
    edits = []  # (lineno0, col, old, new)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in names or a.name.split(".")[0] in PACKAGE_DIRS:
                    edits.append((a.lineno - 1, a.col_offset, a.name, _shadow_name(a.name, pkg)))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module in PACKAGE_DIRS:
                edits.append((node.lineno - 1, node.col_offset, node.module, pkg))  # from ingest import x -> from kn import x
            elif node.module.split(".")[0] in names or node.module.split(".")[0] in PACKAGE_DIRS:
                edits.append((node.lineno - 1, node.col_offset, node.module, _shadow_name(node.module, pkg)))
    for ln, col, old, new in sorted(edits, reverse=True):
        line = lines[ln]
        i = line.index(old.encode(), col)  # the module name is the first occurrence after the keyword
        lines[ln] = line[:i] + new.encode() + line[i + len(old):]
    return b"\n".join(lines).decode()


def make_shadow(src_dir, dest_dir):
    """Create dest_dir/kn/ from the importable top-level modules of src_dir. Returns the names."""
    names = importable_stems(src_dir)
    pkg = os.path.join(dest_dir, SHADOW_PKG)
    os.makedirs(pkg, exist_ok=True)
    open(os.path.join(pkg, "__init__.py"), "w").close()
    sources = _source_files(src_dir)
    for n in names:
        with open(sources[n], encoding="utf-8") as f:
            src = f.read()
        with open(os.path.join(pkg, n + ".py"), "w", encoding="utf-8") as f:
            f.write(_rewrite_internal_imports(src, set(names)))
    return names


def run_tach(project_dir):
    """`tach check` in project_dir (must hold tach.toml). Returns (returncode, output)."""
    p = subprocess.run([os.path.join(BIN, "tach"), "check"], cwd=project_dir,
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def run_import_linter(src_dir, config_path):
    """lint-imports over a shadow of src_dir using config_path (a pyproject.toml / .importlinter).
    Returns (returncode, output)."""
    with tempfile.TemporaryDirectory(prefix="arch-shadow-") as tmp:
        make_shadow(src_dir, tmp)
        env = dict(os.environ, PYTHONPATH=tmp + os.pathsep + os.environ.get("PYTHONPATH", ""))
        p = subprocess.run([os.path.join(BIN, "lint-imports"), "--config", config_path, "--no-cache"],
                           cwd=tmp, capture_output=True, text=True, env=env)
        return p.returncode, p.stdout + p.stderr


# ------------------------------------------------------------------ git-outside-private_git scan
_SPAWNERS = {"subprocess", "os", "pty", "asyncio"}


def _is_git_word(v):
    return isinstance(v, str) and (v == "git" or v.startswith("git "))


def scan_git_usage(src_dir, allowed=("private_git",)):
    """AST scan of every .py in the repo root and ingest/: a command list starting with
    "git", a "git ..." string passed to a process-spawning call, or any os.system/os.popen/
    os.exec*/os.spawn* call outside the allowed modules. Also rejects direct calls to the
    private_git adapter API outside its implementation, so application code must use PrivateGit.
    Returns a list of 'file:line: why'.
    Import-linter already limits who may import `subprocess`; this covers what that cannot,
    i.e. a permitted subprocess user (knowledge.py) quietly running git."""
    problems = []
    sources = _source_files(src_dir)
    for stem in sorted(sources):
        if stem in allowed:
            continue
        path = sources[stem]
        tree = ast.parse(open(path, encoding="utf-8").read())
        git_module_aliases = {"private_git"}
        git_function_aliases = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "private_git":
                        git_module_aliases.add(alias.asname or alias.name)
            elif isinstance(node, ast.ImportFrom) and node.module == "private_git":
                for alias in node.names:
                    if alias.name in {"find_repo", "ensure_clean_tree", "commit_private_change", "is_detached", "describe_repo"}:
                        git_function_aliases.add(alias.asname or alias.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                direct_adapter_call = (
                    isinstance(func, ast.Attribute) and func.attr in {
                        "find_repo", "ensure_clean_tree", "commit_private_change", "is_detached", "describe_repo"
                    } and isinstance(func.value, ast.Name) and func.value.id in git_module_aliases
                ) or (isinstance(func, ast.Name) and func.id in git_function_aliases)
                if direct_adapter_call:
                    problems.append(f"{stem}.py:{node.lineno}: direct private_git call (use the injected PrivateGit protocol)")
            if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
                first = node.elts[0]
                if isinstance(first, ast.Constant) and first.value == "git":
                    problems.append(f"{stem}.py:{node.lineno}: command list starts with 'git'")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and isinstance(node.func.value, ast.Name) and node.func.value.id in _SPAWNERS:
                fn = node.func.attr
                if node.func.value.id == "os" and (fn in ("system", "popen") or fn.startswith(("exec", "spawn"))):
                    problems.append(f"{stem}.py:{node.lineno}: os.{fn}() (use private_git, or a subprocess call that is not git)")
                for sub in ast.walk(node):
                    if isinstance(sub, ast.Constant) and _is_git_word(sub.value):
                        problems.append(f"{stem}.py:{node.lineno}: {node.func.value.id}.{fn}() runs git")
                        break
    return sorted(set(problems))


# ------------------------------------------------------------------ layer-aware source scans
# tach.toml gives every module its ring; these scans read the sources and enforce what neither tach nor
# import-linter can see: what a module *does* (calls, attributes), not only what it imports.
INNER_LAYERS = ("model", "domain_services", "application")
WIRING_LAYERS = ("composition", "infrastructure")          # the only rings that may hold a handle or load code
DB_HANDLE_METHODS = {"execute", "executemany", "executescript", "cursor", "commit", "rollback", "fetchone",
                     "fetchall", "fetchmany", "backup", "iterdump", "create_function"}
DYNAMIC_LOAD_NAMES = {"__import__", "exec", "eval"}      # builtins, as bare names
DYNAMIC_LOAD_ATTRS = {"import_module", "spec_from_file_location", "spec_from_loader", "module_from_spec",
                      "exec_module", "load_source", "run_path"}   # importlib / runpy, as attributes
# The ports of the build ring: they hand a step (or the build) the connection the build opened, so they take one.
# Every other port speaks in the domain's terms and takes no handle.
BUILD_RING_PORTS = {"build_files_port", "datafiles_port", "inputs_port", "step_stores_port"}


def module_layers(src_dir):
    """{bare module name: tach layer} from tach.toml (`ingest.x` / `client.x` become `x`)."""
    import tomllib
    with open(os.path.join(src_dir, "tach.toml"), "rb") as f:
        modules = tomllib.load(f)["modules"]
    out = {}
    for m in modules:
        name = m["path"]
        for package in PACKAGE_DIRS:
            if name.startswith(package + "."):
                name = name[len(package) + 1:]
        out[name] = m.get("layer")
    return out


def _parse(path):
    with open(path, encoding="utf-8") as f:
        return ast.parse(f.read())


def _imported_modules(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                yield node.lineno, a.name
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.lineno, node.module


def scan_inner_ring_imports(src_dir):
    """Model, domain-service and application modules import no concrete adapter: not an infrastructure module, not a
    composition root, not a driving adapter (import-linter says it for the named contracts; this reads tach's rings,
    so a module added to a ring later is covered without editing a list). Returns 'file:line: why'."""
    layers = module_layers(src_dir)
    sources = _source_files(src_dir)
    problems = []
    for name, path in sorted(sources.items()):
        if layers.get(name) not in INNER_LAYERS:
            continue
        for lineno, mod in _imported_modules(_parse(path)):
            target = mod.split(".")[-1] if mod.split(".")[0] in PACKAGE_DIRS else mod.split(".")[0]
            if target in layers and layers[target] not in INNER_LAYERS:
                problems.append(f"{name}.py:{lineno}: {layers[target]} module {target!r} imported from the {layers[name]} ring")
    return sorted(set(problems))


def scan_handles_and_dynamic_loading(src_dir):
    """Outside the composition and infrastructure rings: no `sqlite3` import (a pipeline step may import it to catch
    its errors and name `sqlite3.Row`, never to connect), no dynamic module loading
    (importlib, __import__, spec_from_file_location, exec/eval, runpy) and no `sys.path` edit. Outside those rings
    *and* the pipeline (which runs its SQL on the connection a BuildFiles/step context hands it), no SQL is
    executed either: no `.execute`/`.executescript`/`.cursor`/`.commit`-style call on anything. Returns 'file:line: why'."""
    layers = module_layers(src_dir)
    problems = []
    for name, path in sorted(_source_files(src_dir).items()):
        layer = layers.get(name)
        if layer in WIRING_LAYERS:
            continue
        for node in ast.walk(_parse(path)):
            where = f"{name}.py:{getattr(node, 'lineno', 0)} ({layer})"
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                for m in mods:
                    if m.split(".")[0] == "sqlite3" and layer != "pipeline":
                        problems.append(f"{where}: imports sqlite3")
                    if m.split(".")[0] in ("importlib", "runpy", "imp"):
                        problems.append(f"{where}: imports {m} (dynamic loading belongs to an adapter)")
                if isinstance(node, ast.ImportFrom) and node.module == "sys" and any(a.name == "path" for a in node.names):
                    problems.append(f"{where}: imports sys.path")
            if isinstance(node, ast.Call):
                func = node.func
                if (isinstance(func, ast.Name) and func.id in DYNAMIC_LOAD_NAMES) or \
                        (isinstance(func, ast.Attribute) and func.attr in DYNAMIC_LOAD_ATTRS):
                    problems.append(f"{where}: {ast.unparse(func)}() loads code dynamically")
                if layer != "pipeline" and isinstance(func, ast.Attribute) and func.attr in DB_HANDLE_METHODS:
                    problems.append(f"{where}: .{func.attr}() (SQL/handle use outside composition, infrastructure and the pipeline)")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sys" and node.attr == "path":
                problems.append(f"{where}: sys.path")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == "sqlite3":
                # a step may name sqlite3's row type and its exceptions (to catch IntegrityError); it never makes a handle
                if not (node.attr == "Row" or node.attr.endswith("Error")):
                    problems.append(f"{where}: sqlite3.{node.attr}")
    return sorted(set(problems))


def scan_port_surface(src_dir):
    """The `*_port.py` protocols: no method named like a database call (execute, cursor, commit, fetch*...),
    no import of sqlite3 and, except in the build ring's own ports (BUILD_RING_PORTS), no parameter that is a
    connection or cursor. A port speaks in the domain's terms; a handle crossing it would let SQL leak back
    inward. Returns 'file:line: why'."""
    problems = []
    for name, path in sorted(_source_files(src_dir).items()):
        if not name.endswith("_port"):
            continue
        for node in ast.walk(_parse(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name in DB_HANDLE_METHODS or node.name.startswith(("execute", "fetch", "cursor")):
                    problems.append(f"{name}.py:{node.lineno}: port method {node.name}() is a database call")
                params = [a.arg for a in node.args.posonlyargs + node.args.args + node.args.kwonlyargs]
                for p in params:
                    if name not in BUILD_RING_PORTS and p in ("con", "conn", "connection", "cursor", "cur"):
                        problems.append(f"{name}.py:{node.lineno}: {node.name}() takes a database handle ({p})")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mods = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(m.split(".")[0] == "sqlite3" for m in mods):
                    problems.append(f"{name}.py:{node.lineno}: a port imports sqlite3")
    return sorted(set(problems))


def main():
    """`uv run python tests/arch_check.py`: the same checks as tests/test_architecture.py."""
    rc1, out1 = run_tach(REPO)
    print(out1.strip())
    rc2, out2 = run_import_linter(REPO, os.path.join(REPO, "pyproject.toml"))
    print(out2.strip())
    git = scan_git_usage(REPO)
    for g in git:
        print("git outside private_git:", g)
    scans = {"inner ring imports an adapter": scan_inner_ring_imports(REPO),
             "handle, SQL or dynamic loading outside its ring": scan_handles_and_dynamic_loading(REPO),
             "port exposes a database call": scan_port_surface(REPO)}
    for title, problems in scans.items():
        for p in problems:
            print(f"{title}: {p}")
    return 1 if (rc1 or rc2 or git or any(scans.values())) else 0


if __name__ == "__main__":
    sys.exit(main())
