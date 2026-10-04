"""knowledge.py query commands (search/show/subjects/facts): pins the exact CLI
output and exit codes, then (issue #18) the row-returning functions and the
read-only connection.

Everything runs against a small fixture DB built from the repo's real schema.sql
in a temp dir, reached via KNOWLEDGE_PRIVATE_DIR, so the live db is never touched.
"""
import os
import sqlite3
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWLEDGE = os.path.join(REPO, "knowledge.py")


class Env:
    def __init__(self, root, with_db=True):
        self.root = str(root)
        self.private = os.path.join(self.root, "knowledge-private")
        self.db_dir = os.path.join(self.root, "db")
        for d in (self.private, self.db_dir):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(self.private, "local_paths.py"), "w") as f:
            f.write(f"KNOWLEDGE_DB_DIR = {self.db_dir!r}\nPRIVATE_DATA_DIR = {os.path.join(self.root, 'data')!r}\n")
            for n in ("BODYBUILDING_VAULT", "HEALTH_DIR", "CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON"):
                f.write(f"{n} = {os.path.join(self.root, 'unused')!r}\n")
        self.db = os.path.join(self.db_dir, "knowledge.db")
        self.env = {**os.environ, "KNOWLEDGE_PRIVATE_DIR": self.private}
        if with_db:
            self.build_db()

    def build_db(self):
        con = sqlite3.connect(self.db)
        with open(os.path.join(REPO, "schema.sql")) as f:
            con.executescript(f.read())
        con.executescript("""
            INSERT INTO subjects (id, name, domain, parent_id) VALUES
              (1, 'nutrition', 'health-and-fitness', NULL),
              (2, 'protein', 'health-and-fitness', 1),
              (3, 'emptysubject', 'health-and-fitness', NULL),
              (4, 'jazz', 'music', NULL);
            INSERT INTO vault_files (id, path) VALUES (1, 'vault/protein.md');
            INSERT INTO sources (id, citekey, name, source_type) VALUES
              (1, 'smith2020', 'Smith 2020 protein review', 'primary'),
              (2, 'blog1', 'Some blog', 'tertiary');
            INSERT INTO facts (id, subject_id, statement, is_personal, trust_level, trust_rationale,
                               status, recheck_by, recheck_rationale, origin_file_id, notes) VALUES
              (1, 2, 'Protein intake of 1.6 g/kg maximizes hypertrophy', 0, 'high', 'meta-analysis',
                  'active', '2027-01-01', 'new reviews yearly', 1, 'see review'),
              (2, 1, 'Creatine monohydrate is effective', 0, 'verified', NULL, 'active', NULL, NULL, NULL, NULL),
              (3, 2, 'I feel best on 2 g/kg protein', 1, 'low', 'self report', 'active', '2027-02-02', NULL, NULL, NULL),
              (4, 1, 'Old claim about protein timing', 0, 'medium', NULL, 'superseded', NULL, NULL, NULL, NULL),
              (5, 4, 'Coltrane recorded A Love Supreme in 1964', 0, 'high', NULL, 'retracted', NULL, NULL, NULL, NULL);
            INSERT INTO fact_sources (fact_id, source_id, locator, quote) VALUES
              (1, 1, 'p. 12', 'q'), (1, 2, NULL, NULL);
        """)
        con.commit()
        con.close()

    def cli(self, *args):
        return subprocess.run([sys.executable, KNOWLEDGE, *args], env=self.env, cwd=self.root,
                              capture_output=True, text=True)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def ok(r):
    assert r.returncode == 0, r.stderr
    return r.stdout


L1 = "#1     [protein] (high)  Protein intake of 1.6 g/kg maximizes hypertrophy\n"
L2 = "#2     [nutrition] (verified)  Creatine monohydrate is effective\n"
L3 = "#3     [protein] (low)  I feel best on 2 g/kg protein\n"
L4 = "#4     [nutrition] (medium) [superseded]  Old claim about protein timing\n"
L5 = "#5     [jazz] (high) [retracted]  Coltrane recorded A Love Supreme in 1964\n"


# ---------------------------------------------------------------- search

def test_search_basic(env):
    out = ok(env.cli("search", "protein"))
    assert set(out.splitlines(keepends=True)) == {L1, L3, L4}
    assert out.count("\n") == 3


def test_search_filters(env):
    assert ok(env.cli("search", "protein", "--subject", "protein", "--trust", "high")) == L1
    assert ok(env.cli("search", "protein", "--personal-only")) == L3
    assert set(ok(env.cli("search", "protein", "--not-personal")).splitlines(keepends=True)) == {L1, L4}
    assert ok(env.cli("search", "protein", "--limit", "1")).count("\n") == 1


def test_search_no_match(env):
    r = env.cli("search", "zzzznothing")
    assert (r.returncode, r.stdout, r.stderr) == (0, "No matches.\n", "")
    assert ok(env.cli("search", "protein", "--subject", "nonexistent")) == "No matches.\n"


def test_search_personal_flags_mutually_exclusive(env):
    assert env.cli("search", "protein", "--personal-only", "--not-personal").returncode == 2


