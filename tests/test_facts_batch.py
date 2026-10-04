"""Issue #32 (library, CLI and protocol text): facts_batch.add_facts, `knowledge.py add-facts`,
docs/register-facts-protocol.md. Temp dirs and temp git repos only; never real knowledge-private data."""
import ast
import concurrent.futures
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from test_fact_revisions import world  # noqa: F401  (fixture)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
RULES = {"version": 1, "subject_tags": {"health": "private", "coffee": "normal", "plans": "normal"}, "keywords": ["alice"]}


@pytest.fixture
def fb(world, monkeypatch):
    sys.modules.pop("facts_batch", None)
    import facts_batch
    world.fb = facts_batch
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    world.env.env.update(GIT_ENV)
    with open(os.path.join(world.env.data_dir, "privacy_rules.json"), "w") as f:
        json.dump(RULES, f)
    return world


def it(statement="I drink dark roast coffee.", subject="coffee", **kw):
    return {"statement": statement, "subject": subject, **kw}


def git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


def make_repo(w):
    d = w.env.data_dir
    git(d, "init", "-q")
    git(d, "config", "user.name", "Test")
    git(d, "config", "user.email", "t@example.com")
    git(d, "add", "-A")
    git(d, "commit", "-q", "-m", "init", "--allow-empty")
    return d


def subjects_db(w, *names):
    con = sqlite3.connect(w.env.db)
    con.execute("CREATE TABLE subjects (id INTEGER PRIMARY KEY, name TEXT, parent_id INTEGER)")
    con.executemany("INSERT INTO subjects (name) VALUES (?)", [(n,) for n in names])
    con.commit()
    con.close()


def digest(path):
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def commits(d):
    return int(git(d, "rev-list", "--count", "HEAD"))


# ------------------------------------------------------------------ the happy path

def test_batch_is_saved_active_with_provenance_in_input_order(fb):
    w = fb
    r = w.fb.add_facts([it("A one."), it("B two.", subject="plans", notes="n", trust_level="high"), it("C three.")],
                       session_id="s1", commit=False)
    assert r.ok and [i.outcome for i in r.items] == ["saved"] * 3 and r.total == 3
    e = w.env.entries()
    assert [x["statement"] for x in e] == ["A one.", "B two.", "C three."]
    assert all(x["status"] == "active" and x["captured_via"] == "register-facts" and x["session_id"] == "s1" and x["captured_at"] for x in e)
    assert len({x["source_key"] for x in e}) == 3
    assert [i.source_key for i in r.items] == [x["source_key"] for x in e]
    assert e[1]["notes"] == "n" and e[1]["trust_level"] == "high" and e[0]["trust_level"] == "unverified"


def test_status_pending_and_validation_of_status(fb):
    w = fb
    assert w.fb.add_facts([it()], status="pending", commit=False).ok
    assert w.env.entries()[0]["status"] == "pending"
    bad = w.fb.add_facts([it("Other.")], status="superseded", commit=False)
    assert not bad.ok and len(w.env.entries()) == 1


def test_statements_and_dates_are_stored_exactly_as_given_including_unicode(fb):
    w = fb
    s = ["Starting next week I run 5 km before March 14.", "Café ☕ naïve 日本語 é — dash.", "  padded outside  "]
    assert w.fb.add_facts([it(x, subject="plans", recheck_by="next physical") for x in s], commit=False).ok
    assert [x["statement"] for x in w.env.entries()] == [s[0], s[1], s[2].strip()]
    assert all(x["recheck_by"] == "next physical" for x in w.env.entries())
    assert "日本語" in open(w.env.facts, encoding="utf-8").read()  # ensure_ascii=False, not \u escapes


def test_the_requested_visibility_is_only_an_input_and_cannot_lower(fb):
    w = fb
    r = w.fb.add_facts([it("Plain."), it("Asked private.", visibility="private"), it("Health thing.", subject="health", visibility="normal"),
                        it("Ask Alice about it.", visibility="normal")], commit=False)
    assert [i.visibility for i in r.items] == ["normal", "private", "private", "private"]
    assert [i.raised for i in r.items] == [False, False, True, True]
    assert r.items[0].rule.startswith("normal")
    assert "tagged private" in r.items[2].rule and "alice" in r.items[3].rule
    assert [x["visibility"] for x in w.env.entries()] == ["normal", "private", "private", "private"]


