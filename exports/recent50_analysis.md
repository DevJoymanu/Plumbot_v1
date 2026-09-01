# The last 50 conversations vs everything before

**Recent 50:** 2026-06-21 → 2026-08-30. **Baseline:** the prior 208 (2026-02 → 06).
Same detectors as the earlier audit. No code changed.

Two corrections applied to my own method before comparing:

- Repetition is normalised **per assistant turn**, not per lead — recent conversations
  are half again as long (3.2 → 4.9 customer turns per lead), so per-lead counts would
  have flattered nothing and distorted everything.
- **Phantom log entries excluded.** 4 recent messages were stored with the raw
  `\x1fSPLIT\x1f` marker intact and then re-logged as their two real halves. They were
  never sent (no `message_id`, no `sent_at`), so customers never saw "SPLIT" — but
  counted naively they fake a near-duplicate every time. See "new defects" below.

---

## The refinements worked

| Measure | Earlier 208 | Recent 50 | |
| --- | ---: | ---: | --- |
| Booking rate | 5.3% | **10.0%** | up |
| Reply rate after a bot turn | 52.0% | **58.7%** | up |
| Reply rate after a cron follow-up | 5.8% | **18.3%** | 3x |
| Cron sends per lead | 3.6 | **2.4** | down |
| Human interventions per lead | 0.34 | **0.14** | **-57%** |
| Dismissive "your message is invalid" lines | 1.9 / 100 leads | **0** | gone |
| Picture ask answered with a price wall | 4.3 / 100 leads | **0** | gone |
| Bare ack answered with a question | 83.3% | **57.1%** | better |
| Re-pitch after the customer agreed | 9.1 / 100 leads | **4.0** | better |
| Engaged-and-lost whose last turn was a repeat | 44.0% | **11.1%** | much better |

Five of the nine recommendations from the previous audit show up as real movement.
The two biggest: **the cron stopped being spray** (a third of the volume per lead,
three times the reply rate) and **humans are needed less than half as often**.

The dismissive phrases are completely gone — zero occurrences in 350 assistant turns.

## What has not improved

| Measure | Earlier | Recent | |
| --- | ---: | ---: | --- |
| Near-duplicate messages / 100 assistant turns | 5.3 | 5.7 | slightly worse |
| Questions re-asked / 100 assistant turns | 5.3 | **6.9** | worse |
| Engaged then vanished, as share of leads | 12.0% | **18.0%** | worse |

Repetition did not get fixed — it got *rarer per lead-day but denser per turn*. The
recent re-asks concentrate on `what area are you in` (4x) and
`that sit alright with your budget` (3x).

**The failure mode has moved.** It is no longer "the bot is rude and repetitive."
It is now **"the bot will not stop, and it resolves the wrong item."**

---

## The four failures that account for the recent losses

### 1. Explicit stop requests are ignored by the cron

Lead `872` is the worst conversation in the recent set. A Bulawayo customer wanting a
house built:

- Bot correctly declined the area: *"Bulawayo is a bit far for our team to travel to, so we can't take this one on properly."*
- **The very next message was an `[AUTO FOLLOW-UP]` asking which suburb in Bulawayo the property is in** — contradicting the decline it had just issued.
- Customer twice asked *"Is this ai replying"* / *"Is this still Ai"*, then *"Can i have number of a real person please"*.
- Customer, at the end: *"🤣🤣🤣🤣installation of what"* — the bot was pitching plumbing site visits to someone asking about building a house.
- Customer: *"Thank you for your help. We will talk another time"* (exit signal).
- Customer: **"Ok send hear and please dont say anything more"** (explicit stop).
- Then: `[DELAY PORTFOLIO CHECK-IN]`, `[AUTO FOLLOW-UP]`, `[AUTO FOLLOW-UP]` — **three more pitches after being told to stop.**

Lead `847` is the same shape in miniature: *"Noo thank you"* -> *"Which one you need — bathroom, new plumbing, or kitchen?"*

CLAUDE.md guarantees exit-signal detection runs before any flow-stage logic **in the
webhook**. The follow-up crons reach the lead by their own eligibility path and never
consult it. That was recommendation 7 last time; it is now the single largest
remaining defect.

### 2. The bot prices the wrong item

Lead `844` — customer is discussing a **bathroom**, asks *"how much"*:

> *"That one — Borehole. Borehole from US$500 all-in (supply from US$300 + install from US$200)"*

Then four consecutive `[AUTO FOLLOW-UP]`s asking what work they want for the
*bathroom renovation* — the bot's own stored `project_type` contradicting the price
it had just quoted.

### 3. Supply-only customers get pitched a site visit

Lead `863` said plainly: **"We want 2 tubs like this but just supply"**

- -> *"We'll get you an exact, all-in figure free on a quick on-site visit."*
- -> gave their location; -> *"what works better for you, tomorrow or this Tuesday, for us to come through and have a quick look at the bathroom"*

There is nothing to look at. The same lead earlier asked *"Your location please"* and
was answered with an explanation of why email is the best delivery format.

### 4. A quote request still deflects to the visit

Lead `860` (the only recent **cancelled** lead): *"Sorry I think I asked for a
quotation first"* -> *"We'll get you an exact, all-in figure free on a quick on-site
visit. / What area are you in?"* A human then had to step in with a phone number.

---

## New defects introduced since the refinements

**Empty price template.** Lead `844` received, verbatim:

```
Here's the full pricing for that piece, covering everything in the photo:
- 

What area are you in so we can plan the visit properly and get you your accurate free quote?
```

When the quoted item cannot be resolved, the template still prints its header and a
dangling bullet. Lead `863` shows the same template working correctly, so it is the
resolution failing, not the copy.

**Split-marker phantom entries.** 4 messages stored with `\x1fSPLIT\x1f` intact
alongside their correctly-split halves. Customer-invisible, but it triple-logs one
reply — which corrupts the transcript, any analysis run over it, and any LLM call that
reads history back.

---

## Confidence

Weak on the outcome number, strong on the mechanism. **The booking rate "doubling" is
5 bookings versus 11** — at these sample sizes that difference is not significant, and
the recent cohort also skews toward leads with more turns. Do not bank the 10%.

What *is* solid: 350 assistant turns with **zero** dismissive lines and **zero**
mishandled picture asks, against a baseline where both were routine. Those are real,
and they are the things the refinements targeted.

The engaged-and-lost share rising (12% -> 18%, n=9) is worth watching but is nine
leads; four of them are explained by the failures above.
