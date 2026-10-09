"""Issue #22: mode filter (off | normal | private) in the shared query layer.

Everything runs on a throwaway db built from the real schema.sql; nothing reads the real data.
Private texts in the fixture all contain a marker from PRIVATE_MARKERS; the leak checks assert
that none of them appears anywhere in a normal-mode answer.
"""
import ast
import inspect
import os
import re
import sqlite3
import threading

import pytest

import knowledge
import modes
import privacy
import revisions
from test_add_fact import REPO

T1 = "2026-10-01T08:00:00+00:00"
T2 = "2026-10-02T09:00:00+00:00"
T3 = "2026-10-03T10:00:00+00:00"

PRIVATE_MARKERS = ["zebrafinch", "quokka", "wombat", "narwhal", "okapi", "ocelot", "mongoose"]


def _fact(con, fid, subject, statement, vis="normal", status="active", key=None, trust="medium",
          notes=None):
    sid = con.execute("SELECT id FROM subjects WHERE name = ?", (subject,)).fetchone()[0]
    con.execute(
        """INSERT INTO facts (id, subject_id, statement, trust_level, status, visibility, source_key, notes, freshness)
           VALUES (?,?,?,?,?,?,?,?, 'unreviewed')""",
        (fid, sid, statement, trust, status, vis, key or f"k{fid}", notes))


def _rev(con, fid, n, when, statement, vis, status="active"):
    con.execute(
        """INSERT INTO fact_revisions (fact_id, source_key, revision, changed_at, changed_via, statement,
               trust_level, status, visibility) VALUES (?,?,?,?,?,?,?,?,?)""",
        (fid, f"k{fid}", n, when, "test", statement, "medium", status, vis))


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "k.db")
    con = sqlite3.connect(path)
    con.executescript(open(os.path.join(REPO, "schema.sql")).read())
    for name, parent, private in [("alpha", None, 0), ("gamma", None, 0), ("hush", None, 1),
                                  ("solo", None, 0), ("empty", None, 0), ("top", None, 0),
                                  ("child", "top", 0), ("hidden-parent", None, 1),
                                  ("under-hidden", "hidden-parent", 0)]:
        pid = con.execute("SELECT id FROM subjects WHERE name=?", (parent,)).fetchone()
        con.execute("INSERT INTO subjects (name, parent_id, private) VALUES (?,?,?)",
                    (name, pid[0] if pid else None, private))
    _fact(con, 1, "alpha", "Banana smoothie recipe works well.")
    _fact(con, 2, "alpha", "Pending banana idea.", status="pending")
    _fact(con, 3, "alpha", "Secret banana zebrafinch diagnosis.", vis="private", notes="zebrafinch notes")
    _fact(con, 4, "gamma", "Gamma public fact about banana.")
    _fact(con, 5, "gamma", "Old superseded banana fact.", status="superseded")
    _fact(con, 6, "hush", "Inconsistent normal fact on a private subject, wombat.", vis="normal")
    _fact(con, 7, "solo", "Only-private subject narwhal fact.", vis="private")
    _fact(con, 8, "alpha", "Demoted banana quokka fact.", vis="private")  # was normal, now private
    _rev(con, 8, 1, T1, "Demoted banana fact, original wording.", "normal")
    _rev(con, 8, 2, T3, "Demoted banana quokka fact.", "private")
    _fact(con, 9, "alpha", "Promoted banana fact, now public.", vis="normal")  # was private, now normal
    _rev(con, 9, 1, T1, "Promoted banana okapi draft.", "private")
    _rev(con, 9, 2, T3, "Promoted banana fact, now public.", "normal")
    _fact(con, 10, "child", "Child subject fact banana.")
    _fact(con, 11, "under-hidden", "Fact under a hidden parent banana.")
    _rev(con, 1, 1, T1, "Banana smoothie recipe works well.", "normal")
    con.execute("INSERT INTO sources (id, name, source_type) VALUES (1, 'A paper', 'primary')")
    con.execute("INSERT INTO fact_sources (fact_id, source_id, locator) VALUES (1, 1, 'p. 3')")
    con.execute("INSERT INTO claims (id, statement) VALUES (1, 'Claim citing ocelot facts')")
    con.execute("INSERT INTO claim_facts (claim_id, fact_id) VALUES (1, 3)")
    con.commit()
    con.close()
    ro = knowledge.connect(path)
    yield ro
    ro.close()


