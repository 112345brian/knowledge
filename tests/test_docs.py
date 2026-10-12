"""Generated documentation: the regions of README.md and docs/*.md that the code already states are rendered from it
(`knowledge.py docs build`), and this file fails when a committed region is stale (`knowledge.py docs check`).

* the pure rules (docs_rules): markers, rendering, determinism, escaping;
* the use case (docs_service) against an in-memory file set: build then check, drift, damaged markers;
* the real tree: the committed docs equal the rendered ones, nothing private leaks into them, every module
  has a one-line summary, and the CLI commands behave.
"""
import json
import os
import re
import sys

import pytest
import typer.main

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from conftest import REPO  # noqa: E402

import cli_docs
import cli_parity
import docs_rules as rules
import docs_service
import docs_store
import knowledge

TACH = '''
layers = ["serving", "cli", "pipeline", "composition", "infrastructure", "application", "domain_services", "model"]

[[modules]]
path = "knowledge"
layer = "cli"

[[modules]]
path = "ingest.seed_things"
layer = "pipeline"

[[modules]]
path = "build_rules"
layer = "pipeline"

[[modules]]
path = "thing"
layer = "composition"

[[modules]]
path = "wiring"
layer = "composition"

[[modules]]
path = "thing_store"
layer = "infrastructure"

[[modules]]
path = "thing_service"
layer = "application"

[[modules]]
path = "thing_port"
layer = "domain_services"

[[modules]]
path = "vocab"
layer = "model"
'''

PYPROJECT = '''
[project]
requires-python = ">=3.11"
dependencies = ["typer>=0.12"]

[dependency-groups]
dev = ["pytest>=8", "mypy>=1.11"]
'''

SOURCES = {
    "knowledge": '"""The Typer app."""\n',
    "seed_things": '"""Load things | with a pipe."""\n',
    "build_rules": '"""Step order."""\n',
    "thing": '"""Binds thing_service."""\n',
    "wiring": '"""Binds and verifies."""\n',
    "thing_store": '"""Thing files."""\n',
    "thing_service": '"""Use the things."""\n',
    "thing_port": '"""Port for things."""\n',
    "vocab": '"""Allowed values."""\n',
}

COMMANDS = (
    rules.CommandDoc(path=("search",), help="Search facts. More detail follows here.",
                     arguments=(rules.ArgumentDoc("TERMS", required=True),),
                     options=(rules.OptionDoc(("--limit",), "N", False, "Maximum rows."),
                              rules.OptionDoc(("--json",), "", False, "Print JSON."))),
    rules.CommandDoc(path=("subject", "list"), help="List subjects.", arguments=(), options=()),
)


def inputs(**over):
    base = dict(tach_text=TACH, pyproject_text=PYPROJECT, module_sources=dict(SOURCES), commands=COMMANDS)
    base.update(over)
    return rules.Inputs(**base)


def doc_with(*names, extra=""):
    parts = ["# Title\n", "Hand-written intro.\n"]
    for n in names:
        parts += [f"<!-- generated:{n}:start -->\n", "stale\n", f"<!-- generated:{n}:end -->\n"]
    return "\n".join(parts) + extra


# --------------------------------------------------------------------- markers and regions


def test_a_region_is_fenced_by_named_markers_and_says_do_not_edit():
    text = rules.replace_region(doc_with("module-map"), "module-map", "| a | b |")
    assert text.count("<!-- generated:module-map:start -->") == 1
    assert text.count("<!-- generated:module-map:end -->") == 1
    body = rules.extract_region(text, "module-map")
    assert "do not edit" in body.lower() and "knowledge.py docs build" in body
    assert "| a | b |" in body
    assert "Hand-written intro." in text and text.startswith("# Title")


def test_replacing_a_region_twice_is_idempotent_and_touches_nothing_outside_it():
    once = rules.replace_region(doc_with("module-map", extra="\ntail prose\n"), "module-map", "x")
    assert rules.replace_region(once, "module-map", "x") == once
    assert once.endswith("tail prose\n")


@pytest.mark.parametrize("text, message", [
    ("no markers at all\n", "no markers"),
    ("<!-- generated:module-map:start -->\nbody\n", "end marker"),
    ("<!-- generated:module-map:end -->\n<!-- generated:module-map:start -->\n", "before"),
    ("<!-- generated:module-map:start -->\n<!-- generated:module-map:end -->\n" * 2, "twice"),
])
def test_damaged_or_missing_markers_are_an_error_not_a_silent_skip(text, message):
    with pytest.raises(rules.DocsError, match=message):
        rules.replace_region(text, "module-map", "x")


def test_region_names_found_in_a_file_are_listed_so_an_orphan_region_can_be_reported():
    assert rules.region_names(doc_with("b-one", "a-two")) == ["a-two", "b-one"]