def test_search_prefix_and_quoted_phrase(env):
    assert set(ok(env.cli("search", "protei*")).splitlines(keepends=True)) == {L1, L3, L4}
    assert ok(env.cli("search", '"feel best"')) == L3


@pytest.mark.parametrize("q", ["", '"unbalanced', "(protein", "protein)", "a AND", "'", "*"])
def test_search_fts_syntax_errors_are_pinned(env, q):
    """FTS syntax errors used to escape as an uncaught sqlite3.OperationalError traceback.
    Since the Typer migration (#34) the CLI layer catches it: one-line error, exit 1.
    The library function still raises (see test_functions_return_dicts...)."""
    r = env.cli("search", q)
    assert r.returncode == 1
    assert r.stdout == ""
    assert r.stderr.startswith("error: search failed: ") and r.stderr.count("\n") == 1
    assert "Traceback" not in r.stderr


def test_search_leading_dash_is_a_cli_option_and_fts_column_filter(env):
    assert env.cli("search", "-protein").returncode == 2  # the CLI parser sees an unknown option
    r = env.cli("search", "--", "-protein")  # reaches FTS, where it is read as a column filter
    assert r.returncode == 1 and r.stderr.startswith("error: search failed: ")


# ---------------------------------------------------------------- show

def test_show_full(env):
    assert ok(env.cli("show", "1")) == (
        "Fact #1  [protein]  trust=high  personal=False  status=active\n"
        "\nProtein intake of 1.6 g/kg maximizes hypertrophy\n\n"
        "Trust rationale: meta-analysis\n"
        "Notes: see review\n"
        "Origin: vault/protein.md\n"
        "Recheck by: 2027-01-01  (new reviews yearly)\n"
        "\nSources:\n"
        "  - Smith 2020 protein review (p. 12)\n"
        "  - Some blog\n"
    )


def test_show_minimal_and_partial_recheck(env):
    assert ok(env.cli("show", "2")) == (
        "Fact #2  [nutrition]  trust=verified  personal=False  status=active\n"
        "\nCreatine monohydrate is effective\n\n"
    )
    assert ok(env.cli("show", "3")) == (
        "Fact #3  [protein]  trust=low  personal=True  status=active\n"
        "\nI feel best on 2 g/kg protein\n\n"
        "Trust rationale: self report\n"
        "Recheck by: 2027-02-02\n"
    )


def test_show_unknown_id(env):
    r = env.cli("show", "999")
    assert (r.returncode, r.stdout, r.stderr) == (1, "", "error: no fact with id 999\n")


def test_show_non_integer(env):
    assert env.cli("show", "abc").returncode == 2


# ---------------------------------------------------------------- subjects

def test_subjects_listing_includes_empty_subject(env):
    assert ok(env.cli("subjects")) == SUBJECTS_OUT


SUBJECTS_OUT = (
    "emptysubject                        (health-and-fitness, 0 facts)\n"
    "nutrition                           (health-and-fitness, 2 facts)\n"
    "  protein                             (health-and-fitness, 2 facts)\n"
    "jazz                                (music, 1 facts)\n"
)


# ---------------------------------------------------------------- facts

def test_facts_all_ordered_by_id(env):
    assert ok(env.cli("facts")) == L1 + L2 + L3 + L4 + L5


def test_facts_filters(env):
    assert ok(env.cli("facts", "--subject", "protein")) == L1 + L3
    assert ok(env.cli("facts", "--trust", "high")) == L1 + L5
    assert ok(env.cli("facts", "--status", "superseded")) == L4
    assert ok(env.cli("facts", "--status", "active")) == L1 + L2 + L3
    assert ok(env.cli("facts", "--personal-only")) == L3
    assert ok(env.cli("facts", "--not-personal")) == L1 + L2 + L4 + L5
    assert ok(env.cli("facts", "--not-personal", "--trust", "high", "--status", "retracted", "--subject", "jazz")) == L5
    assert ok(env.cli("facts", "--limit", "2")) == L1 + L2


def test_facts_no_matches_and_empty_subject(env):
    assert ok(env.cli("facts", "--subject", "emptysubject")) == "No matches.\n"
    assert ok(env.cli("facts", "--subject", "nope")) == "No matches.\n"


def test_facts_bad_choice_and_exclusive(env):
    assert env.cli("facts", "--trust", "bogus").returncode == 2
    assert env.cli("facts", "--status", "bogus").returncode == 2
    assert env.cli("facts", "--personal-only", "--not-personal").returncode == 2


# ---------------------------------------------------------------- missing db

@pytest.mark.parametrize("cmd", [["search", "x"], ["show", "1"], ["subjects"], ["facts"]])
def test_missing_db_message_and_exit_code(tmp_path, cmd):
    e = Env(tmp_path, with_db=False)
    r = e.cli(*cmd)
    assert r.returncode == 1
    assert r.stdout == ""
    assert r.stderr == f"error: {e.db} doesn't exist -- run `python3 knowledge.py build` first\n"


# ---------------------------------------------------------------- library functions + read-only (issue #18)

