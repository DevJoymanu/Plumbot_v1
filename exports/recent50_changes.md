# Last 50 conversations: what to change now

Window: **2026-06-21 → 2026-08-30**, 50 leads, 350 assistant turns, 244 customer turns.
Baseline for comparison: the prior 208 leads (2026-02 → 06). No code changed.

Two method corrections applied first: repetition is normalised **per assistant turn**
(recent conversations run 4.9 customer turns/lead vs 3.2), and 4 phantom
`\x1fSPLIT\x1f` log entries are excluded — they were never sent, but counted naively
they fake a duplicate every time.

---

## Where the refinements landed

| Measure | Earlier 208 | Recent 50 | |
| --- | ---: | ---: | --- |
| Human interventions per lead | 0.34 | **0.14** | **-57%** |
| Reply rate after a cron follow-up | 5.8% | **18.3%** | 3x |
| Reply rate after a bot turn | 52.0% | **58.7%** | up |
| Dismissive "your message is invalid" lines | 1.9 / 100 leads | **0** | gone |
| Picture ask answered with a price wall | 4.3 / 100 leads | **0** | gone |
| Bare ack answered with a question | 83.3% | **57.1%** | better |
| Engaged-and-lost whose last turn was a repeat | 44.0% | **11.1%** | much better |
| Booking rate | 5.3% | 10.0% | see caveat |

Five of the nine earlier recommendations show real movement. **Do not redo these** —
the dismissive phrases, the picture-request deflection, and the cron spray are fixed.

**Caveat on the headline:** the booking "doubling" is **5 bookings vs 11**. Not
significant at this n. The solid results are the zeroes across 350 turns.

---

## The evidence base for what is left

### Every human intervention in the recent 50 — only 7, and 6 were cleanup

| Lead | What the human had to do | Genuine business work? |
| --- | --- | --- |
| `874` | *"Can we shift our appointment to 2pm same date"* | **Yes** — a real reschedule |
| `490` | *"**Sorry for being so repetitive**, the reason I ask is..."* | No — apologising for the bot |
| `490` | *"Great I see it, what's the best email to reach you on?"* | No — bot missed the answer |
| `491` | *"All good, what area are you in"* | No — doing the bot's own question |
| `491` | *"...Tomorrow or this Wednesday...for us to come through"* | No — doing the bot's own close |
| `876` | *"...can you please your location on WhatsApp 0773871503"* | No — manual handoff |
| `860` | *"...send your blue print or house plan on WhatsApp 0773871503"* | No — manual handoff |

**One of seven was real work.** The other six were a person compensating in real time.

Lead `490` is the sharpest single piece of evidence in the whole dataset. The human
wrote *"Sorry for being so repetitive"* — and the customer replied:

> **"I did give a timeframe here"**

The customer is telling you directly that the bot re-asked something already answered.

### The 9 leads that engaged and then went silent

| Lead | What killed it |
| --- | --- |
| `872` | Said **"please dont say anything more"** — got 3 more automated pitches |
| `847` | Said *"Noo thank you"* -> *"Which one you need — bathroom, new plumbing, or kitchen?"* |
| `844` | Asked "how much" about a bathroom -> quoted **Borehole**, then an empty price block |
| `863` | Said *"just supply"* -> pitched an on-site visit, twice |
| `860` | Asked for a quotation -> got the visit line; **only recent cancellation**, needed a human |
| `858` `878` `687` `663` | Chased 1–3 times after a soft close |

### Repetition, recent 50

44 repeat events. **11 of them (25%) fired after the customer had already stopped
replying** — the follow-up layer re-asking into silence. Most re-asked:

| Times | Question |
| ---: | --- |
| 4 | what area are you in |
| 3 | that sit alright with your budget |
| 2 | just to confirm is there any plumbing or water-related work involved in this |
| 2 | the borehole option starts at US$… |

Lead `663` alone has 11 repeat events, cycling back to the cold opener
*"Hello, How may we assist you on plumbing services / Which area are you in?"*

---

## The changes, in priority order

### 1. Stop means stop — gate the follow-up crons on exit signals

The largest remaining defect, and the one with the ugliest transcript.

