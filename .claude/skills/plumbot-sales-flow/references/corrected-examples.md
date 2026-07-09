# Corrected examples — real exchanges, bad reply vs. corrected reply

Mined from the production lead corpus (`python manage.py export_conversations`,
phone numbers masked). Every "Bad" reply below actually shipped and the outcome
shown actually happened. "Good" is what the corrected system sends (the approved
script where one exists). Conversation IDs are Appointment PKs for provenance.

Read the pairs for whichever rule you're touching before writing customer-facing
copy. When a new production bug is fixed, add one distilled pair here (customer
message + bad reply + corrected reply + one-line why), alongside its
`scenarios/` file and TEST 0 case. Strip customer names and keep only the turns
that carry the lesson.

---

## Never volunteer price

**Conv 415 (May 2026, ghosted).** Availability question answered with a price.

> **Customer:** "Hello! Do you for shower rooms"
>
> **Bad:** "Yes, we offer shower cubicles from US$170 all-in for supply and
> install, including ready-made units and custom builds. What area are you in
> so we can plan the visit properly?"
>
> **Good:** "Yes, we handle shower cubicles — supply and install, ready-made or
> custom. Is a shower cubicle the only thing you're looking to get sorted?"

Why: they asked *whether*, not *how much*. Leading with US$170 anchors on cost
before any value is built, and stacking the area question on top interrogates.
The good reply confirms, then continues the sale with the scripted
service-availability continuation.

**Conv 470 (June 2026, ghosted).** Buying statement answered with a price menu.

> **Customer:** "I want to purchase 2x shower cubics and asseries"
>
> **Bad:** "• Shower cubicle: Supply from US$130, Install from US$40 / Shower
> cubicles start from US$170 all-in — supply and install. / Already have the
> cubicle? Install-only from US$40. / These are approximate starting prices…
> What area are you in so we can plan the visit properly?"
>
> **Good:** "Nice one — two shower cubicles plus accessories, we can sort that.
> What area are you in so we can plan the visit?"

Why: a purchase commitment is past the Price stage — they're buying, not
shopping. Re-opening the price menu moves them *backwards* and invites
comparison. Acknowledge and advance the booking (`_is_purchase_commitment`
routes here, not to Q&A).

---

## "A quote" is not a price ask — and the fix converts

The same message, before and after the fix. This is the clearest conversion
evidence in the corpus.

**Conv 566 (2 July 2026, ghosted) — before:**

> **Customer:** "I would like to request a quote for plumbing services"
>
> **Bad:** "Our Facebook package is US$800 — freestanding tub and side chamber.
> We'll give you a fixed price once we've seen the space. That sit alright with
> your budget?"

A carried-over tub intent priced a generic quote request. Lead never replied.

**Conv 658 (8 July 2026, BOOKED) — after:**

> **Customer:** "I would like to request a quote for plumbing services"
>
> **Good (two messages, split):**
> 1. "We'll get you an exact, all-in figure free on a quick on-site visit."
> 2. "What area are you in?"

Lead answered the area, took the presumptive day offer, and booked. Quote
requests route to `_build_job_quote_reply` — visit pitch, zero figures.

**Conv 471 (June 2026, ghosted).** Same rule, plus wrong item: "Need a quote to
fit tub and shower" got the *shower cubicle* price block — a price they didn't
ask for, for an item they only half-named. A quote-for-job message gets the
visit pitch, never a price dump.

---

## Answer the current message (customer's words override gates)

**Conv 489 (June 2026, ghosted).** Follow-up question never answered.

> **Customer:** "Howa much to install a tub" → priced (correct — explicit ask).
> **Then:** "Corner tub how much"
>
> **Bad:** re-sent the identical freestanding-US$670 / built-in-US$160 block and
> pivoted to "Is this a full bathroom redo — tiling, fittings, the works…?" —
> the word "corner" was never addressed.
>
> **Good:** "A corner tub is a built-in unit — from US$160 all-in (tub US$80 +
> install US$80). Exact figure confirmed once the plumber sees the space. That
> sit alright with your budget?"

Why: corner tub = built-in (US$160), NOT freestanding. Repeating the previous
block instead of answering the new word is the carryover bug wearing another
hat. Close a price reply with the budget tie-down, not a stage question.

**Conv 411 (May 2026, ghosted).** Quoted photo + Shona, answered in English —
then the identity bug.

