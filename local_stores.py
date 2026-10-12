"""Adapter for `step_stores_port.StepStores`: the privacy, entity, subject, revision, claims and source stores, by name."""
import claims_store
import entity_repository_store
import fact_ingest_store
import fact_tables_store
import privacy_store
import revisions_store
import source_repository_store
import subject_repository_store


def load_privacy_rules(path):
    return privacy_store.load_rules(path)


def apply_privacy_rules(con, rules):
    return privacy_store.apply_rules_to_db(con, rules)


def entity_repository(con):
    return entity_repository_store.SqliteEntityRepository(con)


def subject_repository(con):
    return subject_repository_store.SqliteSubjects(con)


def insert_claim(cur, claim):
    return claims_store.insert_claim(cur, claim)


def link_claim_premise(cur, claim_id, premise):
    return claims_store.link_claim_premise(cur, claim_id, premise)
def source_repository(con):
    return source_repository_store.SqliteSourceRepository(con)
def fact_repository(con):
    return fact_ingest_store.SqliteFactRepository(con)
def revision_writer(con):
    return fact_ingest_store.SqliteFactRevisions(con)
def unit_of_work(con):
    return fact_ingest_store.SqliteUnitOfWork(con)
def read_log(path, base=None):
    return revisions_store.read_log(path, base)
def load_rules(path):
    return privacy_store.load_rules(path)
def fact_revision_table(con):
    return fact_tables_store.SqliteFactRevisionTable(con)
def fact_privacy_table(con):
    return fact_tables_store.SqliteFactPrivacyTable(con)
