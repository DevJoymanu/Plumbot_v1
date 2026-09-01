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

**Lead 670 (July 2026, retired question).** Scope trivia in place of a booking
question.

> **Customer:** "Do you have shower cubicles" → "No, I also want a tub"
>
> **Bad:** "Great, so a shower cubicle and a tub. Are you looking to do a full
> bathroom installation or just those items?" → customer: "What's the
> difference" → a paragraph about renovation scope, no date asked.
>
> **Good:** "Great, so a shower cubicle and a tub." + the next open stage —
> here the visit day, since the area was already known.

Why: the answer changes nothing — the free visit prices whatever is actually
there — so the question buys no information and hands the lead a tangent to
wander into. **Do not ask a lead to settle scope.** Acknowledge what they named
and ask the next booking field. Retired everywhere: the semantic-rescue keyword
branch and its DeepSeek prompt rule, `_affirm_and_progress`, and the
`_FORWARD_BANK` service stage.

**Lead 670 (July 2026).** A yes to the budget tie-down answered as if it were a
question.

> **Customer:** "How much tub" → priced correctly, closed on the budget
> tie-down.
> **Then:** "Yes"
>
> **Bad:** "Yes, we handle tub and all related plumbing work. Is a tub the only
> thing you're looking to get sorted?"
>
> **Good:** take the yes as the micro-yes it is and move to the next booking
> field — "Nice one. Whereabouts are you?"

Why: the classifier kept `product_intent=tub_sales` alive from the previous
turn, so a bare "Yes" was routed into the services-availability answer — the
carryover bug again, this time answering a question the lead never asked. A
message that asks nothing can never be read as a question
(`_is_bare_affirmation` now gates that route). The second half of the reply was
also a symptom: the priced item wasn't captured as the job, so the flow still
sat on project_description with nothing to show for a product they had already
named — single-product price replies now capture like the multi-item and quote
paths always did.

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

**Conv 846 (Aug 2026, barmak).** Asked for an email four times, never once said
why.

> **Bot (bad):** "No problem at all. Let me email you our catalog with the full
> pricing structure so you've got something to look over while you decide, and
> I'll set a reminder to check back in. What's the best email to reach you on?"
>
> **Customer:** "Munongo senda ipapa handi wanzo gara ne data" (just send it
> here, I do not usually have data)
>
> **Bot (good):** "No problem at all. Let me send our catalog over so you've got
> something to weigh up while you decide, and I'll set a reminder to check back
> in.
>
> It goes over as a PDF — the full price breakdown and past jobs in one document
> — so you can open it any time, send it on to whoever else is in on the
> decision, and hold it up against any other quotes you get.
>
> What's the best email for it?"

Why: the old ask stated OUR reason to want an address ("so I can email you") and
gave the lead none of their own, so the path of least resistance was always
"send it here". The replacement names three benefits that are genuinely theirs —
it keeps (a document in a chat is gone when the phone changes), it travels
(these jobs are rarely one person's decision), it compares (a quote-collector
can put ours beside the others). Shared clause: `_EMAIL_VALUE_CLAUSE`.

The delivery choice recommends rather than shrugs — "either works" gives them
nothing to choose on — but a lead who still says WhatsApp is sent it on WhatsApp
and never asked twice (conv 427's rule, now enforced by
`_already_asked_delivery_choice`).

It also names OUR reason plainly — "I can keep your quote properly on file and
follow things up cleanly, rather than it slipping down the WhatsApp thread"
(owner-written, 2026-08-29). Stating the business reason straight reads as
straight dealing rather than extraction, and being followed up properly is the
lead's benefit as much as ours. Its trailing timeframe ask
(`_DELIVERY_CHOICE_TIMEFRAME_TAIL`) is appended ONLY when no check-back date is
on file — with a date already agreed we have just named it, and asking again is
the conv 415 mistake. Having asked, the handler also captures a timeframe reply
instead of force-fitting it as a failed email address.

**Conv 846b (Aug 2026, barmak ad lead).** A delay signal read as a scope answer.

> **Bot:** "Is a tub the only thing you're looking to get sorted?"
>
> **Customer:** "No my main bedroom is not yet sorted will get in touch ndasvika
> pa stage iyoyo thanx" (…will get in touch when I reach that stage)
>
> **Bad:** "No problem — what else would you like sorted while we're there?"
>
> **Good:** acknowledge the defer, park with a check-back date, offer the
> portfolio. They are leaving, not adding to the job.

Why: the leading "No" answered our question by accident. The
`[SERVICE_CONFIRM_PENDING]` branch answers and RETURNS before STEP 1b, so the
delay handler never saw the message — the sixth time a pending state has
swallowed a live signal. Any branch that holds the lead in a flow must let a
delay/exit signal through first (`_sc_delay_override`, and the tags are cleared
so the scope question cannot re-fire on their delay-flow answer). Second fault:
"will GET in touch" was in no list, only "will BE in touch" — now covered along
with the "not at that stage yet" family (`_NOT_YET_STAGE_RE`, which tolerates
the words Shona puts in between: "handisati HANGU ndasvika"). Bare "get in
touch" is deliberately NOT a deferral — "I want to get in touch with your
plumber today" is an eager lead.

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

---

## Clarifying questions name the customer's own subject

**Conv (barmak, 2026-08-30).** An out-of-scope ask answered in the abstract.

