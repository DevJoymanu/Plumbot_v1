# bot/ — the conversation engine

Loaded whenever you touch a file under `bot/`, so it stays short. The detail is in `docs/current-state/`; the root CLAUDE.md table says which file covers what you are about to edit. Read it first.

The pipeline files at this level (`whatsapp_webhook.py`, `unified_classifier.py`, `controller.py`, `response_check.py`, `availability_ask.py`, `utils.py`) break in the same few ways every time:

- **Router order is the logic.** `_generate_and_schedule_reply` runs FAQ → pre-classifier → STEP 0 … STEP 4 and the first step to produce a reply wins. Moving, adding or gating a step changes what every later step sees. Read the Inbound pipeline section of `docs/current-state/pipeline.md` before touching it.
- **Every reply goes through the chain.** `finalise_outbound`, or `_finalised_for_send` when a path writes to the wire itself. A new send path that skips it skips the fee stripper, the free-visit stripper, the booking-claim stripper, speak-as-WE and the dash stripper all at once.
- **The transcript holds what was SENT.** Log through `replace_draft_assistant_turns`, never a bare append, and stamp the outbound WAMID (`attach_message_id` / `record_sent_media`).
- **A classifier `None` is not "not present".** When the unified call fails, every field comes back null at once. Any field the flow depends on needs a deterministic floor (`_area_from_reply`, `_keyword_availability_date`) that never consults the classifier.
- **The model picks the move; code writes anything the customer must trust**: slots, prices, booking state, the fee.
- A change to intent classification, a pricing gate or routing adds a TEST 0 case; a change that shows up in a CONVERSATION adds or extends a file in `scenarios/` (see `scenarios/CLAUDE.md`).
