"""migrate-memory (#29): library (migrate_memory.py) and the Typer command (cli_migrate.py).

Synthetic memory files in temp dirs only; the data dir is a throwaway git repo; nothing here
can read ~/.claude or touch the real knowledge-private (KNOWLEDGE_PRIVATE_DIR is only used so the
modules import).
"""
import ast
import concurrent.futures
import json
import os
import re
import subprocess
import sys
from datetime import date

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

import add_fact  # noqa: E402
import migrate_memory as mm  # noqa: E402

GIT_ENV = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
TODAY = date(2026, 10, 3)


def git(cwd, *args):
    p = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, env={**os.environ, **GIT_ENV})
    assert p.returncode == 0, p.stderr
    return p.stdout.strip()


class W:
    """A temp private repo (data dir inside it) plus a temp projects root."""
    def __init__(self, root, git_repo=True):
        self.root = str(root)
        self.private = os.path.join(self.root, "knowledge-private")
        self.data_dir = os.path.join(self.private, "data")
        self.db_dir = os.path.join(self.root, "db")
        self.projects = os.path.join(self.root, "projects")
        for d in (self.data_dir, self.db_dir, self.projects):
            os.makedirs(d)
        with open(os.path.join(self.private, "local_paths.py"), "w") as f:
            f.write(f"KNOWLEDGE_DB_DIR = {self.db_dir!r}\nPRIVATE_DATA_DIR = {self.data_dir!r}\n")
            for n in ("BODYBUILDING_VAULT", "HEALTH_DIR", "CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON"):
                f.write(f"{n} = {os.path.join(self.root, 'unused')!r}\n")
        self.facts = os.path.join(self.data_dir, "general_facts.json")
        self.db = os.path.join(self.db_dir, "knowledge.db")
        self.env = {**os.environ, **GIT_ENV, "KNOWLEDGE_PRIVATE_DIR": self.private}
        if git_repo:
            git(self.private, "init", "-q")
            git(self.private, "config", "user.name", "T")
            git(self.private, "config", "user.email", "t@example.com")
            with open(os.path.join(self.data_dir, "other.json"), "w") as f:
                f.write("[]\n")
            git(self.private, "add", "-A")
            git(self.private, "commit", "-q", "-m", "init")

    def mem(self, project, name, body="Synthetic body.", type_="project", modified="2026-06-01T12:00:00.000Z",
            session="sess-1", nested=True, raw=None):
        d = os.path.join(self.projects, project, "memory")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, name)
        if raw is not None:
            data = raw if isinstance(raw, bytes) else raw.encode()
        else:
            meta = {"type": type_, "originSessionId": session, "modified": modified}
            lines = ["---", f"name: n-{name}", f"description: d-{name}"]
            body_meta = {k: v for k, v in meta.items() if v is not None}
            if nested:
                lines.append("metadata:\n  node_type: memory")
                lines += [f"  {k}: {v}" for k, v in body_meta.items()]
            else:
                lines += [f"{k}: {v}" for k, v in body_meta.items()]
            lines += ["---", body]
            data = "\n".join(lines).encode()
        with open(path, "wb") as f:
            f.write(data)
        return path

    def run(self, **kw):
        return mm.migrate(root=self.projects, data_path=self.facts, db_path=self.db, today=TODAY, **kw)

    def entries(self):
        if not os.path.exists(self.facts):
            return []
        with open(self.facts) as f:
            return json.load(f)

    def head(self):
        return git(self.private, "rev-parse", "HEAD")

    def status(self):
        return git(self.private, "status", "--porcelain", "--untracked-files=all")

    def cli(self, *args):
        return subprocess.run([sys.executable, os.path.join(REPO, "knowledge.py"), "migrate-memory",
                               "--memory-root", self.projects, *args],
                              env=self.env, cwd=self.root, capture_output=True, text=True)


@pytest.fixture
def w(tmp_path):
    return W(tmp_path)


def actions(report):
    return [r.action for r in report.files]


# ------------------------------------------------------------------ frontmatter parsing