def test_the_instruction_block_marker_convention_is_not_mistaken_for_a_generated_region():
    text = "<!-- instruction-block:start -->\nx\n<!-- instruction-block:end -->\n"
    assert rules.region_names(text) == []


# --------------------------------------------------------------------- rendering


def test_roles_come_from_the_tach_layer_with_two_declared_refinements():
    mods = {m.name: m for m in rules.module_docs(inputs())}
    assert mods["knowledge"].role == "driving adapter"
    assert mods["seed_things"].role == "ETL adapter (driving, batch)"
    assert mods["build_rules"].role == "pipeline rules"
    assert mods["thing"].role == "composition root"
    assert mods["wiring"].role == "composition helper"
    assert mods["thing_store"].role == "infrastructure"
    assert mods["thing_service"].role == "application service"
    assert mods["thing_port"].role == "domain service"
    assert mods["vocab"].role == "model"
    assert mods["knowledge"].layer == "cli"


def test_client_roles_are_inferred_without_tach_entries():
    client_sources = {"feed_service": '"""Runs the feed use case."""\n',
                      "feed_store": '"""Stores feed rows."""\n',
                      "feed_port": '"""Ports for feed rows."""\n',
                      "feed_step": '"""Loads the client feed."""\n'}
    docs = rules.module_docs(inputs(module_sources={**SOURCES, **client_sources},
                                   client_modules=tuple(client_sources)))
    roles = {d.name: (d.layer, d.role) for d in docs if d.layer == "client"}
    assert roles == {"feed_port": ("client", "domain service"),
                     "feed_service": ("client", "application service"),
                     "feed_step": ("client", rules.PIPELINE_ETL),
                     "feed_store": ("client", "infrastructure")}


def test_a_package_module_is_listed_under_its_bare_name():
    assert "seed_things" in {m.name for m in rules.module_docs(inputs())}


def test_a_module_missing_from_tach_or_a_layer_unknown_to_the_roles_is_an_error():
    with pytest.raises(rules.DocsError, match="stray"):
        rules.module_docs(inputs(module_sources={**SOURCES, "stray": '"""Not classified."""\n'}))
    odd = TACH.replace('layer = "model"', 'layer = "mystery"')
    with pytest.raises(rules.DocsError, match="mystery"):
        rules.module_docs(inputs(tach_text=odd))


def test_a_tach_module_with_no_source_is_an_error():
    sources = {k: v for k, v in SOURCES.items() if k != "vocab"}
    with pytest.raises(rules.DocsError, match="vocab"):
        rules.module_docs(inputs(module_sources=sources))


def test_the_summary_is_the_first_sentence_and_long_summaries_are_compacted():
    assert rules.module_summary("m", '"""One line.\n\nMore.\n"""\n') == "One line."
    assert rules.module_summary("m", '"""First line continues\non the next line. Then more.\n"""') == "First line continues on the next line."
    assert rules.module_summary("m", '"""' + ("long summary " * 30) + '"""')[-1] == "…"
    for bad in ('x = 1\n', '"""   """\n'):
        with pytest.raises(rules.DocsError, match="m"):
            rules.module_summary("m", bad)


def test_the_module_map_is_sorted_by_ring_then_name_and_escapes_pipes():
    body = rules.render(inputs())["module-map"]
    rows = [l for l in body.splitlines() if l.startswith("| `")]
    names = [re.match(r"\| `([a-z_]+)`", l).group(1) for l in rows]
    assert names == ["knowledge", "build_rules", "seed_things", "thing", "wiring", "thing_store", "thing_service",
                     "thing_port", "vocab"]
    assert r"Load things \| with a pipe." in body
    assert "| Module | Layer | Role | What it is |" in body


def test_requirements_come_from_pyproject():
    body = rules.render(inputs())["requirements"]
    assert "Python 3.11 or later" in body
    assert "`typer>=0.12`" in body
    assert "`mypy>=1.11`" in body and "`pytest>=8`" in body
    assert body.index("`mypy>=1.11`") < body.index("`pytest>=8`"), "dependency lists are sorted"


def test_a_requires_python_that_is_not_a_plain_lower_bound_is_shown_verbatim():
    body = rules.render(inputs(pyproject_text=PYPROJECT.replace(">=3.11", ">=3.11,<3.14")))["requirements"]
    assert "`>=3.11,<3.14`" in body


def test_the_command_table_lists_nested_commands_and_the_first_sentence_of_help():
    body = rules.render(inputs())["cli-commands"]
    assert "| `search TERMS` | Search facts. |" in body
    assert "| `subject list` | List subjects. |" in body
    assert "More detail" not in body