> **Customer (quoting a portfolio photo):** "This one rinoita marii"
>
> **Bad:** "That looks like a freestanding bathtub. The supply price for a
> freestanding tub starts from US$400, the mixer from US$150, and installation
> is US$120. Which area are you in?" — English reply to a Shona question, parts
> itemised with no all-in figure, open field question instead of a tie-down.
> The customer pushed back ("answer my question direct") and the bot then
> referred them to **"Tinashe"** — the wrong name.
>
> **Good (in Shona):** "Iri i-freestanding tub — rinotangira paUS$670 all-in
> (tub US$400 + mixer US$150 + kuisirwa US$120). Mutengo chaiwo unosimbiswa
> kana plumber aona nzvimbo yacho. Izvozvo zvirikuenderana ne budget yenyu
> here?"

Why: mirror the customer's language per message; lead with the all-in figure;
close on the budget tie-down; the plumber is **Takudzwa** — no other name
exists.

---

## Exits, defers, brush-offs

**Conv 490 (June 2026, ghosted).** Self-initiated defer pressured for a date.

> **Customer:** "Most probably during the weekend, l will get in touch."
>
> **Bad:** "No problem at all. Roughly when are you thinking of getting it
> sorted? Even a rough idea works — say next week, end of the month, or a
> specific day, and I'll set a reminder to check in with you then." (Lead's
> next message was "…" — then silence.)
>
> **Good:** "No problem at all — I'll leave it with you and check back after
> the weekend. If it helps in the meantime, I can email you our portfolio with
> past projects and full pricing so you've got everything to hand."

Why: they gave a timeframe AND said they'd initiate contact. Asking again for
what they just gave you reads as not listening (`_is_self_initiated_defer`
gates the booking pivot). Park with a check-back and one value-add.

**Conv 415 (May 2026, ghosted).** Brush-off answered with a non-sequitur.

> **Customer:** "Thanks will get back to you"
>
> **Bad:** "No problem at all. Roughly when do you think you'll be back in
> town?" — they never said they were out of town; a canned delay line fired on
> the wrong subtype.
>
> **Good:** "No problem at all. Before you go — we've got a portfolio with past
> projects and full pricing; want me to email it over so you've got everything
> to hand while you decide?"

Why: a soft brush-off gets ONE value-add attempt (portfolio by email + address
capture), then a graceful park. Never a scripted reply that contradicts what
they actually said.

**Conv 427 (May–June 2026).** Comparison shopping + "use this platform".

> **Customer:** "Let source other quotations then compare with yours then will
> come back to you" … then, asked for an email: "Kindly use this platform"
>
> **Good (this one was handled manually and became the approved counter):**
> agree, reframe the comparison axis — send the portfolio so they compare
> quality not just price, arm them to compare like-for-like ("check the others
> are all-in and guarantee the work"), ask their timeframe. And "use this
> platform / this chat" is a WhatsApp-delivery request — send it here, do NOT
> re-ask for an email (`wants_whatsapp_delivery`).

---

## Bot loops and dead ends

**Conv 482 (June 2026, ghosted).** The same confirm question three times.

> **Customer:** "Tiling" → bot: "is there any plumbing or water-related work
> involved?" → **"No"** → bot: "Just to be sure — is this actually for any
> plumbing work like pipes, drainage, or installation…?" → **"Nope"** → bot:
> "Just to confirm — is there any plumbing or water-related work involved?"
>
> **Good (after the first "No"):** "Ah, tiling on its own is outside what we
> do — we handle the plumbing side. If any pipes, drainage or fixtures come
> into it later, we're happy to help. All the best with the project!"

Why: the customer answered the question; asking it again (twice) is a loop.
A clear "no plumbing involved" is an out-of-scope exit — acknowledge, leave
the door open, stop. Related: a bare "No" to the property-scope tie-down
("Anything else on the property?") means "nothing else, proceed to the next
booking field" — it once fell to semantic-rescue, which read it as declining
the whole job and disengaged a warm lead
(`_last_assistant_was_value_check` + `_is_nothing_else_reply` →
`_advance_after_scope`).

---

## Service area

**Conv 328 (April 2026, Gweru).** Decline-list town offered a visit.

> **Customer (area reply):** "Gweru"
>
> **Bad:** "Great, what works better for you — Sunday the 19th or Monday the
> 20th — for us to come through and have a quick look at the site…" (plus two
> follow-up nudges to a lead we can't serve).
>
> **Good:** "Ah, sorry — Gweru is a bit far for our team to travel to, so we
> can't take this one on properly. If you've got a project nearer our side in
> future, we'd be glad to help."

**Conv 483 (June 2026, Magunje — correctly served, lead parked warm).**
"Not in Harare but in Hurungwe (Magunje) to be precise." → visit offered,
correctly: Homebase is mobile and Zimbabwe-wide. Only the eight decline-list
towns (Gweru, Bulawayo, Mutare, Masvingo, Victoria Falls, Hwange, Beitbridge,
Plumtree) are out; the decline says "too far to travel", never "Harare only".