def test_parse_nested_and_top_level_and_quotes_and_block_scalars():
    f, body = mm.parse_frontmatter(
        '---\nname: "Quoted: name"\ndescription: >\n  folded\n  text\nmetadata:\n  type: project\n  modified: 2026-01-02\n'
        'originSessionId: abc\n---\nBody here\n')
    assert f["name"] == "Quoted: name" and f["description"] == "folded text"
    assert mm._field(f, "type") == "project" and mm._field(f, "modified") == "2026-01-02"
    assert mm._field(f, "originSessionId") == "abc" and body == "Body here"


def test_parse_handles_crlf_and_bom():
    f, body = mm.parse_frontmatter("﻿---\r\ntype: user\r\n---\r\nhi\r\n")
    assert f["type"] == "user" and body == "hi"


@pytest.mark.parametrize("text", ["", "no frontmatter\n", "---\ntype: user\nnever closed\n"])
def test_parse_rejects_missing_or_unclosed_frontmatter(text):
    with pytest.raises(ValueError):
        mm.parse_frontmatter(text)


@pytest.mark.parametrize("value,expected", [
    ("2026-06-01T12:00:00.000Z", date(2026, 6, 1)),
    ("2026-06-01", date(2026, 6, 1)),
    ("2026-06-01 12:00:00", date(2026, 6, 1)),
    ("2026-06-01T23:30:00-05:00", date(2026, 6, 2)),      # converted to UTC first
    ("2026-06-01T12:00:00+00:00", date(2026, 6, 1)),
    ("yesterday", None), ("2026-13-45", None), ("", None), (None, None), ("   ", None),
])
def test_parse_modified(value, expected):
    assert mm.parse_modified(value) == expected


# ------------------------------------------------------------------ the happy path

def test_migrates_fact_types_with_full_provenance_and_one_commit(w):
    w.mem("proj-a", "a.md", body="First line.\nSecond line.", type_="project")
    w.mem("proj-a", "u.md", type_="user", nested=False)
    w.mem("proj-b", "r.md", type_="reference", modified=None, session=None)
    w.mem("proj-b", "f.md", type_="feedback")
    w.mem("proj-b", "MEMORY.md", body="- index")
    before = w.head()
    r = w.run()
    assert r.exit_code == 0, r
    assert r.totals()["added"] == 3 and r.totals()["skipped-feedback"] == 1 and r.index_files == 1
    es = w.entries()
    assert len(es) == 3
    by_subject = {e["subject"]: e for e in es}
    assert set(by_subject) == {"claude-memory-project", "claude-memory-user", "claude-memory-reference"}
    for e in es:
        assert re.fullmatch(add_fact.SUBJECT_RE.pattern, e["subject"])
        assert e["status"] == "pending" and e["visibility"] == "private" and e["is_personal"] is True
        assert e["trust_level"] == "unverified" and e["captured_via"] == "migrate-memory"
        assert re.fullmatch(r"mm-[0-9a-f]{16}", e["source_key"])
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["recheck_by"])
        assert mm.HASH_MARKER in e["notes"]
    p = by_subject["claude-memory-project"]
    assert p["statement"] == "First line. Second line." and p["session_id"] == "sess-1"
    assert p["source_quote"] == "First line.\nSecond line."
    assert p["recheck_by"] == "2026-11-28"                       # 2026-06-01 + 180 days
    assert "proj-a/a.md" in p["notes"]
    assert by_subject["claude-memory-reference"].get("session_id") is None
    assert by_subject["claude-memory-reference"]["recheck_by"] == "2027-04-01"   # today + 180
    assert any("no `modified`" in x for x in next(f.warnings for f in r.files if f.file == "r.md"))
    # one commit, only the facts file, tree clean
    assert w.head() != before and w.status() == ""
    assert r.commit and git(w.private, "show", "--name-only", "--format=", "HEAD") == "data/general_facts.json"
    assert git(w.private, "log", "--format=%s").splitlines()[0] == "migrate-memory: 3 pending facts"


