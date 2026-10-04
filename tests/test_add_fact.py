"""add_fact: characterization of today's behavior, then the new guarantees from
issue #16 (importable functions, locked atomic append, clean errors).

Every test redirects the data/db locations to a temp dir via KNOWLEDGE_PRIVATE_DIR
(issue #15), so nothing here can touch the real general_facts.json.
"""
import concurrent.futures
import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADD_FACT = os.path.join(REPO, "add_fact.py")
KNOWLEDGE = os.path.join(REPO, "knowledge.py")

ORDERED_FULL_KEYS = ["source_key", "subject", "statement", "trust_level", "is_original_claim", "is_personal", "date_added", "visibility",
                     "domain", "trust_rationale", "notes", "recheck_by", "recheck_rationale",
                     "source_citekey", "source_locator", "source_quote"]


class Env:
    """A throwaway private repo layout: data dir, db dir, and the env for subprocesses."""
    def __init__(self, root):
        self.root = str(root)
        self.private = os.path.join(self.root, "knowledge-private")
        self.data_dir = os.path.join(self.root, "data")
        self.db_dir = os.path.join(self.root, "db")
        for d in (self.private, self.data_dir, self.db_dir):
            os.makedirs(d, exist_ok=True)
        with open(os.path.join(self.private, "local_paths.py"), "w") as f:
            f.write(f"KNOWLEDGE_DB_DIR = {self.db_dir!r}\nPRIVATE_DATA_DIR = {self.data_dir!r}\n")
            for n in ("BODYBUILDING_VAULT", "HEALTH_DIR", "CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON"):
                f.write(f"{n} = {os.path.join(self.root, 'unused')!r}\n")
        self.facts = os.path.join(self.data_dir, "general_facts.json")
        self.db = os.path.join(self.db_dir, "knowledge.db")
        self.env = {**os.environ, "KNOWLEDGE_PRIVATE_DIR": self.private}

    def make_db(self, *citekeys):
        con = sqlite3.connect(self.db)
        con.execute("CREATE TABLE sources (citekey TEXT)")
        con.executemany("INSERT INTO sources VALUES (?)", [(c,) for c in citekeys])
        con.commit()
        con.close()

    def cli(self, *args, via="add_fact"):
        cmd = [sys.executable, ADD_FACT, *args] if via == "add_fact" else [sys.executable, KNOWLEDGE, "add-fact", *args]
        return subprocess.run(cmd, env=self.env, cwd=self.root, capture_output=True, text=True)

    def entries(self):
        with open(self.facts) as f:
            return json.load(f)

    def raw(self):
        with open(self.facts, "rb") as f:
            return f.read()


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


# ---------------------------------------------------------------- characterization (today's behavior)

def test_minimal_add_shape_and_defaults(env):
    r = env.cli("  Hello world.  ", "--subject", "car-maintenance", "--trust", "medium")
    assert r.returncode == 0, r.stderr
    (entry,) = env.entries()
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00", entry.pop("date_added"))  # #19
    assert entry.pop("visibility") == "private"  # #20
    assert re.fullmatch(r"f-[0-9a-f]{12}", entry.pop("source_key"))  # #30
    assert entry == {
        "subject": "car-maintenance", "statement": "Hello world.", "trust_level": "medium",
        "is_original_claim": False, "is_personal": True,
    }
    assert "Added to " in r.stdout and "general_facts.json" in r.stdout and "(1 facts total)" in r.stdout
    assert "python3 build.py" in r.stdout


def test_all_options_and_key_order(env):
    env.make_db("ck1")
    r = env.cli("Full fact.", "--subject", "x", "--trust", "high", "--domain", "health",
                "--original-claim", "--not-personal", "--trust-rationale", "tr", "--notes", "n",
                "--recheck-by", "2026-12-01", "--recheck-rationale", "rr", "--source-citekey", "ck1",
                "--source-locator", "p. 4", "--source-quote", "q")
    assert r.returncode == 0, r.stderr
    (entry,) = env.entries()
    assert list(entry.keys()) == ORDERED_FULL_KEYS
    assert entry["is_original_claim"] is True and entry["is_personal"] is False
    assert entry["domain"] == "health"


