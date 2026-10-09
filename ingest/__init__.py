"""The ingest pipeline: the ETL steps that build knowledge.db (run in the order of build_rules.STEPS),
their shared helpers, and the pure rules they apply. Driving adapters (batch) plus their domain rules;
build.py at the repo root runs the steps. Run a standalone tool as `python3 -m ingest.<name>`."""