def test_second_run_is_a_no_op(w):
    w.mem("p", "a.md")
    w.mem("p", "b.md", type_="user")
    w.run()
    raw, head = open(w.facts, "rb").read(), w.head()
    r = w.run()
    assert actions(r) == ["unchanged", "unchanged"] and r.exit_code == 0
    assert open(w.facts, "rb").read() == raw and w.head() == head and r.commit is None


def test_changed_file_is_reported_not_readded(w):
    path = w.mem("p", "a.md", body="old")
    w.run()
    w.mem("p", "a.md", body="new content")
    raw = open(w.facts, "rb").read()
    r = w.run()
    assert actions(r) == ["changed"] and "changed since migration" in r.files[0].detail
    assert r.exit_code == 0 and open(w.facts, "rb").read() == raw
    assert os.path.exists(path)


def test_dry_run_writes_nothing(w):
    w.mem("p", "a.md")
    w.mem("p", "f.md", type_="feedback")
    head = w.head()
    r = w.run(dry_run=True)
    assert actions(r) == ["would-add", "skipped-feedback"] and r.commit is None
    assert not os.path.exists(w.facts) and w.head() == head and w.status() == ""


def test_dry_run_counts_match_the_real_run(w):
    for i in range(3):
        w.mem("p", f"{i}.md")
    w.mem("p", "bad.md", raw="no frontmatter")
    dry = w.run(dry_run=True)
    real = w.run()
    assert dry.counts()["project"]["would-add"] == real.counts()["project"]["added"] == 3
    assert len(dry.problems) == len(real.problems) == 1


# ------------------------------------------------------------------ long bodies, big files

def test_long_body_is_cut_in_the_statement_but_kept_whole_in_notes(w):
    body = " ".join(f"word{i}" for i in range(400))
    w.mem("p", "long.md", body=body)
    r = w.run()
    e = w.entries()[0]
    assert len(e["statement"]) <= mm.STATEMENT_MAX and e["statement"].endswith(" [...]")
    assert body in e["notes"] and "statement is cut" in e["notes"]
    assert r.files[0].detail == "statement cut, full text in notes"


def test_short_body_is_not_duplicated_into_notes(w):
    w.mem("p", "s.md", body="short fact")
    w.run()
    assert "full text" not in w.entries()[0]["notes"]


def test_huge_file_is_refused_not_truncated(w):
    w.mem("p", "big.md", body="x" * (mm.MAX_FILE_BYTES + 10))
    w.mem("p", "ok.md")
    r = w.run()
    assert actions(r) == ["problem", "added"] and "limit" in r.files[0].detail and r.exit_code == 1
    assert len(w.entries()) == 1


def test_body_at_the_limit_boundary_and_over_statement_limit_with_no_spaces(w):
    w.mem("p", "nospace.md", body="y" * 2000)
    w.run()
    e = w.entries()[0]
    assert len(e["statement"]) <= mm.STATEMENT_MAX and "y" * 2000 in e["notes"]


# ------------------------------------------------------------------ odd files

def test_odd_files_are_reported_never_crash(w):
    w.mem("p", "nofm.md", raw="just text, no frontmatter\n")
    w.mem("p", "unclosed.md", raw="---\ntype: project\n")
    w.mem("p", "empty.md", raw="")
    w.mem("p", "notype.md", raw="---\nname: x\ndescription: y\n---\nbody\n")
    w.mem("p", "weird.md", type_="weirdtype")
    w.mem("p", "latin1.md", raw=b"---\ntype: project\n---\n\xff\xfe bad bytes")
    w.mem("p", "nul.md", raw=b"---\ntype: project\n---\nab\x00cd")
    w.mem("p", "emptybody.md", raw="---\ntype: project\n---\n")
    w.mem("p", "good.md")
    r = w.run()
    assert actions(r).count("problem") == 8 and actions(r).count("added") == 1
    assert r.exit_code == 1
    details = {f.file: f.detail for f in r.files}
    assert "UTF-8" in details["latin1.md"] and "NUL" in details["nul.md"]
    assert "no type" in details["notype.md"] and "weirdtype" in details["weird.md"]
    assert "empty body" in details["emptybody.md"]
    assert len(w.entries()) == 1