def S(mode):
    return modes.Session(mode)


def ids(rows):
    return sorted(r["id"] for r in rows)


def assert_no_marker(obj):
    blob = repr(obj).lower()
    for m in PRIVATE_MARKERS:
        assert m not in blob, f"{m!r} leaked: {blob[:300]}"


# ---------------------------------------------------------------- session / set_mode

def test_fresh_session_is_normal():
    assert modes.get_mode(modes.Session()) == "normal"


def test_set_mode_all_values_and_previous():
    s = modes.Session()
    assert modes.set_mode(s, "private") == {"mode": "private", "previous": "normal"}
    assert modes.set_mode(s, modes.Mode.off) == {"mode": "off", "previous": "private"}
    assert modes.get_mode(s) == "off"


@pytest.mark.parametrize("bad", ["", "Private", " private", "private\n", "public", None, 3, "off\x00", ["off"]])
def test_set_mode_rejects_bad_values_and_keeps_mode(bad):
    s = S("private")
    with pytest.raises(modes.InvalidMode):
        modes.set_mode(s, bad)
    assert modes.get_mode(s) == "private"


def test_set_mode_after_a_failure_still_works():
    s = modes.Session()
    with pytest.raises(modes.InvalidMode):
        modes.set_mode(s, "nope")
    assert modes.set_mode(s, "off")["mode"] == "off"
    with pytest.raises(modes.InvalidMode):
        modes.set_mode(s, "nope")
    assert modes.get_mode(s) == "off"
    assert modes.set_mode(s, "normal")["mode"] == "normal"


def test_constructor_rejects_bad_mode():
    with pytest.raises(modes.InvalidMode):
        modes.Session("pubic")


def test_sessions_do_not_share_mode():
    a, b = modes.Session(), modes.Session()
    modes.set_mode(a, "private")
    assert modes.get_mode(b) == "normal"