def test_domain_general_is_omitted(env):
    env.cli("A.", "--subject", "x", "--trust", "low", "--domain", "general")
    assert "domain" not in env.entries()[0]


def test_file_format_is_indent2_with_trailing_newline_and_unicode_kept(env):
    env.cli("Café ☕ fact.", "--subject", "x", "--trust", "low")
    expected = json.dumps(env.entries(), indent=2, ensure_ascii=False) + "\n"
    assert env.raw().decode() == expected
    assert "Café ☕" in env.raw().decode()


def test_appends_and_preserves_existing_entries(env):
    with open(env.facts, "w") as f:
        json.dump([{"statement": "old", "subject": "s", "trust_level": "low"}], f)
    env.cli("New.", "--subject", "x", "--trust", "low")
    got = env.entries()
    assert [e["statement"] for e in got] == ["old", "New."]


@pytest.mark.parametrize("args,needle", [
    (["   ", "--subject", "x", "--trust", "low"], "statement is empty"),
    (["S.", "--subject", "Car-Maintenance", "--trust", "low"], "kebab-case"),
    (["S.", "--subject", "car maintenance", "--trust", "low"], "kebab-case"),
    (["S.", "--subject", "car_maintenance", "--trust", "low"], "kebab-case"),
    (["S.", "--subject", "car--x", "--trust", "low"], "kebab-case"),
    (["S.", "--subject", "", "--trust", "low"], "kebab-case"),
    (["S.", "--subject", "x", "--trust", "low", "--recheck-by", "abc"], "too short"),
    (["S.", "--subject", "x", "--trust", "low", "--source-locator", "p. 1"], "without --source-citekey"),
    (["S.", "--subject", "x", "--trust", "low", "--source-quote", "q"], "without --source-citekey"),
])
def test_rejections_exit_1_and_write_nothing(env, args, needle):
    r = env.cli(*args)
    assert r.returncode == 1
    assert f"error: " in r.stderr and needle in r.stderr
    assert not os.path.exists(env.facts)


def test_subject_with_leading_hyphen_is_read_as_a_flag_by_argparse_exit_2(env):
    # Pinned today: "-car" never reaches validate(); argparse rejects it first.
    r = env.cli("S.", "--subject", "-car", "--trust", "low")
    assert r.returncode == 2 and "expected one argument" in r.stderr
    assert not os.path.exists(env.facts)


def test_invalid_trust_is_argparse_exit_2(env):
    r = env.cli("S.", "--subject", "x", "--trust", "bogus")
    assert r.returncode == 2
    assert not os.path.exists(env.facts)


def test_unknown_citekey_rejected_when_db_exists(env):
    env.make_db("known")
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--source-citekey", "nope")
    assert r.returncode == 1 and "not found in sources table" in r.stderr
    assert not os.path.exists(env.facts)


def test_citekey_check_skipped_with_note_when_db_missing(env):
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--source-citekey", "anything")
    assert r.returncode == 0
    assert "skipping citekey check" in r.stderr
    assert env.entries()[0]["source_citekey"] == "anything"


def test_recheck_by_accepts_date_and_long_phrase_not_short_text(env):
    assert env.cli("A.", "--subject", "x", "--trust", "low", "--recheck-by", "2026-12-01").returncode == 0
    assert env.cli("B.", "--subject", "x", "--trust", "low", "--recheck-by", "next physical").returncode == 0
    assert env.cli("C.", "--subject", "x", "--trust", "low", "--recheck-by", "soon").returncode == 0  # 4 chars passes today
    assert env.cli("D.", "--subject", "x", "--trust", "low", "--recheck-by", "abc").returncode == 1