def test_empty_body_falls_back_to_description(w):
    w.mem("p", "d.md", raw="---\ndescription: only a description\ntype: user\n---\n")
    r = w.run()
    assert r.exit_code == 0 and w.entries()[0]["statement"] == "only a description"


def test_empty_projects_dir_and_missing_root(w):
    r = w.run()
    assert r.files == [] and r.exit_code == 0 and r.index_files == 0
    missing = mm.migrate(root=os.path.join(w.root, "nope"), data_path=w.facts, db_path=w.db)
    assert missing.refused and "not a directory" in missing.refused and missing.exit_code == 1


def test_symlinks_are_never_followed(w, tmp_path):
    outside = tmp_path / "outside.md"
    outside.write_text("---\ntype: project\n---\nsecret outside\n")
    w.mem("p", "real.md")
    os.symlink(outside, os.path.join(w.projects, "p", "memory", "link.md"))
    other = tmp_path / "otherproj"
    (other / "memory").mkdir(parents=True)
    (other / "memory" / "x.md").write_text("---\ntype: project\n---\nvia linked folder\n")
    os.symlink(other, os.path.join(w.projects, "linkedproj"))
    r = w.run()
    assert sorted(actions(r)) == ["added", "skipped-symlink", "skipped-symlink"] and r.exit_code == 0
    assert len(w.entries()) == 1 and "secret outside" not in open(w.facts).read()


def test_project_folder_without_memory_dir_is_ignored(w):
    os.makedirs(os.path.join(w.projects, "nomem"))
    w.mem("p", "a.md")
    assert actions(w.run()) == ["added"]


# ------------------------------------------------------------------ identity and collisions

def test_source_key_is_stable_and_depends_on_place_not_content():
    assert mm.source_key_for("p", "a.md") == mm.source_key_for("p", "a.md")
    assert mm.source_key_for("p", "a.md") != mm.source_key_for("p2", "a.md")
    assert mm.source_key_for("p", "a.md") != mm.source_key_for("p", "b.md")
    assert add_fact.SOURCE_KEY_RE.match(mm.source_key_for("p" * 500, "é.md"))


def test_colliding_derived_keys_second_file_is_a_problem(w, monkeypatch):
    w.mem("p", "a.md")
    w.mem("p", "b.md")
    monkeypatch.setattr(mm, "source_key_for", lambda project, name: "mm-collide")
    r = w.run()
    assert actions(r) == ["added", "problem"] and "collides" in r.files[1].detail
    assert len(w.entries()) == 1 and r.exit_code == 1


def test_existing_key_in_any_entry_file_counts_as_migrated(w):
    w.mem("p", "a.md")
    key = mm.source_key_for("p", "a.md")
    with open(os.path.join(w.data_dir, "facts_batch1.json"), "w") as f:
        json.dump([{"source_key": key, "subject": "x", "statement": "s", "notes": "no marker"}], f)
    git(w.private, "add", "-A")
    git(w.private, "commit", "-q", "-m", "seed")
    r = w.run()
    assert actions(r) == ["unchanged"] and w.entries() == []


# ------------------------------------------------------------------ privacy

def test_keyword_hit_stays_private_and_still_succeeds(w):
    with open(os.path.join(w.data_dir, "privacy_rules.json"), "w") as f:
        json.dump({"version": 1, "keywords": ["zorblax"]}, f)
    git(w.private, "add", "-A")
    git(w.private, "commit", "-q", "-m", "rules")
    w.mem("p", "a.md", body="Zorblax prefers tea.")
    r = w.run()
    assert r.exit_code == 0 and w.entries()[0]["visibility"] == "private"


