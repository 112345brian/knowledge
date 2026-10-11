"""Adapter for `step_stores_port.StepStores`: the privacy, entity, subject and revision stores, by name."""
import entities_store
import fact_tables_store
import privacy_store
import revisions_store
import subjects_store


def load_privacy_rules(path):
    return privacy_store.load_rules(path)


def apply_privacy_rules(con, rules):
    return privacy_store.apply_rules_to_db(con, rules)


def read_entities(path):
    return entities_store.read_file(path)


def link_entities(con, entities):
    return entities_store.link_facts(con, entities)


def read_subjects(path):
    return subjects_store.read_file(path)


def apply_revisions(con, path):
    return revisions_store.apply_revisions(con, path)


def insert_revision_row(cur, fact_id, revision):
    return revisions_store.insert_revision_row(cur, fact_id, revision)


def read_log(path, base=None):
    return revisions_store.read_log(path, base)


def load_rules(path):
    return privacy_store.load_rules(path)


def fact_revision_table(con):
    return fact_tables_store.SqliteFactRevisionTable(con)


def fact_privacy_table(con):
    return fact_tables_store.SqliteFactPrivacyTable(con)


def entity_link_table(con):
    return fact_tables_store.SqliteEntityLinkTable(con)
