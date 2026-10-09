# Competency questions

> **DRAFT: not authoritative until you review it.** Drafted by an agent from the README, issues #5 and #12 and the
> shape of the data (#49). Edit, delete and add questions freely, then comment "signed off" on issue #49. Until then
> treat the list as a proposal, not a specification.

A competency question is a concrete question the database must be able to answer. Each one below says how it is
answered **today** (a CLI command or SQL), and `tests/test_competency.py` runs every answerable one against a
fixture database built from the real `schema.sql`, so a schema change that breaks a question fails a test. A
question we cannot answer yet says what is missing and points at the issue that would fix it.

Format (the test parses it): `- **CQ-NN** (group) question? Answer: ... Test: test_name` for an answerable
question, or `- **CQ-NN** (group) question? Cannot answer yet: needs ... See: #issue` (or `See: proposed, not yet filed`).

## Provenance and trust

- **CQ-01** (provenance) Which facts cite this source, and where in it (locator, quote)? Answer: `fact_sources` joined to `facts`; `knowledge.py show ID` lists a fact's sources. Test: test_cq_01_facts_citing_a_source
- **CQ-02** (trust) How far should I trust this fact, and why? Answer: `knowledge.py show ID` (trust level, trust_rationale). Test: test_cq_02_trust_and_its_reason
- **CQ-03** (provenance) Where did this fact come from: who or what captured it, in which session, with which of the user's words? Answer: `facts.captured_via`, `session_id`, `captured_at`, `source_quote` (`show ID`). Test: test_cq_03_capture_provenance
- **CQ-04** (provenance) Has the file this fact was extracted from changed since? Answer: `knowledge.py audit-sources`. Test: test_cq_04_source_file_changed_since_extraction
- **CQ-05** (trust) Which facts cite a source that was retracted, doubted or replaced, and by what? Answer: `knowledge.py audit-source-status`. Test: test_cq_05_facts_on_retracted_or_replaced_sources
- **CQ-06** (provenance) What are this source's identifiers, and which other source claims the same one? Answer: `knowledge.py source ids CITEKEY`; `search --identifier`. Test: test_cq_06_source_identifiers_and_duplicates
- **CQ-07** (provenance) Which facts have neither a cited source nor a quoted statement of where they came from? Answer: SQL over `facts` / `fact_sources` (the "unsupported" facts). Test: test_cq_07_unsupported_facts

## Freshness and time

- **CQ-08** (freshness) Which facts are past their recheck-by date? Answer: SQL on `facts.recheck_by` (ISO dates only; free-text rechecks such as "next panel" are listed separately by `audit-claims` for cited facts). Test: test_cq_08_facts_due_for_recheck
- **CQ-09** (freshness) Which facts say they never decay, and why? Answer: `facts.freshness = 'no-decay'` with `recheck_rationale`. Test: test_cq_09_facts_that_never_decay
- **CQ-10** (time) Which facts were true on a given date? Answer: `knowledge.py facts --valid-at YYYY-MM-DD` (valid time, not `--as-of`). Test: test_cq_10_facts_true_on_a_date
- **CQ-11** (time) What did I believe about this fact on a given date? Answer: `knowledge.py show ID --as-of DATE`; `history ID`. Test: test_cq_11_belief_as_of_a_date
- **CQ-12** (time) How has this fact changed, by whom and why? Answer: `knowledge.py history ID`. Test: test_cq_12_history_of_a_fact

## Premise audit

- **CQ-13** (premises) Which claims rest on a premise that is superseded, retracted or overdue? Answer: `knowledge.py audit-claims`. Test: test_cq_13_claims_with_stale_premises
- **CQ-14** (premises) For a claim, which facts are its grounds, its backing and its rebuttals? Answer: `claim_facts.role`; a stale rebuttal is informational. Test: test_cq_14_roles_in_a_claims_argument

## Privacy tiers and modes

- **CQ-15** (privacy) Why is this fact private, and which rule says so? Answer: `knowledge.py privacy check`; `privacy.resolve_visibility`. Test: test_cq_15_why_a_fact_is_private
- **CQ-16** (privacy) What would a client see in normal mode, and is anything private reachable? Answer: `modes.list_facts` / `search_facts` with a normal-mode session; `knowledge-normal.db` plus `leak_test.py`. Test: test_cq_16_what_normal_mode_shows

## Capture history

- **CQ-17** (capture) What is waiting for review, and what was captured in a given session? Answer: `knowledge.py facts --status pending`; `facts.session_id`. Test: test_cq_17_pending_and_per_session_captures
- **CQ-18** (capture) How many facts came in by each route (CLI, register facts, migrated memory)? Answer: `GROUP BY facts.captured_via`. Test: test_cq_18_capture_routes

## Entities and subjects

- **CQ-19** (entities) Which facts mention this person or thing, under any of its names? Answer: `knowledge.py facts --entity NAME`. Test: test_cq_19_facts_about_an_entity
- **CQ-20** (subjects) What subjects sit under a topic, what do they mean, and which are deprecated? Answer: `knowledge.py subject list` / `show`. Test: test_cq_20_subject_hierarchy_and_meaning
- **CQ-21** (applicability) Which facts apply to a given population or condition? Answer: `knowledge.py search "postmenopausal"` (`applies_to` is in the full-text index). Test: test_cq_21_facts_for_a_population
- **CQ-22** (kinds) Which decisions, preferences or plans do I have on record? Answer: `knowledge.py facts --kind decision`. Test: test_cq_22_facts_of_a_kind

## Build and data

- **CQ-23** (build) Which inputs and code produced this database? Answer: `knowledge.py build-info`. Test: test_cq_23_which_inputs_produced_this_db
- **CQ-24** (music) Which artists have I seen live most often? Answer: SQL over `concert_attendances` / `artists`. Test: test_cq_24_most_seen_artists
- **CQ-25** (health) What is the latest value of a metric, from which source? Answer: SQL over `measurements` / `metrics` / `sources`. Test: test_cq_25_latest_measurement

## Cannot answer yet

- **CQ-26** (consistency) Which facts contradict each other? Cannot answer yet: needs a way to record that two facts conflict (a relation between facts, or a `disputed` pair), and nothing detects it. See: proposed, not yet filed
- **CQ-27** (retrieval) Give a remote client the facts for a question together with the stale-premise warnings it must repeat? Cannot answer yet: needs the MCP server and its response shape. See: #2, #3, #5
- **CQ-28** (freshness) Which facts are overdue by a policy that depends on their kind (a measurement decays faster than a definition)? Cannot answer yet: needs per-kind default freshness, which #39 deliberately left out. See: proposed, not yet filed
