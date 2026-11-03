---
name: knowledge-freshness
description: "Check whether facts in the user's personal knowledge base (the `knowledge` MCP server) are past their recheck date, and offer to re-verify them. Use when the user asks what is stale or due for review, or when a fact you relied on from knowledge-recall is past its recheck_by or about to be. Re-verification is proposed to the user, never silently rewritten."
---

# Knowledge freshness

Perishable facts carry a `recheck_by` date. Past that date the fact is a lead, not an answer.

## Requires

The `knowledge` MCP server, and search results that carry `recheck_by`. If results lack it, say so and stop; do not guess dates.

## Steps

1. Get today's date from the conversation or system context. Never assume one.
2. `search_facts` (by subject, or by the topic the user cares about) and compare each fact's `recheck_by` with today. Group the results: overdue, due within about two weeks, fine, and no-decay.
3. Present overdue and soon-due facts: statement, subject, recheck date, trust.
4. For the ones the user wants re-checked, research each from the original source where possible (the recorded source quote and locator say where it came from), then:
   - still true: tell the user, and offer to record a new fact with a later `recheck_by` through `knowledge-record`;
   - changed: show old and new side by side; offer to record the new claim. Do not edit or delete the old one; a person supersedes facts with the CLI (`knowledge.py`).
5. If the user wants every stale fact across the whole base, give them the CLI: `knowledge.py audit-claims` lists claims whose premises are past `recheck_by`.

## Do not

- Do not treat an overdue fact as false, only as unchecked.
- Do not re-verify dozens at once; do the ones the user picks.