def test_knowledge_py_add_fact_forwards_to_the_same_behavior(env):
    r = env.cli("Via umbrella.", "--subject", "x", "--trust", "low", via="knowledge")
    assert r.returncode == 0, r.stderr
    assert env.entries()[0]["statement"] == "Via umbrella."
    bad = env.cli("   ", "--subject", "x", "--trust", "low", via="knowledge")
    assert bad.returncode == 1 and "statement is empty" in bad.stderr


# edge inputs (global rule: try them before changing shared behavior). These pin what happens
# today so a change to them is deliberate, not accidental.

def test_edge_very_long_statement_is_accepted(env):
    r = env.cli("x" * 100_000, "--subject", "x", "--trust", "low")
    assert r.returncode == 0
    assert len(env.entries()[0]["statement"]) == 100_000


def test_edge_nul_character_in_statement_is_accepted_today(env):
    # Passing NUL through argv is impossible on POSIX; the function-level API can still receive one.
    # Pinned in test_function_api_nul_in_statement below once the API exists.
    pass


# ---------------------------------------------------------------- new guarantees (#16)

def test_corrupt_existing_file_is_a_clean_error_and_left_untouched(env):
    with open(env.facts, "w") as f:
        f.write("[{ not json")
    before = env.raw()
    r = env.cli("S.", "--subject", "x", "--trust", "low")
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "not valid JSON" in r.stderr and "general_facts.json" in r.stderr
    assert env.raw() == before


def test_non_list_existing_file_is_a_clean_error_and_left_untouched(env):
    with open(env.facts, "w") as f:
        f.write('{"a": 1}')
    before = env.raw()
    r = env.cli("S.", "--subject", "x", "--trust", "low")
    assert r.returncode == 1
    assert "Traceback" not in r.stderr
    assert "JSON array" in r.stderr
    assert env.raw() == before


def test_concurrent_appends_lose_nothing(env):
    n = 24

    def one(i):
        return env.cli(f"Concurrent fact {i}.", "--subject", "x", "--trust", "low")

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        results = list(ex.map(one, range(n)))
    assert all(r.returncode == 0 for r in results), [r.stderr for r in results if r.returncode]
    statements = sorted(e["statement"] for e in env.entries())
    assert statements == sorted(f"Concurrent fact {i}." for i in range(n))