def test_a_subject_unknown_to_the_db_resolves_private_for_every_item_of_the_batch(fb):
    w = fb
    subjects_db(w, "gardening")
    r = w.fb.add_facts([it("One.", subject="gardening"), it("Two.", subject="brand-new"), it("Three.", subject="brand-new")], commit=False)
    assert [i.visibility for i in r.items] == ["normal", "private", "private"]
    assert "does not exist yet" in r.items[1].rule and r.items[1].raised


# ------------------------------------------------------------------ atomicity

def test_one_invalid_item_writes_nothing_and_every_item_gets_an_outcome(fb):
    w = fb
    r = w.fb.add_facts([it("Good."), it("Bad subject.", subject="Not Kebab"), it("Also good.")], commit=False)
    assert not r.ok and not r.errors
    assert [i.outcome for i in r.items] == ["not_saved", "invalid", "not_saved"]
    assert "kebab" in r.items[1].errors[0]
    assert not os.path.exists(w.env.facts)


def test_invalid_item_leaves_an_existing_file_byte_identical(fb):
    w = fb
    assert w.fb.add_facts([it("First.")], commit=False).ok
    before = digest(w.env.facts)
    assert not w.fb.add_facts([it("Second."), {"statement": "", "subject": "coffee"}], commit=False).ok
    assert digest(w.env.facts) == before


@pytest.mark.parametrize("bad", [
    {"statement": "x"}, {"subject": "coffee"}, {"statement": "x", "subject": "coffee", "bogus": 1},
    {"statement": "x", "subject": "coffee", "source_key": "k1"}, {"statement": "x", "subject": "coffee", "status": "pending"},
    {"statement": "x", "subject": "coffee", "captured_via": "evil"}, {"statement": 5, "subject": "coffee"},
    {"statement": "x", "subject": None}, {"statement": "x", "subject": "coffee", "notes": ["a"]},
    {"statement": "x", "subject": "coffee", "is_personal": "yes"}, {"statement": "x", "subject": "coffee", "trust_level": "great"},
    {"statement": "x", "subject": "coffee", "visibility": "public"}, {"statement": "   ", "subject": "coffee"},
    "just a string", None, 7, [],
    {"statement": "x\u0000y", "subject": "coffee\u0000"},
])
def test_malformed_items_are_reported_not_raised(fb, bad):
    w = fb
    r = w.fb.add_facts([it("Fine."), bad], commit=False)
    assert not r.ok and r.items[1].outcome == "invalid" and r.items[1].errors
    assert r.items[0].outcome == "not_saved" and not os.path.exists(w.env.facts)


def test_bad_batch_shapes(fb):
    w = fb
    for bad in ("text", {"statement": "x"}, None, 5):
        r = w.fb.add_facts(bad, commit=False)
        assert not r.ok and r.errors and not r.items
    r = w.fb.add_facts([it(f"Fact {i}.") for i in range(w.fb.MAX_BATCH + 1)], commit=False)
    assert not r.ok and "limit" in r.errors[0] and not os.path.exists(w.env.facts)


def test_empty_list_is_ok_writes_nothing_and_ignores_a_dirty_tree(fb):
    w = fb
    d = make_repo(w)
    open(os.path.join(d, "stray.txt"), "w").write("x")
    n = commits(d)
    r = w.fb.add_facts([])
    assert r.ok and not r.items and r.commit is None and not os.path.exists(w.env.facts) and commits(d) == n


def test_corrupt_facts_file_is_a_batch_error_and_stays_untouched(fb):
    w = fb
    open(w.env.facts, "w").write("{not json")
    r = w.fb.add_facts([it()], commit=False)
    assert not r.ok and r.errors and "not valid JSON" in r.errors[0] and r.items[0].outcome == "not_saved"
    assert open(w.env.facts).read() == "{not json"


def test_corrupt_privacy_rules_is_a_batch_error(fb):
    w = fb
    open(os.path.join(w.env.data_dir, "privacy_rules.json"), "w").write("[1]")
    r = w.fb.add_facts([it()], commit=False)
    assert not r.ok and r.errors and not os.path.exists(w.env.facts)


# ------------------------------------------------------------------ duplicates

def test_duplicates_in_one_batch_and_against_the_file_are_skipped_per_item(fb):
    w = fb
    assert w.af.append_fact(w.af.NewFact("Already here.", "coffee", "medium")).ok
    r = w.fb.add_facts([it("Already here."), it("New one."), it("  new   ONE. "), it("Already here.", subject="plans")], commit=False)
    assert [i.outcome for i in r.items] == ["duplicate", "saved", "duplicate", "saved"]
    assert r.ok and [x["statement"] for x in w.env.entries()] == ["Already here.", "New one.", "Already here."]
    assert r.total == 3