def test_the_command_reference_lists_options_and_help():
    body = rules.render(inputs())["cli-reference"]
    assert "### `search TERMS`" in body
    assert "- `--limit N`: Maximum rows." in body
    assert "- `--json`: Print JSON." in body
    assert "### `subject list`" in body


def test_rendering_is_deterministic_and_independent_of_input_order():
    a = rules.render(inputs())
    b = rules.render(inputs(commands=tuple(reversed(COMMANDS)),
                            module_sources=dict(reversed(list(SOURCES.items())))))
    assert a == b


def test_prose_is_escaped_outside_code_spans_only():
    c = rules.CommandDoc(path=("x",), help="Reads ~/a/*/b.md and `keeps *this* as is` <now>.")
    body = rules.render(inputs(commands=(c,)))["cli-commands"]
    assert r"`x` | Reads ~/a/\*/b.md and `keeps *this* as is` \<now\>." in body


def test_rendered_output_has_no_timestamps_or_absolute_paths():
    for name, body in rules.render(inputs()).items():
        assert not re.search(r"\d{4}-\d{2}-\d{2}", body), name
        assert "/Users/" not in body and "/home/" not in body, name


def test_every_target_region_is_rendered_and_every_rendered_region_has_a_target():
    assert set(rules.render(inputs())) == {r for regions in rules.TARGETS.values() for r in regions}
    flat = [r for regions in rules.TARGETS.values() for r in regions]
    assert len(flat) == len(set(flat)), "a region belongs to exactly one file"


# --------------------------------------------------------------------- the use case, against fakes


class FakeFiles:
    def __init__(self, files, modules=None):
        self.files = dict(files)
        self.modules = dict(SOURCES if modules is None else modules)
        self.writes = []

    def read(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]

    def write(self, path, text):
        self.writes.append(path)
        self.files[path] = text

    def module_sources(self):
        return dict(self.modules)


def _world(**over):
    files = {
        "tach.toml": TACH,
        "pyproject.toml": PYPROJECT,
        "README.md": doc_with("requirements", "cli-commands"),
        "docs/architecture.md": doc_with("module-map"),
        "docs/cli.md": doc_with("cli-reference"),
    }
    files.update(over)
    return FakeFiles(files)


def _deps(files):
    return docs_service.DocsDeps(docs_files=files)


def test_check_reports_every_stale_region_and_build_fixes_exactly_those():
    files = _world()
    deps = _deps(files)
    stale = docs_service.check(deps, COMMANDS)
    assert sorted(stale) == ["README.md: cli-commands", "README.md: requirements", "docs/architecture.md: module-map",
                             "docs/cli.md: cli-reference"]
    changed = docs_service.build(deps, COMMANDS)
    assert sorted(changed) == ["README.md", "docs/architecture.md", "docs/cli.md"]
    assert docs_service.check(deps, COMMANDS) == []


def test_build_writes_nothing_when_everything_is_current():
    files = _world()
    deps = _deps(files)
    docs_service.build(deps, COMMANDS)
    files.writes.clear()
    assert docs_service.build(deps, COMMANDS) == []
    assert files.writes == []


def test_check_never_writes():
    files = _world()
    docs_service.check(_deps(files), COMMANDS)
    assert files.writes == []


def test_a_hand_edit_inside_a_generated_region_is_caught_and_outside_it_is_not():
    files = _world()
    deps = _deps(files)
    docs_service.build(deps, COMMANDS)
    arch = files.files["docs/architecture.md"]
    files.files["docs/architecture.md"] = arch + "\nA new hand-written paragraph.\n"
    assert docs_service.check(deps, COMMANDS) == []
    files.files["docs/architecture.md"] = arch.replace("| `vocab` |", "| `vocab` | edited by hand |", 1)
    assert docs_service.check(deps, COMMANDS) == ["docs/architecture.md: module-map"]


def test_a_code_change_makes_the_committed_doc_stale():
    files = _world()
    deps = _deps(files)
    docs_service.build(deps, COMMANDS)
    files.modules["vocab"] = '"""Allowed values, now described differently."""\n'
    assert docs_service.check(deps, COMMANDS) == ["docs/architecture.md: module-map"]
    files.modules["extra"] = '"""A new module."""\n'
    with pytest.raises(rules.DocsError, match="extra"):
        docs_service.check(deps, COMMANDS)


def test_a_missing_target_file_or_marker_is_reported_by_check_and_raised_by_build():
    deps = _deps(_world(**{"docs/cli.md": "# CLI\n\nno markers\n"}))
    assert any("docs/cli.md" in p and "cli-reference" in p and "marker" in p for p in docs_service.check(deps, COMMANDS))
    with pytest.raises(rules.DocsError, match="cli-reference"):
        docs_service.build(deps, COMMANDS)
    files = _world()
    del files.files["README.md"]
    assert any(p.startswith("README.md") for p in docs_service.check(_deps(files), COMMANDS))


