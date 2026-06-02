---
description: Execute the Plumbot response-framework fix plan surfaced from production WhatsApp transcripts, in priority order, with regression safety
argument-hint: [task] — p0 | p1 | p2 | all (default: all). Or a single task id e.g. null-date
allowed-tools: Read, Grep, Glob, Edit, Bash
---

This is a **goal**, not a conversation. Work through it autonomously in the order below. Pause only at the checkpoints defined here. Requested scope: **$ARGUMENTS** (empty → `all`).

## The goal

Fix the response-framework defects observed in real Plumbot WhatsApp conversations, without regressing what already works. Plumbot is the WhatsApp sales/booking bot for Homebase Plumbers (Harare), on the **Meta WhatsApp Business API**, with a DeepSeek classifier → dispatcher architecture and a Hormozi qualification flow (Service → Project Description → Area → Availability).

## Definition of done

- Every task in scope has a code change **and** a matching assertion in the existing golden test set.
- The golden/regression suite passes (the pure-string `must_include`/`must_exclude` checker — do not introduce a new harness).
- None of the protected behaviours below have regressed.

## Hard constraints — do not break these

- The `<customer_input>` delimiting and JSON-schema validation stay intact. Never loosen injection defenses to smooth a flow.
- **Preserve what works:** Shona / code-switched comprehension, the price clarity, the completed booking happy-path, and the human-handoff escape hatch (referring to Tinashe on +263774819901). These are confirmed-good in the transcripts; protect them with assertions before touching nearby code.

## Method for every task

Locate the real code (Grep/Glob) → state the file/line and the fix as a concrete before/after diff → for any behaviour-changing fix, get confirmation → apply → add the golden assertion drawn from the cited conversation → run the checker → report the pass/fail delta. If a flow changed end-to-end, hand it to the `plumbot-conversation-flow-tester` subagent rather than re-checking by hand.

---

## P0 — broken in front of customers (pause after each)

**null-date** — The delay-nudge template renders a missing date as the literal word "None" to customers ("is it okay if we reach out to you on None?", "Should we put None in the diary?"). Evidence: conv 421. The booking step immediately prior computed a real date ("Monday 15 June"), so the nudge is reading a different/empty field than the delay-handler set. Find the mismatch; the nudge must read the same stored follow-up date, and must not send if that field is empty.

**scheduler-state-guard** — The follow-up scheduler fires into states where it should stay silent. Evidence: conv 378 (four auto-follow-ups asking for suburb *after* a delay-reactivation date and confirmed email were already set) and conv 411 (an auto-follow-up fired immediately after a clean human handoff). Same class as the prior `pending_upload` over-firing fix. Add a state guard so `parked`, `handed-off`, `awaiting-human`, and `already-confirmed` suppress follow-ups.

**webhook-dedup** — Inbound messages appear processed twice. Evidence: conv 369 (every customer line duplicated). Confirm the webhook dedup is active and, critically, that duplicates are not double-counting toward lead score.

## P1 — costing conversions (batch; one confirmation for the group)

**delay-intent-split** — One "delay / out-of-town" intent is absorbing several distinct situations and replying to all of them with the travel-assuming "Roughly when do you think you'll be back in town?". Evidence: conv 427 (customer corrects it: "We are not out of town but we go to work"), plus 415, 421, 378. Split into distinct intents — *busy/at-work*, *needs to arrange access*, *genuinely travelling*, *soft brush-off* — each with its own response. Highest-leverage classifier change.

**answer-direct-questions-first** — The qualification flow overrides plain customer questions. Evidence: conv 427 (tub measurements ignored to ask tub type), conv 369 ("who am I speaking to?" and the visiting plumber's name asked repeatedly, never answered), culminating in conv 411 ("answer my question direct"). Add a dispatcher rule: if the customer asks a direct question, answer it first, *then* advance the stage.


## P2 — polish

**adaptive-pricing** — The canned price block fires identically even when the customer asked specifically about built-in tubs or measurements (conv 427). Make the priced response track the actual question.

**input-format-validation** — An email typed when the bot asked for a *name* just re-asked the name question (conv 410). Catch obvious format mismatches.

---

## Closing checkpoint

Report: tasks completed, the diff summary per task, golden assertions added, suite pass/fail delta, and any protected-behaviour check results. List anything you deferred and why.