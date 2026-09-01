# What to change, based on what humans had to fix and where interested leads died

Evidence: 258 live transcripts. Two populations matter — the **44 leads who engaged
substantively and never booked**, and the **77 messages a human had to send by hand**.

No code was changed. Each item below names the evidence and the size of the effect.

---

## The one number that frames everything

| Turn type | Sent | Customer replied next | Rate |
| --- | ---: | ---: | ---: |
| Live bot reply | 841 | 450 | **53.5%** |
| Human (staff) message | 74 | 42 | **56.8%** |
| Cron follow-up | 879 | 67 | **7.6%** |

Half of all outbound volume is cron follow-ups, and they are answered one time in
thirteen. The bot in live conversation performs about as well as a human. **The
problem is not the bot's live replies — it is what happens when the bot loses the
thread, and what the cron does afterwards.**

---

## 1. "Show me" must outrank the qualification ladder

**Evidence.** 41 customers asked to see something (pictures, catalogue, portfolio,
"your Facebook"). 21 got pictures. **9 got a price list or another qualification
question instead**, and 3 of those never wrote again.

- lead `54`: *"Pliz share pics of ur work"* -> *"Do you have a plan already, or would you like us to do a site visit?"*
- lead `71`: *"May I please have pricing nad pictures of your free standing tubs"* -> *"Do you have a plan already, or would you..."*
- lead `273`: *"may you kindly share your catalogue"* -> *"Which service are you interested in?"*
- lead `443`: *"Kindly share your catalogue for free standing bath tubs"* -> a price paragraph, no pictures

**Corroboration from the humans.** Picture-related staff messages had the **highest
reply rate of any human intervention (67%, 10 of 15)** — higher than money talk (43%)
or handing over a phone number (33%). The single most valuable thing a human did was
show the customer something.

**Change.** A request to *see* something should resolve deterministically to a
portfolio send before stage routing — the same precedence price intent already has.
The portfolio machinery already exists; it is losing the routing contest.

## 2. Stop replying to a bare acknowledgement with a question

**Evidence.** 25 customers sent a bare ack ("Okay 👍", "Ohk", "Thanks", "Noted").
**19 got a question back. 8 of those 19 ended the conversation permanently** — a 42%
kill rate on a turn that carried no new information.

- lead `62`: *"Okay 👍"* -> *"Perfect. When would you be available for an appointment? Please provide both the day and time..."* (the identical sentence it had already sent five times)
- lead `129`: *"Thanks 👍"* -> *"Thank you! Which service are you interested in?"*

An ack is a conversational full stop, not an unanswered field. Acknowledge and stop,
or say nothing and let the cadence work.

## 3. Cap re-asking; a third ask should be impossible

**Evidence.** Across the corpus the bot re-asked *"which service are you interested
in"* **17 times**, *"when would you be available"* 9, *"which area are you in"* 6.
Lead `62` received the identical availability sentence **six times, twice
byte-identical**. **38.6% of engaged-but-unbooked leads (17 of 44) had a repeat as
the last thing they were sent.**

CLAUDE.md says a semantic duplicate detector gates qualification questions. On this
evidence it is not gating these paths. A question asked twice without an answer
should change form or escalate — never fire a third time.

## 4. Retire plan-or-visit as the catch-all

**Evidence.** *"Do you have a plan already, or would you like us to do a site visit?"*
is the most common last thing an engaged lead ever hears — 5 as the direct final
question, and it dominates the 15 leads whose final turn "pitched the visit"
(47% of which were repeats).

It has become the fallback for *"I did not understand that"*, which is why it lands
on people who asked something else entirely: lead `65` asked for modern bathroom
pictures and got it; lead `54` said *"Toilets"* and got it.

## 5. Never ask what the customer just told you

- lead `84`: *"Are your renovations restricted to bathrooms and kitchens only?"* -> *"We also offer New Plumbing Installation services. Which service are you interested in? We offer: Bathroom Renovation..."*
- lead `54`: *"Toilets"* -> *"I understand you're interested in toilet services. Do you have a plan already...?"*
- lead `304`: *"and thi"* -> the full cold-opener service menu, context discarded

This is the "customer's own words override any gate" rule in CLAUDE.md failing in the
answer path rather than the pricing path.

## 6. Delete four sentences outright

Every one of these tells the customer their message was defective:

| Phrase | Seen | What triggered it |
| --- | ---: | --- |
| *"I didn't receive any previous message"* | 1 | lead `89`: *"Did you see this👆"* |
| *"I don't see any new information in your message"* | 1 | lead `56`: *"Did i ?"* |
| *"Could you please clarify..."* | 2 | lead `141`: *"Insuit and the other"* |

Small counts, outsized damage: **2 of the 6 recorded customer complaints in the
entire corpus were direct replies to these lines.** Lead `89` answered *"I said i wil
contact you in due course"* and left.

## 7. The follow-up cadence is the biggest single bleed

879 cron sends, 67 replies. Lead `325` received **three consecutive AUTO FOLLOW-UPs**
with no customer turn between them. Lead `872` said *"Thank you for your help. We will
talk another time"* and was sent a visit re-pitch seven turns later.

Two changes, both cheap:
- **Make follow-up #1 different in kind** — a picture or a portfolio link, not a
  nudge. That is what worked when humans did it by hand.
- **Exit signals must gate the cron, not just the webhook.** CLAUDE.md guarantees
  exit-signal-first ordering in `whatsapp_webhook.py`; `send_followups` reaches the
  lead by its own eligibility path and does not honour it.

## 8. Give the bot the repair move the humans had to improvise

10 staff messages existed only to repair a bot misfire. Humans twice invented a
network excuse as cover:

> *"Sorry, Network yanga ya dakwa, this text was meant for another client — I meant to ask kuti which area are you located in?"*

> *"so sorry about that, that text was meant for another client..."* (lead `75`, which went on to **confirm**)

A lead who writes *"I said..."*, *"as I said earlier"*, or *"answer my question
direct"* should trigger acknowledge-and-correct. Today it triggers another
qualification question.

## 9. Escalate on the signals that already exist

Only **1 lead in 258** was ever marked `HANDED_OFF`. Meanwhile:

- lead `872`: *"Can i have number of a real person please"*
- lead `411`: *"Ndikuita kunge ndikutaura ne chartbot answer my question direct"*

Both are unambiguous handoff triggers. Three humans manually pasted a phone number to
do this by hand — and that was the *worst*-performing human intervention (33% reply),
which suggests handing over late, as a last resort, does not work. It needs to happen
at the signal.

---

## Confidence

Directional, not conclusive. The engaged-and-lost population is 44 leads and the
booked population is 16, so no single percentage here is precise. The rankings are
driven by effects large enough to survive that: 7.6% vs 53.5% reply rates, a question
repeated 17 times, an identical sentence sent 6 times to one lead.

**The measurement blind spot worth closing first:** the dashboard's free-text send
logs staff messages as plain bot turns, so silent human takeovers are invisible. The
77 counted here are a floor. Until that is tagged, "how often does a human have to
rescue this" cannot be answered exactly.
