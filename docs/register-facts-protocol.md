# The `register facts` review protocol (#32)

Review happens inside the chat, in every client, with no hooks and no reliance on Claude capturing
anything unprompted. The user says `register facts`; Claude lists what it would save, waits, applies
the user's edits, then writes once with a single `add_facts` call (library `facts_batch.add_facts`,
CLI `knowledge.py add-facts`, MCP tool `add_facts` from #2).

## Flow

1. The user says `register facts` (or accepts Claude's offer at the end of a long conversation).
2. Claude scans the conversation and renders a numbered list. Each line has the statement in the
   user's own words (dates exactly as stated), the subject Claude chose, and the resulting
   visibility with the rule behind it (from `knowledge.py add-facts --dry-run`, or the MCP tool's
   dry-run result).
3. The user replies in plain language: `yes`, `drop 3`, `make 2 private`, `change 4 to ...`.
   Claude applies the edits and may show the list again; it does not write yet.
4. Claude calls `add_facts` once with the final list. Facts reviewed this way are written `active`
   (a human reviewed them) with provenance (#24). `status: pending` is only for callers that did
   not show the user the list.

## Rules the protocol depends on

- **`applies_to` is copied, never inferred.** When the user or a cited source states who or what a
  fact holds for ("in adult men", "for type 2 diabetes"), put those words in `applies_to`. If nothing
  was stated, leave it out (unknown); do not write "general" unless the source says the fact is general.
- **Dates stay as stated.** "Next week" and "March 14" are stored as those words. The tool never
  resolves a date, and Claude must not either (the #14 spike saw it invent `2026-10-09`).
- **Claude picks the subject and the kind.** The kind (#39: observation, measurement, decision,
  preference, plan, definition, inference, rule, lesson, or `unclassified` when unsure) is
  self-reported guidance, shown on each line so the user can change it. Trust level, visibility and
  everything else are not Claude's decision. `make 2 private` is the only way a fact is made more private by hand; the stored
  visibility is always the most restrictive of the request, the subject tag and the keyword list
  (#31), and a new subject is private until it exists (fail closed).
- **Never ask permission mid-conversation.** Nothing is saved until the user has seen the numbered
  list and answered. Before that, do not interrupt the conversation to ask whether to save a fact.
- **Be concrete about what to list.** Durable facts the user stated about themselves, their plans or
  their world. Do not list questions, hypotheticals, or things Claude said. A coffee preference is a
  fact like any other; whether it is private is the rules' decision, not a reason to leave it out.
- **One call.** The batch is all-or-nothing for validation: one invalid item and nothing is written,
  so fix the list and resend it whole. Privacy raises, duplicates and mode refusals are reported per
  item and do not stop the others.

## What the result tells Claude to say

Per item: `saved` (with the stored visibility and the rule that set it), `duplicate` (already
there, nothing written), `refused` (e.g. the fact resolves private while the database is in normal
mode: say "database private" and register it again), `invalid` (with the reason), or `not_saved`
(valid, but another item was invalid). Report them back as a short numbered list; never claim a fact
was saved unless its outcome is `saved`. A `commit_error` means the facts are written but not
committed.

## Not part of this issue

Mentioning the number of pending facts at the start of a session is a #27 item (server
instructions), not part of `add_facts`. The 2,048-character budget for the server instructions as a
whole is also #27's; the compact block below is kept well under it (about 1,300 characters) so that
#27 can fit its other rules around it.

## Compact instruction block

The text between the markers is what #27 embeds. `tests/test_facts_batch.py` checks that it stays
under 2,048 characters and starts with the key rule.

<!-- instruction-block:start -->
KEY RULE: save facts only through `register facts`. Never save, and never ask whether to save, in the middle of a conversation.

When the user says "register facts" (or accepts your offer at the end of a long chat):
1. Scan the conversation for durable facts the user stated about themselves, their plans or their world. Skip questions, hypotheticals and anything you said.
2. Show a numbered list. Each line: the statement in the user's own words, the subject (lowercase-kebab), its applies_to (only when the user or a source stated a population or condition; never infer one), its kind (observation, measurement, decision, preference, plan, definition, inference, rule or lesson; unsure means unclassified), its freshness (a recheck_by date: ask the user when, never invent one; or, only if it truly never changes like a birthdate, no_decay true plus a recheck_rationale saying why), and the visibility with the rule, taken from a dry run. Do not guess the visibility.
3. Wait. The user replies "yes", "drop 3", "make 2 private" or "change 4 to ...". Apply it and re-list if anything changed. Do not write yet.
4. Call add_facts ONCE with the final list. Every item needs a recheck_by, or no_decay true with a recheck_rationale, and a kind. Do not set visibility or trust unless the user asked ("make 2 private" sets visibility private).

Keep dates and numbers exactly as stated ("next week" stays "next week"; never write a date you worked out). A coffee preference is an ordinary fact: list it. The tool decides privacy, not you.

Afterwards say what each item's result was (saved, duplicate, refused, invalid); never claim a save that was not "saved". If a fact was refused as private, tell the user to say "database private" and try again.
<!-- instruction-block:end -->
