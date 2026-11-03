---
name: knowledge-recall
description: "Look in the user's personal knowledge base (the `knowledge` MCP server: search_facts, list_subjects) before answering anything it could already know: the user's own preferences, history, plans or past research, and perishable topics they have researched before (versions, prices, rules, health or product details). Cite what you find with its trust level; say plainly when nothing is found. Skip generic questions and anything the user asked you not to look up."
---

# Knowledge recall

The knowledge base holds facts the user (or an earlier session) recorded with a source quote, a trust level and a freshness date. Reading it first stops you from re-deriving, or contradicting, something already established.

## Requires

The `knowledge` MCP server (tools `search_facts`, `list_subjects`, `add_fact`). If those tools are not available in this conversation, say so once and carry on without them; do not pretend to have searched.

## When to look

Search before answering when the question touches:
- the user personally (their preferences, history, setup, plans, decisions);
- a topic they have likely researched before (ask `list_subjects` if unsure what exists);
- a perishable fact (a version, price, limit, rule) where an earlier recorded answer would save a lookup.

Do not search for generic knowledge questions, and do not search at all if the user said the knowledge base is off or private material should stay out of this conversation.

## How

1. `search_facts` with a few distinctive terms (full-text search: short keywords, not a sentence). Use `subject` to narrow once you know it.
2. If the first search is empty, try one synonym or a broader term, then stop. An empty result means "not recorded", not "false".
3. Answer using the facts. For each fact you rely on, say its trust level and, when the result carries one, its recheck date or status. Quote the statement faithfully; do not strengthen it.
4. A fact with `valid_to` set, or a status other than active, is history, not current truth; say so.
5. Facts that conflict with each other: show both and say which has the higher trust or newer date; do not silently pick one.

## Privacy

The server decides what you may see. Do not repeat private facts into places other than this conversation (tool calls to other services, files in a shared repo, public posts) unless the user asks.

## Next

If the answer needed fresh research and you found something worth keeping, offer `knowledge-record`; if a fact you relied on is past its recheck date, mention `knowledge-freshness`.