def test_bad_privacy_rules_are_reported_per_file_and_nothing_is_committed(w):
    with open(os.path.join(w.data_dir, "privacy_rules.json"), "w") as f:
        f.write("{not json")
    git(w.private, "add", "-A")
    git(w.private, "commit", "-q", "-m", "rules")
    head = w.head()
    w.mem("p", "a.md")
    r = w.run()
    assert actions(r) == ["problem"] and r.exit_code == 1 and w.head() == head
    assert w.entries() == []


# ------------------------------------------------------------------ git and failure modes

def test_dirty_private_tree_refuses_and_writes_nothing(w):
    with open(os.path.join(w.data_dir, "other.json"), "w") as f:
        f.write('["dirty"]\n')
    w.mem("p", "a.md")
    r = w.run()
    assert r.refused and "uncommitted" in r.refused.lower() and r.exit_code == 1
    assert w.entries() == [] and actions(r) == ["problem"]


def test_dirty_tree_with_nothing_to_add_is_fine(w):
    w.mem("p", "a.md")
    w.run()
    with open(os.path.join(w.data_dir, "other.json"), "w") as f:
        f.write('["dirty"]\n')
    assert w.run().exit_code == 0


def test_allow_dirty_commits_only_the_facts_file(w):
    with open(os.path.join(w.data_dir, "other.json"), "w") as f:
        f.write('["dirty"]\n')
    w.mem("p", "a.md")
    r = w.run(allow_dirty=True)
    assert r.exit_code == 0
    assert git(w.private, "show", "--name-only", "--format=", "HEAD") == "data/general_facts.json"
    assert w.status() == "M data/other.json"


def test_data_dir_outside_git_writes_without_commit(tmp_path):
    w2 = W(tmp_path, git_repo=False)
    w2.mem("p", "a.md")
    r = w2.run()
    assert r.exit_code == 0 and r.not_in_git and r.commit is None and len(w2.entries()) == 1