Lead `872` said **"Ok send hear and please dont say anything more"**, then *"Thank
you"* — and received `[DELAY PORTFOLIO CHECK-IN]`, `[AUTO FOLLOW-UP]`,
`[AUTO FOLLOW-UP]`. Earlier in the same conversation the bot had correctly declined
the area (*"Bulawayo is a bit far..."*) and **the very next message was an
`[AUTO FOLLOW-UP]` asking which suburb in Bulawayo** — the cron reversing a decision
the bot had just made.

CLAUDE.md guarantees exit-signal-first ordering *in the webhook*. `send_followups`,
the delay nudges, and the parked nudges reach the lead by their own eligibility path
and never consult it. **Exit/stop state needs to be a property of the lead that every
send path reads, not a branch in one handler.**

### 2. A question the customer has answered must not be re-askable — including by the cron

25% of recent repeats fired after the lead went quiet. A human had to apologise for
this out loud on lead `490`, and the customer's own reply confirmed the answer was
already on file.

Concretely: `846` asked *"catalog by email, or sent right here on WhatsApp?"* twice
after silence; `844` sent the borehole price line twice after silence; `684` asked
*"does that sit alright with your budget"* into silence.

The semantic duplicate detector exists. It is not covering the follow-up path.

### 3. Make handoff a first-class trigger, not a manual paste

**2 of the 7 human interventions were a person pasting `0773871503` by hand.** The
signals that should have fired it were all present and explicit:

- `872`: *"Is this ai replying"*, then *"Is this still Ai"*, then *"Can i have number of a real person please"*
- `860`: *"Sorry I think I asked for a quotation first"* (second attempt at the same ask)

Note from the earlier audit: manual handoffs had the *worst* reply rate of any human
intervention (33%). Handing over late does not work — it has to fire on the signal.

### 4. An out-of-area decision must stick system-wide

Lead `872` was declined for Bulawayo and then asked for its suburb, then pitched site
visits repeatedly. The decline needs to write lead state that suppresses booking
pitches and follow-ups, not just produce one polite sentence.

### 5. Separate supply-only from supply-and-install

Lead `863`: **"We want 2 tubs like this but just supply"** -> *"We'll get you an exact,
all-in figure free on a quick on-site visit"* -> after they gave a location, the visit
close again. There is nothing on site to look at. Supply-only should route to a
quote/delivery path, never the visit ladder.

### 6. Resolve the item before pricing it — and never print an empty price block

Lead `844` received this verbatim:

```
Here's the full pricing for that piece, covering everything in the photo:
- 

What area are you in so we can plan the visit properly and get you your accurate free quote?
```

The same template renders correctly on lead `863`, so the copy is fine and the
resolution is failing. Same lead was quoted **Borehole** pricing for a bathroom
enquiry while its own `project_type` read `bathroom_renovation`. A price reply whose
item list is empty should not send at all.

### 7. A quotation request is not a visit pitch

Lead `860` — the only cancellation in the window — asked twice for a quotation and got
the on-site-visit line both times, then needed a human. This is the
quote-vs-price-figure split not holding on the second attempt.

### 8. Cap the two questions that now dominate re-asks

*"what area are you in"* (4x) and *"that sit alright with your budget"* (3x). The
budget tie-down in particular is being re-fired into silence.

### 9. Two data-hygiene items that cost nothing and block measurement

- **Phantom split entries.** 4 recent messages stored with the raw marker alongside
  their real halves — customer-invisible, but it triple-logs one reply and corrupts
  the transcript that later LLM calls read back.
- **The dashboard's free-text send is still untagged**, so silent human takeovers are
  invisible. Every "human intervention" figure here is a floor. Tagging it is the
  prerequisite for knowing whether item 3 actually worked.

---

## What this adds up to

The bot's *manners* are fixed — no dismissals, no price-walling a picture request, far
less spraying. What remains is **a control problem, not a tone problem**: the system
does not reliably honour decisions it has already made (stop, declined area, answered
question, supply-only, quote-not-visit).

Items 1–4 are all the same underlying gap — **decisions live in one handler instead of
on the lead**, so every other send path re-litigates them. That is where the remaining
losses are concentrated, and it is one architectural fix rather than seven patches.