@pytest.fixture(scope="module")
def af(tmp_path_factory):
    """add_fact imported in-process against a throwaway private dir."""
    e = Env(tmp_path_factory.mktemp("afmod"))
    mp = pytest.MonkeyPatch()
    mp.setenv("KNOWLEDGE_PRIVATE_DIR", e.private)
    for m in ("paths", "local_paths", "add_fact_under_test"):
        sys.modules.pop(m, None)
    mp.syspath_prepend(REPO)
    spec = importlib.util.spec_from_file_location("add_fact_under_test", ADD_FACT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    mp.undo()


def fact(af, **kw):
    base = dict(statement="A fact.", subject="x", trust_level="low")
    base.update(kw)
    return af.NewFact(**base)


def test_function_api_append_returns_structured_result(af, tmp_path):
    p = tmp_path / "facts.json"
    res = af.append_fact(fact(af, statement="  Stripped.  "), data_path=str(p), db_path=str(tmp_path / "none.db"))
    assert res.ok and res.errors == [] and res.total == 1
    assert res.entry["statement"] == "Stripped."
    assert json.loads(p.read_text())[0] == res.entry


def test_function_api_validation_errors_are_returned_not_printed(af, tmp_path, capsys):
    p = tmp_path / "facts.json"
    res = af.append_fact(fact(af, statement="  ", subject="Bad Subject"), data_path=str(p), db_path=str(tmp_path / "none.db"))
    assert not res.ok
    assert any("statement is empty" in e for e in res.errors)
    assert any("kebab-case" in e for e in res.errors)
    assert not p.exists()
    assert capsys.readouterr().err == ""


def test_function_api_citekey_note_returned_when_db_missing(af, tmp_path):
    res = af.append_fact(fact(af, source_citekey="k"), data_path=str(tmp_path / "f.json"), db_path=str(tmp_path / "none.db"))
    assert res.ok and any("skipping citekey check" in n for n in res.notes)


def test_function_api_nul_in_statement_is_pinned(af, tmp_path):
    p = tmp_path / "facts.json"
    res = af.append_fact(fact(af, statement="bad\x00fact"), data_path=str(p), db_path=str(tmp_path / "none.db"))
    # Today's behavior (accepted) is preserved by the refactor; making NUL a refusal is a separate,
    # deliberate decision because every caller would then need to handle the new outcome.
    assert res.ok
    assert json.loads(p.read_text())[0]["statement"] == "bad\x00fact"


def test_failed_replace_leaves_original_intact_and_no_temp_files(af, tmp_path, monkeypatch):
    p = tmp_path / "facts.json"
    p.write_text(json.dumps([{"statement": "old"}]))
    before = p.read_bytes()

    def boom(*a, **k):
        raise OSError("disk full")
    monkeypatch.setattr(af.os, "replace", boom)
    res = af.append_fact(fact(af), data_path=str(p), db_path=str(tmp_path / "none.db"))
    assert not res.ok and any("disk full" in e for e in res.errors)
    assert p.read_bytes() == before
    assert os.listdir(tmp_path) == ["facts.json"]  # no temp file, and no lock file inside the data dir


def test_build_entry_matches_cli_shape(af):
    entry = af.build_entry(fact(af, domain="health", notes="n"))
    assert list(entry.keys()) == ["source_key", "subject", "statement", "trust_level", "is_original_claim", "is_personal", "date_added", "visibility", "domain", "notes"]


def test_a_successful_add_leaves_only_the_facts_file_in_the_data_dir(env):
    # The lock file must live outside the data dir: a stray file would dirty knowledge-private's tree.
    env.cli("S.", "--subject", "x", "--trust", "low")
    assert os.listdir(env.data_dir) == ["general_facts.json"]


def test_db_without_a_sources_table_is_a_clean_error(env):
    sqlite3.connect(env.db).close()  # exists, but empty
    r = env.cli("S.", "--subject", "x", "--trust", "low", "--source-citekey", "k")
    assert r.returncode == 1 and "Traceback" not in r.stderr
    assert "could not check --source-citekey" in r.stderr
    assert not os.path.exists(env.facts)


def test_file_permissions_are_preserved_on_rewrite(env):
    with open(env.facts, "w") as f:
        json.dump([], f)
    os.chmod(env.facts, 0o640)
    env.cli("S.", "--subject", "x", "--trust", "low")
    assert (os.stat(env.facts).st_mode & 0o777) == 0o640


# ---------------------------------------------------------------- #19: date_added

def test_cli_writes_a_frozen_utc_date_added(env):
    env.env["KNOWLEDGE_FROZEN_NOW"] = "2026-10-03T08:00:00+00:00"
    assert env.cli("S.", "--subject", "x", "--trust", "low").returncode == 0
    assert env.entries()[0]["date_added"] == "2026-10-03T08:00:00+00:00"


def test_each_add_gets_its_own_date(env):
    env.env["KNOWLEDGE_FROZEN_NOW"] = "2026-10-03T08:00:00+00:00"
    env.cli("A.", "--subject", "x", "--trust", "low")
    env.env["KNOWLEDGE_FROZEN_NOW"] = "2026-10-04T09:30:00+00:00"
    env.cli("B.", "--subject", "x", "--trust", "low")
    assert [e["date_added"] for e in env.entries()] == ["2026-10-03T08:00:00+00:00", "2026-10-04T09:30:00+00:00"]


def test_a_bad_frozen_clock_is_not_silently_replaced_by_the_real_one(env):
    env.env["KNOWLEDGE_FROZEN_NOW"] = "garbage"
    r = env.cli("S.", "--subject", "x", "--trust", "low")
    assert r.returncode != 0 and not os.path.exists(env.facts)
