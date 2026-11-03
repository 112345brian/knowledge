"""Every module says which ring of the onion it is in and what its public functions do.

The import graph already forbids a wrong dependency (tach, import-linter); this makes the architecture legible where
the code is read. A module's docstring must name its tach layer as ``tach layer `<layer>` `` (the layer is the one in
tach.toml, so a docstring cannot claim a different ring than the one enforced), and every public class and function in
it, and every public method, needs a docstring saying what it does.

`NOT_YET_DESCRIBED` is a shrinking allowlist of modules written before this rule: a new module may not join it, and a
module that is brought up to the rule must leave it (the second test fails until it does).
"""
import ast
import os
import re
import tomllib

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NOT_YET_DESCRIBED = frozenset(
    {
    "acquisition_rules",
    "acquisition_store",
    "add_fact",
    "add_fact_port",
    "add_fact_service",
    "add_fact_store",
    "build",
    "build_composition",
    "build_files_port",
    "build_info_store",
    "build_port",
    "build_records",
    "build_repositories_port",
    "build_rules",
    "build_steps_port",
    "building",
    "claims_audit",
    "claims_rules",
    "claims_store",
    "cli_add_fact",
    "cli_audits",
    "cli_build",
    "cli_common",
    "cli_docs",
    "cli_entities",
    "cli_facts",
    "cli_facts_batch",
    "cli_inbox",
    "cli_lifecycle",
    "cli_migrate",
    "cli_parity",
    "cli_privacy",
    "cli_review",
    "cli_sources",
    "cli_subjects",
    "client_bootstrap",
    "client_registry",
    "clock",
    "clock_port",
    "container",
    "datafiles_port",
    "docs",
    "docs_files_port",
    "docs_rules",
    "docs_service",
    "docs_store",
    "entities",
    "entities_service",
    "entities_store",
    "entity_files_port",
    "entity_link_rules",
    "entity_link_service",
    "entity_migration_port",
    "entity_migration_rules",
    "entity_migration_store",
    "entity_queries_port",
    "entity_queries_store",
    "entity_repository_store",
    "entity_rules",
    "fact_entry",
    "fact_ingest",
    "fact_ingest_decisions",
    "fact_ingest_port",
    "fact_ingest_service",
    "fact_ingest_store",
    "fact_queries",
    "fact_reads_port",
    "fact_reads_store",
    "fact_rules",
    "fact_state",
    "facts_batch",
    "facts_batch_rules",
    "facts_batch_service",
    "facts_file_port",
    "fixity_rules",
    "fixity_store",
    "identifiers",
    "identity_rules",
    "identity_service",
    "ids",
    "ids_port",
    "inbox",
    "ingest.apply_fact_revisions",
    "ingest.backfill_dates",
    "ingest.backfill_extracted_hashes",
    "ingest.backfill_rules",
    "ingest.backfill_source_keys",
    "ingest.fact_ingest_rules",
    "ingest.facts",
    "ingest.link_entities",
    "ingest.link_source_relations",
    "ingest.literature_sources",
    "ingest.seed_general_facts",
    "ingest.seed_sources",
    "ingest.seed_subject_hierarchy",
    "ingest.shared",
    "ingest.source_ingest_rules",
    "ingest.source_readers",
    "inputs_port",
    "knowledge",
    "leak_rules",
    "leak_scan_store",
    "leak_test",
    "lifecycle",
    "lifecycle_reads_port",
    "lifecycle_rules",
    "lifecycle_service",
    "lifecycle_store",
    "local_buildfiles",
    "local_datafiles",
    "local_inputs",
    "local_stores",
    "locks",
    "mcp_port",
    "mcp_rules",
    "mcp_server",
    "memory_files_port",
    "migrate_memory",
    "migrate_memory_rules",
    "migrate_memory_service",
    "migrate_memory_store",
    "modes",
    "modes_store",
    "new_fact",
    "normal_db",
    "normal_db_port",
    "normal_db_service",
    "normal_db_store",
    "normal_rules",
    "pipeline_context",
    "privacy",
    "privacy_floor_service",
    "privacy_rules_port",
    "privacy_store",
    "private_edit_store",
    "private_git_port",
    "queries",
    "queries_service",
    "review",
    "review_reads_port",
    "review_rules",
    "review_service",
    "review_store",
    "reviews_port",
    "revision_apply_rules",
    "revision_apply_service",
    "revisions",
    "revisions_port",
    "revisions_store",
    "rules_edit",
    "rules_edit_service",
    "source_ingest_service",
    "source_record",
    "source_repository_store",
    "source_status",
    "source_status_store",
    "sources_port",
    "sql_runner_port",
    "sql_runner_store",
    "step_stores_port",
    "subject_files_port",
    "subject_hierarchy_service",
    "subject_repository_store",
    "subject_rules",
    "subjects",
    "subjects_service",
    "subjects_store",
    "textmatch",
    "timestamps",
    "validtime",
}
)


def _layers():
    with open(os.path.join(REPO, "tach.toml"), "rb") as f:
        return {m["path"]: m["layer"] for m in tomllib.load(f)["modules"] if m.get("layer")}


def _source(module):
    return os.path.join(REPO, module.replace(".", os.sep) + ".py")


def _public_defs(tree):
    """The public classes and functions of a module, and the public methods of its classes (not nested helpers)."""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            yield node
            if isinstance(node, ast.ClassDef):
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)) and not member.name.startswith("_"):
                        yield member


def problems(module, layer):
    """What is missing from `module`'s documentation, as short sentences; empty when it conforms."""
    tree = ast.parse(open(_source(module), encoding="utf-8").read())
    doc = ast.get_docstring(tree) or ""
    found = re.findall(r"tach layer `(\w+)`", doc)
    out = []
    if not found:
        out.append(f"the module docstring does not say `tach layer `{layer}``")
    elif layer not in found:
        out.append(f"the module docstring says tach layer `{found[0]}`, but tach.toml puts it in `{layer}`")
    out += [f"{node.name} has no docstring" for node in _public_defs(tree) if not ast.get_docstring(node)]
    return out


@pytest.mark.parametrize("module", sorted(m for m in _layers() if m not in NOT_YET_DESCRIBED))
def test_module_names_its_ring_and_describes_its_public_functions(module):
    assert problems(module, _layers()[module]) == []


def test_the_allowlist_only_shrinks():
    layers = _layers()
    unknown = sorted(NOT_YET_DESCRIBED - set(layers))
    assert unknown == [], f"not tach modules any more, remove from NOT_YET_DESCRIBED: {unknown}"
    done = sorted(m for m in NOT_YET_DESCRIBED if not problems(m, layers[m]))
    assert done == [], f"these now conform, remove them from NOT_YET_DESCRIBED: {done}"