> **Customer:** "Cost of wiring a new 4 bedroom house"
>
> **Bad:** "Just to confirm — is there any plumbing or water-related work
> involved in this?"
>
> **Good:** "Just to clarify, is the wiring you're asking about related to
> plumbing or water systems in the house?"

Why: "this" makes the customer work out what we read, and whether we read the
word 'wiring' at all — it lands as a deflection rather than an answer. Naming
their own word back costs nothing and proves we were listening. Use THEIR
vocabulary, not our label: someone who wrote "wiring" is asked about the wiring,
never about "the electrical work". `_oos_subject` pulls the subject as they
typed it (longest match first, so "electrical work" is never truncated to
"electrical"), and the verb agrees with it — "are the burglar bars", never "is
the doors". Only a message naming nothing recognisable still asks in the
abstract.

---

## Say WHEN in the lead's own words

**Conv (2026-09-01, Dzivarasekwa extension).** A parked lead read out a date.

> **Customer:** "Let me update you tomorrow morning"
>
> **Bad:** "Got it, no problem. We'll check back with you right here on
> Wednesday 02 September.
>
> If anything changes before then, just send a message."
>
> **Good:** "Got it, no problem. We'll check back with you tomorrow morning.
>
> If anything changes before then, just send a message."

Why: the bad reply is not wrong — Wednesday 02 September IS tomorrow. It is a
diary entry read back to someone who spoke in ordinary words, and that one
line is enough to make the whole thread feel automated. Their own words
("tomorrow morning") are the proof we read them, and they cost nothing to
repeat. "right here" went with it: in a WhatsApp thread, "we'll check back
with you" already means here.

`_checkback_when_phrase` is the shared resolver — 'tomorrow morning', 'this
evening', 'tonight', 'on Friday afternoon' — and it falls back to the formal
date only past six days out, where a bare weekday would be ambiguous. The
daypart comes from the customer's own wording (or the time they named earlier
in the flow, kept in `[FOLLOW_UP_TIME]`), never from a clock time they gave:
"Friday at 2" is an hour, not someone saying "Friday afternoon". The phrase
carries its own preposition, because "on tomorrow morning" is not English.

Same rule, same message, for language: a Shona lead hears "mangwana
mangwanani", not an English date.

---

## A new build is confirmed back, not qualified blind

**Conv 566 / prod (new-build leads).** A structure with no plumbing in it yet.

> **Customer:** "It's a new building and we require installation of all the
> plumbing requirements on the plan"
>
> **Bad:** "Is it a bathroom or a kitchen you're looking to get sorted?"
> (or "Got it! Can you tell me a bit more about the project?")
>
> **Good:** "So you need a new plumbing installation for a new building?"

Why: a new build is a different job from a refit — nothing to renovate, the
whole system goes in from scratch — so the bathroom/kitchen this-or-that is a
question it has no answer to, and "tell me a bit more about the project" throws
the lead's own words back at them. Confirming the scope is a micro-yes: it
proves we read the message and starts the ladder on a yes.

Use THEIR noun: someone who wrote "building" is asked about a building, not a
house. `_new_build_subject` is the resolver (adjacency-bound: "a new bathroom
in my house" is a refit, not a build; "Dzivarasekwa extension" is a suburb),
`_new_build_confirmation` the gate. It fires only at the two scope-gathering
stages and only once — asked twice it is the bot loop — and it records the
service type as it asks, so the "yes" advances to the project detail instead
of falling back to "bathroom or kitchen?".

---

## A new build beats the out-of-scope hold

**Conv (2026-09-01).** The clarification was right; the verdict was not.

> **Customer:** "Cost of wiring a new 4 bedroom house"
>
> **Bot (correct):** "Just to clarify, is the wiring you're asking about
> related to plumbing or water systems in the house?"
>
> **Customer:** "It's a new building"
>
> **Bad:** "We specialise in plumbing and bathroom/kitchen renovations, so
> that service is outside what we do. For that you'd need a specialist…"
>
> **Good:** "So you need a new plumbing installation for a new building?"

Why: the customer never said the wiring was plumbing, so the classifier stayed
on out_of_scope and the decline fired — technically defensible, commercially
the worst possible reply. A four-bedroom house going up needs a full plumbing
system whatever else it needs, and they had just told us there was one. We shut
the door on a live new-build lead over the one trade they happened to ask about
first.

The wiring is still not ours; the building is. `_mentions_new_build` (one
resolver shared with the booking flow, so the decline and the confirmation can
never disagree) breaks the OOS hold and hands the lead back to the flow, which
names the job back to them. The guard stays narrow: an answer with no build in
it — "no, it's the electrical wiring" — still declines, or the module would
never turn anyone away again.

DeepSeek answers it, off the `new_build` field of the same `unified_classify`
result the turn already computes — no extra round trip — and it earns the seat:
the opener here, "cost of wiring a new 4 bedroom house", has words between
"new" and "house", so the adjacency-bound regex misses it, and loosening the
regex to span them would also swallow "a new bathroom in my house". Live: that
message classifies `intent=out_of_scope` AND `new_build="house"` at once, which
is exactly the shape of the case.

**Add-only** — the classifier may widen the signal but never close it. A phrase
the regex fallback knows still counts with the API down or disagreeing, because
the failure costs are lopsided: over-firing asks one question the lead waves
off, missing one declines a live lead.

This is the same recurring bug as the delay-hold and pricing-gate cases above:
a holding state outranking what the customer just said.