def test_rerunning_the_same_file_writes_and_commits_nothing(fb):
    w = fb
    d = make_repo(w)
    batch = [it("One."), it("Two.")]
    first = w.fb.add_facts(batch)
    assert first.ok and first.commit and commits(d) == 2
    before = digest(w.env.facts)
    again = w.fb.add_facts(batch)
    assert again.ok and [i.outcome for i in again.items] == ["duplicate"] * 2
    assert again.commit is None and commits(d) == 2 and digest(w.env.facts) == before


# ------------------------------------------------------------------ dry run

def test_dry_run_reports_visibility_and_rule_and_writes_nothing(fb):
    w = fb
    assert w.af.append_fact(w.af.NewFact("Exists.", "coffee", "low")).ok
    d = make_repo(w)
    open(os.path.join(d, "stray.txt"), "w").write("x")  # a dirty tree does not stop a dry run
    n = commits(d)
    before = digest(w.env.facts)
    r = w.fb.add_facts([it("Brand new."), it("Sees alice.", subject="plans"), it("Exists.")], dry_run=True)
    assert r.ok and r.dry_run
    assert [i.outcome for i in r.items] == ["would_save", "would_save", "duplicate"]
    assert [i.visibility for i in r.items] == ["normal", "private", "normal"]
    assert "alice" in r.items[1].rule and all(i.source_key is None for i in r.items)
    assert digest(w.env.facts) == before and commits(d) == n


def test_dry_run_still_reports_invalid_items(fb):
    r = fb.fb.add_facts([it(), it(subject="BAD")], dry_run=True)
    assert not r.ok and r.items[1].outcome == "invalid" and not os.path.exists(fb.env.facts)


# ------------------------------------------------------------------ size

def test_a_thousand_items_in_one_write_and_one_commit(fb):
    w = fb
    d = make_repo(w)
    t = time.time()
    r = w.fb.add_facts([it(f"Fact number {i}.") for i in range(1000)])
    assert r.ok and len(r.saved) == 1000 and len(w.env.entries()) == 1000 and commits(d) == 2
    assert time.time() - t < 60


# ------------------------------------------------------------------ git (#10 semantics)

def test_one_commit_containing_only_the_facts_file(fb):
    w = fb
    d = make_repo(w)
    r = w.fb.add_facts([it("One."), it("Two.", subject="plans")])
    assert r.ok and r.commit and commits(d) == 2 and not r.detached
    assert git(d, "show", "--name-only", "--format=%s", "HEAD").splitlines() == ["add-facts: 2 fact(s) (coffee, plans)", "", "general_facts.json"]
    assert git(d, "status", "--porcelain") == ""


def test_dirty_tree_refuses_before_writing_unless_allow_dirty(fb):
    w = fb
    d = make_repo(w)
    open(os.path.join(d, "stray.txt"), "w").write("x")
    r = w.fb.add_facts([it()])
    assert not r.ok and "uncommitted" in r.errors[0] and r.items[0].outcome == "not_saved" and not os.path.exists(w.env.facts)
    r = w.fb.add_facts([it()], allow_dirty=True)
    assert r.ok and r.commit
    assert git(d, "status", "--porcelain") == "?? stray.txt"  # the stray file was not swept into the commit


def test_non_git_dir_writes_without_committing_and_says_so(fb):
    r = fb.fb.add_facts([it()])
    assert r.ok and r.commit is None and any("not inside a git repository" in n for n in r.notes)


def test_commit_false_skips_git_even_on_a_dirty_tree(fb):
    w = fb
    d = make_repo(w)
    open(os.path.join(d, "stray.txt"), "w").write("x")
    n = commits(d)
    assert w.fb.add_facts([it()], commit=False).ok and commits(d) == n


def test_commit_failure_after_the_write_leaves_the_facts_and_reports_it(fb):
    w = fb
    d = make_repo(w)
    hook = os.path.join(d, ".git", "hooks", "pre-commit")
    open(hook, "w").write("#!/bin/sh\necho no >&2\nexit 1\n")
    os.chmod(hook, 0o755)
    r = w.fb.add_facts([it("Kept.")])
    assert not r.ok and r.commit is None and "NOT committed" in r.commit_error
    assert [i.outcome for i in r.items] == ["saved"] and w.env.entries()[0]["statement"] == "Kept."


