"""Parity rule (#34): every UI/inbox/MCP action is also a CLI command.

(a) each registered action resolves to a real Typer command;
(b) each read command takes --json;
(c) optional future modules (mcp_server, inbox) are imported if present and must
    declare PARITY_ACTIONS, every name of which has a registry entry.
The negative tests prove each check can fail.
"""
import importlib
import importlib.util
import sys

import pytest
import typer.main

import cli_parity
import knowledge


@pytest.fixture(scope="module")
def group():
    return typer.main.get_command(knowledge.app)


def test_every_registered_action_resolves_to_a_real_command(group):
    assert cli_parity.actions_without_command(cli_parity.ACTIONS, group) == []


def test_every_cli_command_is_registered(group):
    assert set(group.commands) == {path[0] for path in cli_parity.ACTIONS.values()}


def test_registry_fails_for_a_stub_action_without_a_command(group):
    stub = {**cli_parity.ACTIONS, "stub_action": ("no-such-command",), "nested": ("search", "deeper")}
    assert cli_parity.actions_without_command(stub, group) == ["nested", "stub_action"]


READ_COMMANDS = ("search", "show", "subjects", "facts", "review-pending")


@pytest.mark.parametrize("name", READ_COMMANDS)
def test_read_commands_have_json_flag(group, name):
    assert "--json" in {o for p in group.commands[name].params for o in p.opts}


@pytest.mark.parametrize("name", ("approve", "reject"))
def test_review_write_commands_report_json_too(group, name):
    assert "--json" in {o for p in group.commands[name].params for o in p.opts}


@pytest.mark.parametrize("name", ("search", "facts", "subjects"))
def test_status_filtered_reads_offer_include_pending(group, name):
    assert "--include-pending" in {o for p in group.commands[name].params for o in p.opts}


def _check_layer(modname):
    mod = importlib.import_module(modname)
    assert hasattr(mod, "PARITY_ACTIONS"), f"{modname} must declare PARITY_ACTIONS (see cli_parity.py)"
    missing = cli_parity.declared_without_entry(mod.PARITY_ACTIONS, cli_parity.ACTIONS)
    assert missing == [], f"{modname} exposes actions with no CLI command registered: {missing}"


@pytest.mark.parametrize("modname", cli_parity.LAYER_MODULES)
def test_future_layers_declare_only_registered_actions(modname):
    if importlib.util.find_spec(modname) is None:
        pytest.skip(f"{modname} not built yet")
    _check_layer(modname)


def test_unregistered_layer_action_is_detected():
    assert cli_parity.declared_without_entry(["search_facts", "approve_fact"], cli_parity.ACTIONS) == ["approve_fact"]


def test_a_layer_module_with_an_unregistered_tool_fails(tmp_path, monkeypatch):
    """End to end: a stub mcp_server declaring a tool with no CLI command fails the layer check."""
    (tmp_path / "mcp_server.py").write_text("PARITY_ACTIONS = ['search_facts', 'stub_tool']\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    try:
        with pytest.raises(AssertionError, match="stub_tool"):
            _check_layer("mcp_server")
    finally:
        sys.modules.pop("mcp_server", None)


def test_a_layer_module_without_declaration_fails(tmp_path, monkeypatch):
    (tmp_path / "inbox.py").write_text("X = 1\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    importlib.invalidate_caches()
    try:
        with pytest.raises(AssertionError, match="PARITY_ACTIONS"):
            _check_layer("inbox")
    finally:
        sys.modules.pop("inbox", None)