def test_sessions_do_not_share_mode_across_threads(db):
    sessions = [modes.Session() for _ in range(8)]
    results = {}

    def work(i, s):
        modes.set_mode(s, "private" if i % 2 else "off")
        results[i] = modes.get_mode(s)

    ts = [threading.Thread(target=work, args=(i, s)) for i, s in enumerate(sessions)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert results == {i: ("private" if i % 2 else "off") for i in range(8)}


def test_non_session_is_a_type_error(db):
    with pytest.raises(TypeError):
        modes.get_fact("private", db, 1)


def test_with_mode_stamps_the_mode():
    s = modes.Session()
    assert modes.with_mode(s, [1]) == {"mode": "normal", "data": [1]}
    modes.set_mode(s, "private")
    assert modes.with_mode(s, None)["mode"] == "private"


# ---------------------------------------------------------------- off

READS = [
    lambda s, c: modes.search_facts(s, c, "banana"),
    lambda s, c: modes.get_fact(s, c, 1),
    lambda s, c: modes.list_facts(s, c),
    lambda s, c: modes.list_subjects(s, c),
    lambda s, c: modes.get_history(s, c, 1),
    lambda s, c: modes.get_fact_as_of(s, c, 1, "2026-12-31"),
]


@pytest.mark.parametrize("read", READS)
def test_off_every_read_raises(db, read):
    with pytest.raises(modes.ModeOff) as e:
        read(S("off"), db)
    assert "database normal" in str(e.value)


def test_off_then_reads_then_back_on(db):
    s = modes.Session()
    modes.set_mode(s, "off")
    with pytest.raises(modes.ModeOff):
        modes.list_facts(s, db)
    modes.set_mode(s, "normal")
    assert modes.list_facts(s, db)


def test_off_refuses_writes():
    res = privacy.resolve_visibility("alpha", "x", "normal", privacy.Rules())
    with pytest.raises(modes.ModeOff):
        modes.check_write(S("off"), res)
    with pytest.raises(modes.ModeOff):
        modes.prepare_write(S("off"), "alpha", "x", privacy.Rules())


def test_every_public_session_function_is_gated_when_off(db):
    """A new public read added to modes.py without a gate fails here."""
    skip = {"get_mode", "set_mode", "with_mode"}
    for name in modes.__all__:
        fn = getattr(modes, name)
        if not inspect.isfunction(fn) or name in skip:
            continue
        params = list(inspect.signature(fn).parameters)
        assert params[0] == "session", f"{name} must take the session first"
        args = {"session": S("off"), "con": db, "terms": "banana", "ref": 1, "as_of": "2026-12-31",
                "resolution": privacy.resolve_visibility("alpha", "x", "normal", privacy.Rules()),
                "subject": "alpha", "statement": "x", "rules": privacy.Rules()}
        with pytest.raises(modes.ModeOff):
            fn(**{p: args[p] for p in params if p in args and not (
                inspect.signature(fn).parameters[p].default is not inspect._empty)})


# ---------------------------------------------------------------- compatibility with knowledge.py

def _strip_gate(rows):
    return rows


def test_compat_get_fact_matches_knowledge(db):
    # Private mode returns the full row, exactly knowledge.get_fact. Normal mode returns the same row
    # minus NORMAL_HIDDEN_FIELDS (the columns normal_db.py also leaves out).
    for fid in (1, 4, 9, 10):
        full = knowledge.get_fact(db, fid)
        assert modes.get_fact(S("private"), db, fid) == full
        trimmed = {k: v for k, v in full.items() if k not in modes.NORMAL_HIDDEN_FIELDS}
        assert modes.get_fact(S("normal"), db, fid) == trimmed
    assert modes.get_fact(S("private"), db, 1)["sources"] == [{"name": "A paper", "locator": "p. 3"}]


def test_normal_mode_get_fact_hides_notes_provenance_and_the_vault_path(db):
    path = db.execute("PRAGMA database_list").fetchone()[2]
    w = sqlite3.connect(path)  # the fixture's own connection is read-only
    w.execute("""UPDATE facts SET notes = 'a private aside', source_quote = 'my own words', session_id = 's-1',
                 captured_via = 'mcp', is_personal = 1 WHERE id = 1""")
    w.commit()
    w.close()
    normal = modes.get_fact(S("normal"), db, 1)
    assert normal is not None and normal["statement"]
    for hidden in modes.NORMAL_HIDDEN_FIELDS:
        assert hidden not in normal, hidden
    assert "a private aside" not in repr(normal) and "my own words" not in repr(normal)
    private = modes.get_fact(S("private"), db, 1)
    assert private["notes"] == "a private aside" and private["session_id"] == "s-1"
    assert set(modes.NORMAL_HIDDEN_FIELDS) <= set(private) | {"origin_path"}


def _all_statuses(fn, db, *a, **kw):
    """knowledge.* now hides non-active facts by default (#6); private mode shows every status, so
    the reference is the union over each explicit status."""
    rows = {}
    for st in ("active", "pending", "superseded", "retracted"):
        rows.update({r["id"]: r for r in fn(db, *a, status=st, **kw)})
    return rows


def test_compat_list_search_subjects_row_shapes(db):
    got = {r["id"]: r for r in modes.list_facts(S("normal"), db)}
    ref = {r["id"]: r for r in knowledge.list_facts(db)}
    for fid, row in got.items():
        assert row == ref[fid]
    s_got = {r["id"]: r for r in modes.search_facts(S("normal"), db, "banana")}
    s_ref = {r["id"]: r for r in knowledge.search_facts(db, "banana")}
    for fid, row in s_got.items():
        assert row == s_ref[fid]
    assert set(s_got) == {1, 4, 9, 10, 11}
    priv = {r["id"]: r for r in modes.list_facts(S("private"), db, limit=1000)}
    assert priv == _all_statuses(knowledge.list_facts, db, limit=1000)
    priv_s = {r["id"]: r for r in modes.search_facts(S("private"), db, "banana")}
    assert priv_s == _all_statuses(knowledge.search_facts, db, "banana")
    # Subjects: same rows; private mode counts every status, knowledge.list_subjects only active ones.
    p_sub, k_sub = modes.list_subjects(S("private"), db), knowledge.list_subjects(db)
    names = lambda rows: [{k: v for k, v in r.items() if k in ("name", "domain", "parent")} for r in rows]
    assert names(p_sub) == names(k_sub)
    assert all(p["n_facts"] >= k["n_facts"] for p, k in zip(p_sub, k_sub))


def test_compat_history_and_as_of_match_revisions_for_visible_fact(db):
    assert modes.get_history(S("normal"), db, 1) == revisions.get_history(db, 1)
    assert modes.get_fact_as_of(S("normal"), db, 1, "2026-12-31") == revisions.get_fact_as_of(db, 1, "2026-12-31")
    assert modes.get_history(S("private"), db, 8) == revisions.get_history(db, 8)
    assert modes.get_fact_as_of(S("private"), db, 8, T1) == revisions.get_fact_as_of(db, 8, T1)


# ---------------------------------------------------------------- normal mode: what is visible

def test_normal_lists_only_active_normal_facts_on_non_private_subjects(db):
    assert ids(modes.list_facts(S("normal"), db)) == [1, 4, 9, 10, 11]


def test_normal_include_pending(db):
    assert ids(modes.list_facts(S("normal"), db, include_pending=True)) == [1, 2, 4, 9, 10, 11]
    assert ids(modes.search_facts(S("normal"), db, "banana", include_pending=True)) == [1, 2, 4, 9, 10, 11]
    assert modes.get_fact(S("normal"), db, 2) is None
    assert modes.get_fact(S("normal"), db, 2, include_pending=True)["id"] == 2


def test_normal_status_filter_only_narrows(db):
    assert modes.list_facts(S("normal"), db, status="superseded") == []
    assert modes.list_facts(S("normal"), db, status="pending") == []
    assert ids(modes.list_facts(S("normal"), db, status="active")) == [1, 4, 9, 10, 11]


def test_private_sees_everything(db):
    assert ids(modes.list_facts(S("private"), db)) == list(range(1, 12))
    assert modes.get_fact(S("private"), db, 3)["id"] == 3


def test_direct_id_of_private_fact_is_indistinguishable_from_missing(db):
    s = S("normal")
    for private_id in (3, 6, 7, 8, 2, 5):
        assert modes.get_fact(s, db, private_id) is None
        assert modes.get_history(s, db, private_id) == []
        assert modes.get_fact_as_of(s, db, private_id, "2027-01-01") is None
    assert modes.get_fact(s, db, 99999) is None
    assert modes.get_history(s, db, 99999) == []
    assert modes.get_fact_as_of(s, db, 99999, "2027-01-01") is None


def test_source_key_lookup_of_private_fact_is_not_found(db):
    s = S("normal")
    assert modes.get_fact(s, db, "k3") is None
    assert modes.get_history(s, db, "k8") == []
    assert modes.get_fact(s, db, "k1")["id"] == 1
    assert modes.get_fact(s, db, "nope") is None


def test_ref_type_errors(db):
    for bad in (True, 1.5, None, b"k1"):
        with pytest.raises(TypeError):
            modes.get_fact(S("normal"), db, bad)


def test_fts_matching_private_text_returns_nothing(db):
    s = S("normal")
    for term in ("zebrafinch", "quokka", "narwhal", "wombat", "zebrafinch notes"):
        assert modes.search_facts(s, db, term) == []
    assert ids(modes.search_facts(S("private"), db, "zebrafinch")) == [3]


def test_fts_limit_applies_after_the_filter(db):
    # 'banana' matches private facts too; limit=1 must still return a visible one.
    for _ in range(3):
        rows = modes.search_facts(S("normal"), db, "banana", limit=1)
        assert len(rows) == 1 and rows[0]["id"] in {1, 4, 9, 10, 11}


def test_subjects_normal_hides_private_and_counts_visible_only(db):
    rows = {r["name"]: r for r in modes.list_subjects(S("normal"), db)}
    assert "hush" not in rows          # private-tagged subject
    assert "solo" not in rows          # only private facts
    assert "empty" not in rows         # nothing visible
    assert "hidden-parent" not in rows
    assert rows["alpha"]["n_facts"] == 2   # facts 1 and 9 (not 2, 3, 8)
    assert rows["gamma"]["n_facts"] == 1   # not the superseded one
    assert rows["child"]["parent"] == "top" and "top" in rows
    assert rows["under-hidden"]["parent"] is None  # hidden parent's name must not leak
    assert_no_marker(rows)
    assert "hush" not in repr(rows) and "solo" not in repr(rows) and "hidden-parent" not in repr(rows)


def test_subjects_private_shows_all_with_full_counts(db):
    rows = {r["name"]: r for r in modes.list_subjects(S("private"), db)}
    assert rows["alpha"]["n_facts"] == 5 and rows["solo"]["n_facts"] == 1 and rows["empty"]["n_facts"] == 0


def test_subject_filter_on_hidden_subject_looks_like_unknown(db):
    assert modes.list_facts(S("normal"), db, subject="hush") == []
    assert modes.list_facts(S("normal"), db, subject="no-such") == []
    assert modes.search_facts(S("normal"), db, "wombat", subject="hush") == []


def test_history_drops_private_revisions_and_hides_demoted_fact(db):
    s = S("normal")
    assert modes.get_history(s, db, 8) == []   # now private: nothing, even its normal revision 1
    hist = modes.get_history(s, db, 9)         # now normal, revision 1 was private
    assert [r["revision"] for r in hist] == [2]
    assert_no_marker(hist)
    assert [r["revision"] for r in modes.get_history(S("private"), db, 9)] == [1, 2]


def test_as_of_before_a_demotion_does_not_reveal_the_old_normal_revision(db):
    s = S("normal")
    for when in (T1, T2, T3, "2027-01-01"):
        assert modes.get_fact_as_of(s, db, 8, when) is None
    assert modes.get_fact_as_of(S("private"), db, 8, T1)["statement"].startswith("Demoted banana fact")


def test_as_of_when_private_revision_was_in_force_is_none_not_the_old_one(db):
    s = S("normal")
    assert modes.get_fact_as_of(s, db, 9, T2) is None          # rev 1 (private) was in force
    assert modes.get_fact_as_of(s, db, 9, T3)["revision"] == 2
    assert modes.get_fact_as_of(S("private"), db, 9, T2)["revision"] == 1


def test_as_of_bad_date_is_value_error(db):
    with pytest.raises(ValueError):
        modes.get_fact_as_of(S("normal"), db, 1, "yesterday")


def test_claims_and_other_tables_are_not_reachable_through_results(db):
    assert modes.search_facts(S("private"), db, "ocelot") == []    # claim text is not searchable here
    fact = modes.get_fact(S("private"), db, 3)
    assert not any(k.startswith("claim") for k in fact)
    assert not any("ocelot" in repr(v) for v in fact.values())
    assert_no_marker(modes.get_fact(S("normal"), db, 1))


def test_no_private_marker_in_any_normal_answer(db):
    s = S("normal")
    out = [modes.list_facts(s, db, limit=1000, include_pending=True),
           modes.list_subjects(s, db),
           modes.search_facts(s, db, "banana", include_pending=True, limit=1000),
           [modes.get_fact(s, db, i, include_pending=True) for i in range(1, 15)],
           [modes.get_history(s, db, i, include_pending=True) for i in range(1, 15)],
           [modes.get_fact_as_of(s, db, i, "2027-01-01", include_pending=True) for i in range(1, 15)]]
    assert_no_marker(out)


# ---------------------------------------------------------------- query text edge cases

@pytest.mark.parametrize("q", ["", "   ", "\t\n", '"unbalanced', "banana AND", "(", "NEAR(", "*", '""', "a\x00b"])
def test_bad_search_text_is_a_clean_error(db, q):
    for mode in ("normal", "private"):
        with pytest.raises(modes.InvalidQuery) as e:
            modes.search_facts(S(mode), db, q)
        assert "sqlite" not in str(e.value).lower() and "fts5" not in str(e.value).lower()


def test_non_string_search_text(db):
    for bad in (None, 5, b"banana"):
        with pytest.raises(modes.InvalidQuery):
            modes.search_facts(S("normal"), db, bad)


@pytest.mark.parametrize("q", ["banana'; DROP TABLE facts; --", "' OR '1'='1", "banana\" OR visibility:private",
                               "visibility:private", "notes:zebrafinch", "statement:zebrafinch"])
def test_injection_looking_text_never_leaks_and_never_modifies(db, q):
    try:
        rows = modes.search_facts(S("normal"), db, q)
    except modes.InvalidQuery:
        rows = []
    assert_no_marker(rows)
    assert db.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 11


def test_injection_in_filter_arguments_is_parameterized(db):
    assert modes.list_facts(S("normal"), db, subject="x' OR 1=1 --") == []
    assert modes.list_facts(S("normal"), db, trust="medium' OR '1'='1") == []
    assert modes.get_fact(S("normal"), db, "k3' OR '1'='1") is None


@pytest.mark.parametrize("limit", [0, -1, 1001, True, "5", None, 2.5])
def test_bad_limits_rejected(db, limit):
    with pytest.raises(ValueError):
        modes.list_facts(S("normal"), db, limit=limit)
    with pytest.raises(ValueError):
        modes.search_facts(S("normal"), db, "banana", limit=limit)


def test_read_only_connection_is_not_written_through_modes(db):
    with pytest.raises(sqlite3.OperationalError):
        db.execute("DELETE FROM facts")


# ---------------------------------------------------------------- writes

RULES = privacy.Rules(subject_tags={"hush": "private", "alpha": "normal"}, keywords=("zebrafinch",))


def test_normal_mode_refuses_a_resolved_private_write_with_instruction():
    res = privacy.resolve_visibility("hush", "anything", "normal", RULES)
    with pytest.raises(modes.WriteRefused) as e:
        modes.check_write(S("normal"), res)
    assert "database private" in str(e.value)


def test_normal_mode_refuses_when_keyword_raises_the_model_requested_normal():
    res = privacy.resolve_visibility("alpha", "zebrafinch results", "normal", RULES)
    assert res.visibility == "private"
    with pytest.raises(modes.WriteRefused):
        modes.check_write(S("normal"), res)


def test_normal_mode_accepts_resolved_normal():
    res = privacy.resolve_visibility("alpha", "plain text", "normal", RULES)
    assert modes.check_write(S("normal"), res) == "normal"


def test_private_mode_accepts_either_and_returns_the_resolved_value():
    r_priv = privacy.resolve_visibility("hush", "x", "normal", RULES)
    r_norm = privacy.resolve_visibility("alpha", "plain", "normal", RULES)
    assert modes.check_write(S("private"), r_priv) == "private"
    assert modes.check_write(S("private"), r_norm) == "normal"


def test_private_mode_write_with_no_visibility_never_becomes_normal():
    """The #14 spike regression: the tool defaulted visibility and a sensitive fact was saved normal."""
    res = modes.prepare_write(S("private"), "alpha", "perfectly bland sentence", RULES)
    assert res.visibility == "private"
    assert isinstance(res, privacy.Resolution)
    # and when the rules are silent/empty, still private
    assert modes.prepare_write(S("private"), "alpha", "bland", privacy.Rules()).visibility == "private"
    # an explicit normal request in private mode still goes through the resolver
    assert modes.prepare_write(S("private"), "alpha", "bland", RULES, requested="normal").visibility == "normal"
    assert modes.prepare_write(S("private"), "alpha", "zebrafinch", RULES, requested="normal").visibility == "private"


def test_prepare_write_normal_mode_defaults_to_resolver_input_normal_and_can_refuse():
    assert modes.prepare_write(S("normal"), "alpha", "bland", RULES).visibility == "normal"
    with pytest.raises(modes.WriteRefused):
        modes.prepare_write(S("normal"), "hush", "bland", RULES)


def test_prepare_write_rejects_garbage_requested():
    with pytest.raises(ValueError):
        modes.prepare_write(S("private"), "alpha", "x", RULES, requested="public")


@pytest.mark.parametrize("bad", ["normal", "private", None, {"visibility": "normal"}])
def test_check_write_refuses_a_bare_visibility_value(bad):
    with pytest.raises(TypeError):
        modes.check_write(S("private"), bad)


def test_mode_changed_between_resolve_and_check_is_rechecked():
    s = S("private")
    res = privacy.resolve_visibility("hush", "x", "private", RULES)
    assert modes.check_write(s, res) == "private"
    modes.set_mode(s, "normal")
    with pytest.raises(modes.WriteRefused):
        modes.check_write(s, res)


def test_second_attempt_after_refusal_works_once_mode_is_private():
    s = modes.Session()
    with pytest.raises(modes.WriteRefused):
        modes.prepare_write(s, "hush", "x", RULES)
    modes.set_mode(s, "private")
    assert modes.prepare_write(s, "hush", "x", RULES).visibility == "private"


def test_a_visibility_written_by_a_normal_session_matches_what_a_normal_session_can_read(db):
    """End to end: the value check_write returns is exactly what the filter keys on."""
    for who, expect_visible in (("alpha", True), ("hush", False)):
        res = privacy.resolve_visibility(who, "bland", "normal", RULES)
        assert (res.visibility == "normal") is expect_visible


# ---------------------------------------------------------------- callers can't discard / bypass

def _py_files():
    skip = {".venv", ".git", ".claude", "tests", "__pycache__", "docs"}
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f.endswith(".py"):
                yield os.path.join(root, f)


# Modules allowed to read facts directly: the query layers and the build pipeline (which creates
# them). Anything else that wants fact rows must go through modes.py.
FACT_READERS_ALLOWED = {
    "modes.py", "knowledge.py", "normal_db.py", "revisions.py", "privacy.py", "claims_audit.py", "review.py",
    # CLI-only audit over the full local db (like claims_audit); never serves tool results
    "fixity.py",
    # build/ingest pipeline: writes the tables, never serves tool results
    "04_ingest_facts.py", "05_seed_claims.py", "11_seed_general_facts.py", "12_apply_fact_revisions.py",
    "add_fact.py", "backfill_source_keys.py", "build.py",
    # build-time checker: scans the full db for private markers, never serves tool results
    "leak_test.py",
}
FACT_TABLE_RE = re.compile(
    r"\b(?:FROM|JOIN|INTO|UPDATE)\s+(?:facts|fact_revisions|claim_facts)\b"
    r"|\bfacts_fts\b|\bfact_revisions\b|\bv_facts\b|\bv_fact_tags\b|\bv_claims_with_stale_premises\b",
    re.IGNORECASE)


def test_no_module_outside_the_allowlist_reads_fact_tables():
    offenders = []
    for path in _py_files():
        name = os.path.basename(path)
        if name in FACT_READERS_ALLOWED:
            continue
        if FACT_TABLE_RE.search(open(path, encoding="utf-8", errors="replace").read()):
            offenders.append(os.path.relpath(path, REPO))
    assert not offenders, ("these read facts/fact_revisions directly and would bypass the mode "
                           f"filter; use modes.py: {offenders}")


def test_the_scan_would_catch_a_bypass(tmp_path):
    assert FACT_TABLE_RE.search("con.execute('SELECT * FROM facts')")
    assert FACT_TABLE_RE.search("SELECT 1 FROM x JOIN facts f ON 1")
    assert FACT_TABLE_RE.search("SELECT * FROM facts_fts WHERE facts_fts MATCH ?")
    assert not FACT_TABLE_RE.search("def list_facts(): 'the facts about subjects'")


def test_modes_itself_never_writes_sql():
    src = open(os.path.join(REPO, "modes.py"), encoding="utf-8").read()
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|REPLACE|PRAGMA|ATTACH)\b", src.split('"""', 2)[2])


GATED = {"check_write", "prepare_write"}


def test_no_caller_discards_a_write_decision():
    """check_write / prepare_write raise on refusal, but their return value is the visibility to
    store; a bare expression statement means the caller ignored it."""
    offenders = []
    for path in _py_files():
        tree = ast.parse(open(path, encoding="utf-8", errors="replace").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                f = node.value.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", None)
                if name in GATED:
                    offenders.append(f"{os.path.relpath(path, REPO)}:{node.lineno}")
    assert not offenders, f"result of check_write/prepare_write discarded: {offenders}"


def test_discard_scan_detects_a_discard():
    tree = ast.parse("modes.check_write(s, r)\nx = modes.check_write(s, r)\n")
    hits = [n for n in ast.walk(tree) if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)
            and n.value.func.attr in GATED]
    assert len(hits) == 1
