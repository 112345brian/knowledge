"""The client registry: a client source is data the client directory registers, so adding one changes no base file."""
import os

import pytest

import build_rules
import client_bootstrap
from client_registry import ClientRegistry, ClientSource, Input, Step

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def own_source():
    return ClientSource(
        "my_feed", steps=(Step("client/my_feed.py", after="ingest/facts.py", order=5),
                          Step("client/my_feed_links.py", after="client/my_feed.py")),
        schema=("client/my_feed.sql",), inputs=(Input("MY_FEED_JSON", "my-feed-json", required=True),),
        report_tables=("my_feed_rows",), data_files=("my_feed_hints.json",))


def test_a_source_the_client_adds_gets_its_steps_schema_inputs_and_report_tables_without_a_base_change():
    registry = client_bootstrap.registry()
    registry.add(own_source())
    steps = registry.steps_for(("my_feed",))
    assert steps.index("client/my_feed.py") == steps.index("ingest/facts.py") + 1
    assert steps.index("client/my_feed_links.py") == steps.index("client/my_feed.py") + 1
    assert [s for s in steps if not s.startswith("client/")] == list(build_rules.CORE_PIPELINE)
    assert registry.schema_files(("my_feed",)) == ["schema.sql", "client/my_feed.sql"]
    assert registry.missing_inputs(("my_feed",), []) == [("my_feed", "MY_FEED_JSON")]
    assert registry.missing_inputs(("my_feed",), ["MY_FEED_JSON"]) == []
    assert "my_feed_rows" in registry.report_tables(["facts", "my_feed_rows"], build_rules.CORE_REPORT_TABLES)
    assert "my_feed_hints.json" in registry.data_files()
    assert "client/my_feed.py" not in registry.steps_for(())          # not enabled: not run


def test_the_shipped_sources_are_unchanged_by_a_client_addition():
    plain = client_bootstrap.registry()
    extended = client_bootstrap.registry()
    extended.add(own_source())
    for name in plain.names:
        assert extended.steps_for((name,)) == plain.steps_for((name,))


def test_bad_registrations_are_refused():
    registry = ClientRegistry(build_rules.CORE_PIPELINE)
    with pytest.raises(ValueError, match="not a pipeline step"):
        registry.add(ClientSource("x", steps=(Step("client/x.py", after="ingest/nope.py"),)))
    with pytest.raises(ValueError, match="core step"):
        registry.add(ClientSource("y", steps=(Step("ingest/facts.py", after=None),)))
    registry.add(ClientSource("a", steps=(Step("client/s.py", after="ingest/facts.py"),)))
    with pytest.raises(ValueError, match="registered twice"):
        registry.add(ClientSource("a"))
    with pytest.raises(ValueError, match="differently"):
        registry.add(ClientSource("b", steps=(Step("client/s.py", after="ingest/facts.py", order=9),)))


def test_without_a_client_directory_the_build_is_core_only(tmp_path):
    import local_buildfiles
    registry = client_bootstrap.registry(local_buildfiles, str(tmp_path))
    assert registry.names == () and registry.steps_for(()) == list(build_rules.CORE_PIPELINE)


def test_the_bootstrap_is_loaded_by_path_once_the_client_directory_has_one(tmp_path):
    import local_buildfiles
    (tmp_path / "client").mkdir()
    (tmp_path / "client" / "bootstrap.py").write_text(
        "from client_registry import ClientSource, Step\n"
        "def register(registry):\n"
        "    registry.add(ClientSource('mine', steps=(Step('client/mine.py', after='ingest/facts.py'),)))\n")
    registry = client_bootstrap.registry(local_buildfiles, str(tmp_path))
    assert registry.names == ("mine",) and "client/mine.py" in registry.steps_for(("mine",))


def test_no_base_module_names_a_shipped_client_source():
    """The base asks the registry; the names below live only in client/ (and in the tests and docs)."""
    names = ("concerts_csv", "rym_export_csv", "scrobbles_json", "CONCERTS_CSV", "RYM_EXPORT_CSV", "SCROBBLES_JSON",
             "MUSIC_SOURCES", "bodybuilding.db")
    offenders = []
    for folder in ("", "ingest"):
        for name in sorted(os.listdir(os.path.join(REPO, folder) if folder else REPO)):
            if name.endswith(".py"):
                text = open(os.path.join(REPO, folder, name), encoding="utf-8").read()
                offenders += [f"{folder + '/' if folder else ''}{name}: {n}" for n in names if n in text]
    assert offenders == []
