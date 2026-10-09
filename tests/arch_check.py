"""Helpers for architecture enforcement (#36): run tach and import-linter, and build the
`kn` shadow package that lets import-linter see this flat-module repo.

Why a shadow package: import-linter (via grimp) only analyses *packages* (directories with an
__init__.py). `root_packages = ["add_fact", ...]` is rejected ("'add_fact' is a module, not a
package"), and this repo is deliberately flat. So `make_shadow()` copies every importable
top-level module into `<tmp>/kn/` and rewrites the *project-internal* import statements
(`import privacy` -> `import kn.privacy`, `from revisions import x` -> `from kn.revisions import x`).
The shadow copy is only ever parsed, never executed. Every import statement (top-level, lazy
inside a function, TYPE_CHECKING) is rewritten, so lazy imports are seen too.

The numbered scripts (`01_seed_sources.py`, ... `12_apply_fact_revisions.py`) are not valid
module names, so they cannot be in the shadow. tach models them by file instead (tach.toml).

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

# Every numbered script, and why import-linter cannot see it. Asserted equal to the files on
# disk by tests/test_architecture.py, so a new numbered script has to be listed here on purpose.
NUMBERED_SCRIPTS_EXCLUDED = (
    "01_seed_sources", "02_ingest_literature_sources", "03_ingest_measurements",
    "04_ingest_facts", "05_seed_claims", "06_seed_subject_hierarchy", "07_ingest_concerts",
    "08_ingest_music_ratings", "09_ingest_scrobbles", "10_seed_artist_members",
    "11_seed_general_facts", "12_apply_fact_revisions", "13_link_entities",
)
NUMBERED_REASON = ("not importable names (they start with a digit); build.py loads them with "
                   "importlib. tach checks them by file path instead.")


def top_level_stems(src_dir):
    return sorted(f[:-3] for f in os.listdir(src_dir) if f.endswith(".py"))


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


def _rewrite_internal_imports(source, names, pkg=SHADOW_PKG):
    tree = ast.parse(source)
    lines = source.encode().split(b"\n")
    edits = []  # (lineno0, col, old, new)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] in names:
                    edits.append((a.lineno - 1, a.col_offset, a.name, f"{pkg}.{a.name}"))
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] in names:
                edits.append((node.lineno - 1, node.col_offset, node.module, f"{pkg}.{node.module}"))
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
    for n in names:
        with open(os.path.join(src_dir, n + ".py"), encoding="utf-8") as f:
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
    """AST scan of every top-level .py (numbered scripts included): a command list starting with
    "git", a "git ..." string passed to a process-spawning call, or any os.system/os.popen/
    os.exec*/os.spawn* call outside the allowed modules. Returns a list of 'file:line: why'.
    Import-linter already limits who may import `subprocess`; this covers what that cannot,
    i.e. a permitted subprocess user (knowledge.py) quietly running git."""
    problems = []
    for stem in top_level_stems(src_dir):
        if stem in allowed:
            continue
        path = os.path.join(src_dir, stem + ".py")
        tree = ast.parse(open(path, encoding="utf-8").read())
        for node in ast.walk(tree):
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


def main():
    """`uv run python tests/arch_check.py`: the same checks as tests/test_architecture.py."""
    rc1, out1 = run_tach(REPO)
    print(out1.strip())
    rc2, out2 = run_import_linter(REPO, os.path.join(REPO, "pyproject.toml"))
    print(out2.strip())
    git = scan_git_usage(REPO)
    for g in git:
        print("git outside private_git:", g)
    return 1 if (rc1 or rc2 or git) else 0


if __name__ == "__main__":
    sys.exit(main())