# ------------------------------------------------------------------ concurrency

def test_concurrent_batches_and_single_adds_lose_nothing(fb):
    w = fb

    def batch(n):
        return w.fb.add_facts([it(f"B{n} item {j}.") for j in range(5)], commit=False).ok

    def single(n):
        return w.af.append_fact(w.af.NewFact(f"Single {n}.", "coffee", "low")).ok

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(batch, n) for n in range(8)] + [ex.submit(single, n) for n in range(8)]
        assert all(f.result() for f in futs)
    e = w.env.entries()
    assert len(e) == 8 * 5 + 8 and len({x["source_key"] for x in e}) == len(e)


def test_identical_concurrent_batches_write_each_fact_once(fb):
    w = fb
    batch = [it(f"Same {j}.") for j in range(10)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
        rs = list(ex.map(lambda _: w.fb.add_facts(batch, commit=False), range(4)))
    assert all(r.ok for r in rs) and len(w.env.entries()) == 10
    assert sum(len(r.saved) for r in rs) == 10


# ------------------------------------------------------------------ modes gate

def test_mode_gate_refuses_private_results_in_normal_mode_per_item(fb):
    w = fb
    s = w.fb.modes.Session("normal")
    r = w.fb.add_facts([it("Fine."), it("Mentions alice."), it("Also fine.")], session=s, commit=False)
    assert [i.outcome for i in r.items] == ["saved", "refused", "saved"] and not r.ok
    assert "database private" in r.items[1].errors[0]
    assert [x["statement"] for x in w.env.entries()] == ["Fine.", "Also fine."]


def test_mode_off_refuses_the_whole_call_and_private_mode_defaults_requests_private(fb):
    w = fb
    r = w.fb.add_facts([it()], session=w.fb.modes.Session("off"), commit=False)
    assert not r.ok and r.errors and not os.path.exists(w.env.facts)
    r = w.fb.add_facts([it()], session=w.fb.modes.Session("private"), commit=False)
    assert r.ok and r.items[0].visibility == "private" and not r.items[0].raised


# ------------------------------------------------------------------ add_fact helper + pinned behavior

def test_append_records_is_one_write_with_an_in_lock_reject_hook_and_append_record_is_unchanged(fb):
    w = fb
    p = os.path.join(w.env.data_dir, "x.json")
    assert w.af.append_record(p, {"n": 0}) == 1
    total, rej = w.af.append_records(p, [{"n": 1}, {"n": 1}, {"n": 2}], reject=lambda ex, r: "dup" if r in ex else None)
    assert total == 3 and rej == [(1, "dup")]
    before = digest(p)
    assert w.af.append_records(p, [{"n": 2}], reject=lambda ex, r: "dup") == (3, [(0, "dup")]) and digest(p) == before
    assert w.af.append_records(p, []) == (3, []) and digest(p) == before
    q = os.path.join(w.env.data_dir, "y.json")
    assert w.af.append_records(q, [{"a": 1}, {"a": 2}]) == (2, []) and json.load(open(q)) == [{"a": 1}, {"a": 2}]


def test_add_fact_still_resolves_privacy_the_same_way(fb):
    w = fb
    r = w.af.append_fact(w.af.NewFact("Hello Alice.", "coffee", "low", visibility="normal"))
    assert r.ok and r.entry["visibility"] == "private" and r.privacy.raised_above_request


# ------------------------------------------------------------------ callers must read the result

def test_no_caller_discards_the_result_of_add_facts():
    offenders = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in {".git", ".claude", ".venv", "__pycache__", "node_modules"}]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                    f = node.value.func
                    called = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                    if called == "add_facts":
                        offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    assert offenders == [], f"result of add_facts discarded at {offenders}"


def test_the_scan_catches_a_discarded_call():
    tree = ast.parse("facts_batch.add_facts(x)\ny = facts_batch.add_facts(x)\n")
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)]
    assert len(hits) == 1


def test_cli_checks_every_outcome_not_just_did_not_raise():
    src = open(os.path.join(REPO, "cli_facts_batch.py")).read()
    assert "result.commit_error" in src and "result.ok" in src


# ------------------------------------------------------------------ CLI

def run_cli(w, *args, stdin=None):
    return subprocess.run([sys.executable, os.path.join(REPO, "knowledge.py"), "add-facts", *args],
                          env=w.env.env, cwd=w.env.root, capture_output=True, text=True, input=stdin)


