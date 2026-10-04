"""Cross-feature check written at merge time: the provenance/visibility flags (#19, #20, #24) and the
auto-commit (#10) were built by separate agents and tested separately; this runs them together."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_private_git import GitEnv  # noqa: E402


def test_mcp_style_capture_is_written_with_provenance_visibility_date_and_committed(tmp_path):
    env = GitEnv(tmp_path)
    r = env.add("I decided to use SQLite.", "knowledge-project", "medium",
                "--visibility", "normal", "--captured-via", "mcp", "--session-id", "sess-1",
                "--source-quote", "I've decided to use SQLite")
    assert r.returncode == 0, r.stderr
    (entry,) = json.load(open(env.facts))
    assert entry["visibility"] == "normal"
    assert entry["captured_via"] == "mcp" and entry["session_id"] == "sess-1"
    assert entry["source_quote"] == "I've decided to use SQLite"
    assert "captured_at" in entry and "date_added" in entry
    assert env.status() == ""                       # committed, tree clean
    assert env.log()[0] == "add-fact: knowledge-project (medium)"


def test_mcp_capture_without_session_id_is_refused_and_commits_nothing(tmp_path):
    env = GitEnv(tmp_path)
    head = env.head()
    r = env.add("X.", "x", "low", "--captured-via", "mcp", "--source-quote", "q")
    assert r.returncode == 1
    assert not os.path.exists(env.facts)
    assert env.head() == head and env.status() == ""


def test_default_visibility_is_private_even_through_the_committing_path(tmp_path):
    env = GitEnv(tmp_path)
    assert env.add("Plain CLI fact.", "x", "low").returncode == 0
    assert json.load(open(env.facts))[0]["visibility"] == "private"
