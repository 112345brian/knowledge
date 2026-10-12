"""Config-free onion rules for modules a client adds under client/."""
import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SYSTEM_INFRASTRUCTURE = {"sqlite3", "os", "pathlib", "subprocess", "shutil", "tempfile", "glob"}
ROLE_RANK = {
    "model": 5, "domain service": 4, "pipeline rules": 4,
    "application service": 3, "infrastructure": 2, "composition root": 1,
    "composition helper": 1, "pipeline use case": 0, "pipeline": 0,
    "driving adapter": 0,
}


def client_role(name):
    if name.endswith("_rules"):
        return "pipeline rules"
    if name.endswith(("_port", "_records")) or name == "artist_directory":
        return "domain service"
    if name.endswith("_service"):
        return "application service"
    if name.endswith("_store"):
        return "infrastructure"
    if name == "bootstrap" or name.endswith("_ingest"):
        return "composition root"
    return "pipeline"


def _base_roles():
    roles = {}
    for line in (REPO / "docs/architecture.md").read_text(encoding="utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) >= 4 and re.fullmatch(r"`[A-Za-z0-9_]+`", cells[0]):
            roles[cells[0][1:-1]] = cells[2]
    return roles


def ring_problems(sources, base_roles=None):
    """Check imports by suffix-derived role; new client files require no architecture config."""
    base_roles = _base_roles() if base_roles is None else base_roles
    roles = {name: client_role(name) for name in sources}
    problems = []

    def module_role(imported_name):
        if imported_name.startswith("client."):
            return roles.get(imported_name.split(".", 1)[1])
        if imported_name == "ingest" or imported_name.startswith("ingest."):
            return "pipeline"
        root = imported_name.split(".", 1)[0]
        return roles.get(root, base_roles.get(root))

    def imported_modules(node):
        if isinstance(node, ast.Import):
            return [alias.name for alias in node.names]
        if isinstance(node, ast.ImportFrom):
            if node.module == "client":
                return [f"client.{alias.name}" for alias in node.names]
            if node.level:
                if node.level != 1:
                    return []
                parent = node.module or ""
                names = [alias.name for alias in node.names]
                if parent:
                    return [f"{parent}.{name}" for name in names] if names else [parent]
                return names
            return [node.module] if node.module else []
        return []

    for name, source in sources.items():
        role = roles[name]
        source_rank = ROLE_RANK[role]
        tree = ast.parse(source)
        for node in ast.walk(tree):
            imported = imported_modules(node)
            roots = {path.split(".", 1)[0] for path in imported}
            if role in {"domain service", "pipeline rules", "application service", "pipeline"}:
                for package in sorted(roots & SYSTEM_INFRASTRUCTURE):
                    problems.append(f"{name}:{node.lineno}: {role} module imports infrastructure package {package}")
            for path in imported:
                target_role = module_role(path)
                if target_role in ROLE_RANK and ROLE_RANK[target_role] < source_rank:
                    problems.append(f"{name}:{node.lineno}: {role} module imports outward module {path} ({target_role})")
    return problems


def _sources(directory):
    return {path.stem: path.read_text(encoding="utf-8")
            for path in sorted(directory.glob("*.py")) if path.name != "__init__.py"}


def test_new_client_modules_are_checked_by_name_convention_only():
    sources = _sources(REPO / "client")
    assert ring_problems(sources) == []


def test_new_service_cannot_import_database_os_or_a_store_without_registration():
    for source in ("import sqlite3\n", "import os\n", "import foo_store\n", "from . import foo_store\n"):
        sources = {"foo_service": source, "foo_store": ""}
        assert ring_problems(sources), source
    assert ring_problems({"foo_service": "import fact_reads_store\n"})


def test_unregistered_plain_client_module_needs_no_config():
    assert client_role("foo") == "pipeline"
    assert ring_problems({"foo": "VALUE = 1\n"}) == []

