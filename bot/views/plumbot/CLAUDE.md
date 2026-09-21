# bot/views/plumbot/ — the Plumbot mixins (what the bot says, and when)

**Load the `plumbot-sales-flow` skill before editing anything here.** Then read, for the mixin you are changing:

- `response_mixin.py`: `docs/current-state/sales-and-pricing.md` and `docs/current-state/conversation-rules.md`
- `extraction_mixin.py`, `availability_mixin.py`, `booking_mixin.py`, `reschedule_mixin.py`: `docs/current-state/booking-and-scheduling.md` and `docs/current-state/conversation-rules.md`
- `plan_upload_mixin.py`: `docs/current-state/post-visit-and-plan-path.md`

What goes wrong here most:

- **A sentence the customer reads lives in `bot/copy_catalog.py`, not in the mixin.** `response_mixin.py` holds most of the copy in the system, and the same sentence written in two places is how "five OTHER copies" drifted. Look it up in the catalog first; add it there if it is new.
- **The first ask of an early-flow question is the exact script** (`_get_first_pass_question`, `retry_count == 0`); only a re-ask is paraphrased, and a call site that reaches the script without reading `retry_count` re-sends it word for word.
- **Resolvers are singular**: `_job_is_known`, `_get_two_visit_slots` / `_visit_slot_labels`, `_availability_ask`, `lead_has_no_time_preference`, `resolve_flexible_slot`, `_reschedule_slot`, `_area_from_reply`. Call the one that exists; a second copy of the question will disagree with the first.
- **Prices come from the tenant** (`TenantConfig`, the tenant's own price rows and quote lines). A figure nothing backs is never guessed and never borrowed from Homebase.
- **Never re-pitch the visit to a lead who has committed**, and never claim a booking the row does not have.
- Shona: new copy supports it or says plainly that it does not. The first-pass question bank is still English-only.