def test_a_build_that_would_fail_midway_writes_nothing():
    files = _world(**{"docs/cli.md": "# CLI\n"})
    with pytest.raises(rules.DocsError):
        docs_service.build(_deps(files), COMMANDS)
    assert files.writes == []


def test_an_orphan_generated_region_in_a_file_is_a_problem():
    files = _world(**{"README.md": doc_with("requirements", "cli-commands", "no-such-generator")})
    problems = docs_service.check(_deps(files), COMMANDS)
    assert "README.md: no-such-generator (no generator by that name)" in problems


# --------------------------------------------------------------------- the real tree


@pytest.fixture(scope="module")
def commands():
    return cli_docs.command_docs(typer.main.get_command(knowledge.app))


def _deps_real():
    return docs_service.DocsDeps(docs_files=docs_store.RepoDocs(REPO))


def test_committed_docs_match_the_generated_ones(commands):
    stale = docs_service.check(_deps_real(), commands)
    assert stale == [], ("generated docs are stale: " + ", ".join(stale)
                         + "\nrun `uv run python knowledge.py docs build` and commit the result")


def test_docs_check_command_exits_zero_on_the_committed_tree(capsys):
    assert knowledge.main(["docs", "check"]) == 0


def test_docs_check_command_exits_non_zero_and_names_the_region_when_stale(tmp_path, capsys):
    import shutil
    for name in ("README.md", "tach.toml", "pyproject.toml"):
        shutil.copy(os.path.join(REPO, name), tmp_path / name)
    shutil.copytree(os.path.join(REPO, "docs"), tmp_path / "docs")
    for package in ("ingest", "client"):
        shutil.copytree(os.path.join(REPO, package), tmp_path / package, ignore=shutil.ignore_patterns("__pycache__"))
    for f in os.listdir(REPO):
        if f.endswith(".py"):
            shutil.copy(os.path.join(REPO, f), tmp_path / f)
    readme = (tmp_path / "README.md").read_text()
    (tmp_path / "README.md").write_text(readme.replace("Python 3.11 or later", "Python 9 or later"))
    root = ["--root", str(tmp_path)]
    assert knowledge.main(["docs", "check", *root]) == 1
    err = capsys.readouterr().err
    assert "README.md: requirements" in err and "knowledge.py docs build" in err
    assert knowledge.main(["docs", "build", *root]) == 0
    assert knowledge.main(["docs", "check", *root]) == 0
    assert "Python 3.11 or later" in (tmp_path / "README.md").read_text()


def test_docs_commands_are_registered_for_cli_parity():
    assert cli_parity.ACTIONS["docs_build"] == ("docs", "build")
    assert cli_parity.ACTIONS["docs_check"] == ("docs", "check")


def test_the_generator_needs_no_private_data_directory(monkeypatch, tmp_path):
    for var in ("KNOWLEDGE_PRIVATE_DIR", "PRIVATE_DATA_DIR", "KNOWLEDGE_DB_DIR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert knowledge.main(["docs", "check"]) == 0


def test_generated_regions_hold_no_local_path_or_personal_fact():
    root = REPO
    for rel, names in rules.TARGETS.items():
        text = open(os.path.join(root, rel), encoding="utf-8").read()
        for name in names:
            body = rules.extract_region(text, name)
            assert not re.search(r"(/Users/|/home/|[A-Za-z]:\\\\)", body), (rel, name)
            assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", body), (rel, name)


def test_every_module_has_a_complete_one_line_summary():
    sources = docs_store.RepoDocs(REPO).module_sources()
    problems = []
    for name, source in sorted(sources.items()):
        try:
            rules.module_summary(name, source)
        except rules.DocsError as e:
            problems.append(str(e))
    assert problems == [], "\n".join(problems)


def test_every_generated_region_in_the_repo_belongs_to_a_known_generator():
    found = {}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".git", ".venv", "__pycache__", ".claude", "tests"}]
        for f in files:
            if f.endswith(".md"):
                rel = os.path.relpath(os.path.join(root, f), REPO).replace(os.sep, "/")
                names = rules.region_names(open(os.path.join(root, f), encoding="utf-8").read())
                if names:
                    found[rel] = names
    assert found == {path: sorted(names) for path, names in rules.TARGETS.items()}


def test_cli_reference_covers_every_registered_command(commands):
    paths = {" ".join(c.path) for c in commands}
    for path in cli_parity.ACTIONS.values():
        assert " ".join(path) in paths, path


def test_command_docs_are_plain_data(commands):
    assert commands, "no commands found"
    for c in commands:
        json.dumps({"path": c.path, "help": c.help})
        assert all(isinstance(o.names, tuple) and o.names for o in c.options)