def test_commit_failure_is_exit_3_and_facts_are_kept(w):
    hook = os.path.join(w.private, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    w.mem("p", "a.md")
    r = w.run()
    assert r.exit_code == 3 and r.commit_error and len(w.entries()) == 1


def test_interrupted_run_commits_what_was_written_and_rerun_finishes(w, monkeypatch):
    for n in ("a.md", "b.md", "c.md"):
        w.mem("p", n)
    real = add_fact.append_fact
    calls = []

    def flaky(fact, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real(fact, **kw)

    monkeypatch.setattr(add_fact, "append_fact", flaky)
    with pytest.raises(KeyboardInterrupt):
        w.run()
    monkeypatch.setattr(add_fact, "append_fact", real)
    assert len(w.entries()) == 1 and w.status() == ""      # first fact written and committed
    r = w.run()
    assert sorted(actions(r)) == ["added", "added", "unchanged"]
    keys = [e["source_key"] for e in w.entries()]
    assert len(keys) == 3 == len(set(keys))


def test_killed_run_leaving_a_dirty_tree_needs_allow_dirty_then_completes(w):
    w.mem("p", "a.md")
    w.mem("p", "b.md")
    w.run()
    # simulate a hard kill after writing one more fact: uncommitted facts file
    w.mem("p", "c.md")
    add_fact.append_fact(add_fact.NewFact(statement="x", subject="s", trust_level="low", no_decay=True, recheck_rationale="no decay"), data_path=w.facts, db_path=w.db)
    assert w.run().refused
    r = w.run(allow_dirty=True)
    assert actions(r).count("added") == 1 and len([e for e in w.entries() if e["source_key"].startswith("mm-")]) == 3


def test_concurrent_add_fact_and_two_migrations_lose_and_duplicate_nothing(w):
    for i in range(6):
        w.mem("p", f"{i}.md")

    def adder(i):
        res = add_fact.append_fact(add_fact.NewFact(statement=f"manual {i}", subject="manual", trust_level="low", no_decay=True, recheck_rationale="no decay"),
                                   data_path=w.facts, db_path=w.db)
        assert res.ok

    with concurrent.futures.ThreadPoolExecutor(6) as ex:
        futs = [ex.submit(adder, i) for i in range(4)]
        futs += [ex.submit(w.run, allow_dirty=True) for _ in range(2)]
        for f in futs:
            f.result()
    es = w.entries()
    mm_keys = [e["source_key"] for e in es if e["source_key"].startswith("mm-")]
    assert len(mm_keys) == 6 == len(set(mm_keys))
    assert sum(1 for e in es if e["subject"] == "manual") == 4


def test_unreadable_existing_facts_file_refuses(w):
    with open(w.facts, "w") as f:
        f.write("{broken")
    git(w.private, "add", "-A")
    git(w.private, "commit", "-q", "-m", "broken")
    w.mem("p", "a.md")
    r = w.run()
    assert r.refused and "cannot read" in r.refused and r.exit_code == 1


# ------------------------------------------------------------------ CLI

def test_cli_json_and_exit_codes(w):
    w.mem("p", "a.md")
    w.mem("p", "f.md", type_="feedback")
    r = w.cli("--dry-run", "--json")
    assert r.returncode == 0, r.stderr
    d = json.loads(r.stdout)
    assert d["dry_run"] is True and d["totals"]["would-add"] == 1 and d["by_type"]["feedback"] == {"skipped-feedback": 1}
    assert not os.path.exists(w.facts)
    r = w.cli()
    assert r.returncode == 0 and "Committed " in r.stdout and len(w.entries()) == 1
    r = w.cli("--json")
    assert json.loads(r.stdout)["totals"]["unchanged"] == 1 and r.returncode == 0
    assert len(w.entries()) == 1


def test_cli_exit_1_for_problem_files_and_text_lists_them(w):
    w.mem("p", "bad.md", raw="nothing")
    w.mem("p", "ok.md")
    r = w.cli("--dry-run")
    assert r.returncode == 1 and "problem" in r.stdout and "p/bad.md" in r.stdout and "would-add" in r.stdout


def test_cli_exit_3_on_commit_failure(w):
    hook = os.path.join(w.private, ".git", "hooks", "pre-commit")
    with open(hook, "w") as f:
        f.write("#!/bin/sh\nexit 1\n")
    os.chmod(hook, 0o755)
    w.mem("p", "a.md")
    r = w.cli()
    assert r.returncode == 3 and "NOT committed" in r.stderr


def test_cli_missing_root_is_exit_1(w):
    r = subprocess.run([sys.executable, os.path.join(REPO, "knowledge.py"), "migrate-memory", "--memory-root",
                        os.path.join(w.root, "absent"), "--dry-run"], env=w.env, capture_output=True, text=True)
    assert r.returncode == 1 and "not a directory" in r.stderr


# ------------------------------------------------------------------ callers must check .ok

def _py_files():
    skip = {".venv", ".git", ".claude", "tests", "__pycache__", "docs"}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


def _discarded_calls(tree, name):
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            f = node.value.func
            if (f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)) == name:
                hits.append(node.lineno)
    return hits


def test_no_caller_discards_an_append_fact_result():
    offenders = []
    for path in _py_files():
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        offenders += [f"{os.path.relpath(path, REPO)}:{n}" for n in _discarded_calls(tree, "append_fact")]
    assert not offenders, f"AddResult discarded (check .ok): {offenders}"


def test_migrate_memory_reads_ok_from_every_append_fact_result():
    tree = ast.parse(open(os.path.join(REPO, "migrate_memory.py"), encoding="utf-8").read())
    assigned = [n for n in ast.walk(tree) if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
                and getattr(n.value.func, "attr", None) == "append_fact"]
    assert assigned, "expected migrate_memory to call append_fact"
    names = {t.id for n in assigned for t in n.targets if isinstance(t, ast.Name)}
    reads_ok = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute) and n.attr == "ok"
                and isinstance(n.value, ast.Name) and n.value.id in names]
    assert len(reads_ok) >= len(assigned)


def test_discard_scan_detects_a_discard():
    tree = ast.parse("add_fact.append_fact(f)\nr = add_fact.append_fact(f)\n")
    assert _discarded_calls(tree, "append_fact") == [1]
