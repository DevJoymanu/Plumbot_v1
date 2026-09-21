# bot/management/commands/ — the crons

Read before editing:

- `send_followups.py` and the nudge loops: `docs/current-state/followups-and-cron.md`
- `send_post_visit_followups`, `send_plan_quote_followups`, `send_visit_checkins`: `docs/current-state/post-visit-and-plan-path.md`
- `process_inbound_emails.py` and anything that sends mail: `docs/current-state/email.md`

What goes wrong here most:

- **The follow-up scripts are the owner's own April 2026 copy.** Do not "fix" them, including the scarcity lines. The `owner script` cases in TEST 0 pin them word for word. Only the MODEL's rewrite is held to them (`fit_to_template`, and the fence in `bot/copy_fence.py`).
- **Four touches is a ceiling, four hours is a floor**, counted across ALL loops off the transcript (`touches_since_last_reply`), and `space_offsets` drops a touch rather than squeezing it. The count is read off the schedule everywhere (`max_followups_for`), never restated.
- **A closed messaging window blocks everything.** We never pay for templates.
- **Re-check `lead_is_suppressed` before EVERY customer send**, not once per run: a lead can be parked or booked between two ticks. One bad lead never stops the run.
- **Every send is gated by the timestamp written as it goes out**, so a tick is idempotent; `--dry-run` writes nothing and creates nothing.
- **Cron copy is outside the webhook's outbound chain**, so it goes through `dequalify_free_visit` where the message is composed.
- **Synthetic-key leads (`email_…`, `quotation_only_…`) get no proactive WhatsApp.**
- **Adding a cron = a new Railway service + `cronSchedule` + a `PLUMBOT_CRON` value**, never a start command in the dashboard. A deployment built without a schedule runs once and never ticks again while showing SUCCESS; see `_cronScheduleTrap` in `railway.json`.