@pytest.fixture
def lib(env, monkeypatch):
    """knowledge.py imported fresh against the fixture's private dir."""
    import importlib.util
    monkeypatch.setenv("KNOWLEDGE_PRIVATE_DIR", env.private)
    for m in ("paths", "local_paths"):
        sys.modules.pop(m, None)
    spec = importlib.util.spec_from_file_location("knowledge_under_test", KNOWLEDGE)
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(REPO)
    spec.loader.exec_module(mod)
    yield mod
    for m in ("paths", "local_paths"):
        sys.modules.pop(m, None)


def test_connect_is_read_only(lib, env):
    con = lib.connect()
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("UPDATE facts SET statement = 'x' WHERE id = 1")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM facts")
    finally:
        con.close()
    con = sqlite3.connect(env.db)
    assert con.execute("SELECT statement FROM facts WHERE id = 1").fetchone()[0].startswith("Protein")
    con.close()


def test_connect_missing_db_raises_not_exits(lib, env):
    os.remove(env.db)
    with pytest.raises(lib.DatabaseNotFound, match="run `python3 knowledge.py build` first"):
        lib.connect()
    assert not os.path.exists(env.db)  # ro open must not create it either


def test_connect_path_with_uri_special_characters(lib, tmp_path):
    import shutil
    odd = tmp_path / "we ird#dir?x"
    odd.mkdir()
    dst = odd / "k.db"
    shutil.copy(lib.DB_PATH, dst)
    con = lib.connect(str(dst))
    assert con.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 5
    con.close()


def test_functions_return_dicts_and_never_print_or_exit(lib, capsys):
    con = lib.connect()
    hits = lib.search_facts(con, "protein", personal=False)
    assert {h["id"] for h in hits} == {1, 4}
    assert set(hits[0]) == {"id", "subject", "trust_level", "status", "statement"}
    assert [r["id"] for r in lib.list_facts(con, status="active", personal=True)] == [3]
    assert [r["id"] for r in lib.list_facts(con)] == [1, 2, 3, 4, 5]
    assert lib.list_facts(con, subject="emptysubject") == []
    assert lib.search_facts(con, "zzzz") == []
    assert lib.get_fact(con, 999) is None
    f = lib.get_fact(con, 1)
    assert f["subject"] == "protein" and f["origin_path"] == "vault/protein.md"
    assert f["sources"] == [{"name": "Smith 2020 protein review", "locator": "p. 12"},
                            {"name": "Some blog", "locator": None}]
    assert lib.get_fact(con, 2)["sources"] == []
    subs = {s["name"]: s for s in lib.list_subjects(con)}
    assert subs["emptysubject"]["n_facts"] == 0 and subs["protein"]["parent"] == "nutrition"
    with pytest.raises(sqlite3.OperationalError):
        lib.search_facts(con, '"unbalanced')
    con.close()
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_main_converts_missing_db_to_message_and_exit_code(lib, env, capsys):
    os.remove(env.db)
    assert lib.main(["facts"]) == 1
    assert capsys.readouterr().err == f"error: {env.db} doesn't exist -- run `python3 knowledge.py build` first\n"


# ---------------------------------------------------------------- --json (issue #34)

def test_json_flag_on_every_read_command(env):
    import json
    hits = json.loads(ok(env.cli("search", "protein", "--not-personal", "--json")))
    assert {h["id"] for h in hits} == {1, 4}
    assert set(hits[0]) == {"id", "subject", "trust_level", "status", "statement"}
    assert json.loads(ok(env.cli("search", "zzzznothing", "--json"))) == []
    assert [r["id"] for r in json.loads(ok(env.cli("facts", "--status", "active", "--json")))] == [1, 2, 3]
    assert json.loads(ok(env.cli("facts", "--subject", "nope", "--json"))) == []
    f = json.loads(ok(env.cli("show", "1", "--json")))
    assert f["subject"] == "protein" and f["sources"][0] == {"name": "Smith 2020 protein review", "locator": "p. 12"}
    subs = {s["name"]: s for s in json.loads(ok(env.cli("subjects", "--json")))}
    assert subs["protein"]["parent"] == "nutrition" and subs["emptysubject"]["n_facts"] == 0


def test_json_errors_stay_on_stderr_with_same_exit_code(env, tmp_path):
    r = env.cli("show", "999", "--json")
    assert (r.returncode, r.stdout, r.stderr) == (1, "", "error: no fact with id 999\n")
    e = Env(tmp_path / "nodb", with_db=False)
    r = e.cli("facts", "--json")
    assert r.returncode == 1 and r.stdout == "" and "run `python3 knowledge.py build` first" in r.stderr


def test_help_works_for_app_and_every_command(env):
    assert "search" in ok(env.cli("--help"))
    for c in ("build", "clean-concerts", "search", "show", "subjects", "facts"):
        assert ok(env.cli(c, "--help")).strip(), c
    assert "--json" in ok(env.cli("facts", "--help"))


def test_add_fact_help_is_forwarded_to_add_fact_py(env):
    assert "usage:" in ok(env.cli("add-fact", "--help")).lower()