def write_json(w, data, name="in.json"):
    p = os.path.join(w.env.root, name)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return p


def test_cli_file_and_stdin_commit_once_and_print_a_summary(fb):
    w = fb
    d = make_repo(w)
    r = run_cli(w, write_json(w, [it("From file."), it("Hello alice.")]), "--session-id", "s9")
    assert r.returncode == 0, r.stderr
    assert "1. saved [normal]" in r.stdout and "2. saved [private]" in r.stdout and "alice" in r.stdout and "Committed" in r.stdout
    e = w.env.entries()
    assert [x["status"] for x in e] == ["active", "active"] and e[0]["captured_via"] == "register-facts" and e[0]["session_id"] == "s9"
    r = run_cli(w, "-", "--captured-via", "cli", "--status", "pending", stdin=json.dumps([it("From stdin.")]))
    assert r.returncode == 0, r.stderr
    assert w.env.entries()[2]["status"] == "pending" and w.env.entries()[2]["captured_via"] == "cli" and commits(d) == 3
    r = run_cli(w, stdin=json.dumps([it("Stdin without dash.")]))
    assert r.returncode == 0 and len(w.env.entries()) == 4


def test_cli_dry_run_prints_rule_and_writes_nothing(fb):
    w = fb
    r = run_cli(w, write_json(w, [it("Hello alice."), it("Plain.")]), "--dry-run")
    assert r.returncode == 0 and "would_save [private]" in r.stdout and "keyword: the statement contains the listed word 'alice'" in r.stdout
    assert "nothing written" in r.stdout and not os.path.exists(w.env.facts)


def test_cli_json_output(fb):
    w = fb
    r = run_cli(w, write_json(w, [it("One.")]), "--json", "--dry-run")
    data = json.loads(r.stdout)
    assert r.returncode == 0 and data["ok"] and data["dry_run"] and data["items"][0]["outcome"] == "would_save" and data["items"][0]["visibility"] == "normal"


def test_cli_error_exit_codes(fb):
    w = fb
    assert run_cli(w, os.path.join(w.env.root, "missing.json")).returncode == 1
    assert run_cli(w, stdin="not json").returncode == 1
    assert run_cli(w, stdin='{"a": 1}').returncode == 1
    r = run_cli(w, write_json(w, [it(), it(subject="BAD")]))
    assert r.returncode == 1 and "invalid" in r.stdout and not os.path.exists(w.env.facts)
    assert run_cli(w, write_json(w, [it()]), "--status", "bogus").returncode != 0
    d = make_repo(w)
    open(os.path.join(d, "stray.txt"), "w").write("x")
    r = run_cli(w, write_json(w, [it("Dirty.")]))
    assert r.returncode == 1 and "uncommitted" in r.stderr and not os.path.exists(w.env.facts)


def test_cli_commit_failure_exits_3_with_the_facts_written(fb):
    w = fb
    d = make_repo(w)
    hook = os.path.join(d, ".git", "hooks", "pre-commit")
    open(hook, "w").write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    r = run_cli(w, write_json(w, [it("Kept.")]))
    assert r.returncode == 3 and "NOT committed" in r.stderr and w.env.entries()[0]["statement"] == "Kept."


def test_cli_is_one_added_line_in_knowledge_py_and_registered_for_parity():
    import cli_parity
    assert cli_parity.ACTIONS["add_facts"] == ("add-facts",)
    src = open(os.path.join(REPO, "knowledge.py")).read()
    assert len([l for l in src.splitlines() if "cli_facts_batch" in l]) == 1


# ------------------------------------------------------------------ protocol text

def _doc():
    return open(os.path.join(REPO, "docs", "register-facts-protocol.md"), encoding="utf-8").read()


def _block():
    return _doc().split("<!-- instruction-block:start -->")[1].split("<!-- instruction-block:end -->")[0].strip()


def test_instruction_block_is_under_the_2048_character_cutoff_and_key_rule_first():
    block = _block()
    assert len(block) < 2048
    assert block.startswith("KEY RULE:") and "register facts" in block.splitlines()[0]


def test_instruction_block_carries_the_required_rules():
    b = _block().lower()
    for needle in ("numbered list", "drop 3", "make 2 private", "change 4 to", "once", "exactly as stated", "never write a date",
                   "the subject", "dry run", "coffee preference", "ask whether to save"):
        assert needle in b, needle
    d = _doc()
    assert "#27" in d and "pending" in d  # the pending-count note is a #27 item
