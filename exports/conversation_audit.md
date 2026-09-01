# Conversation audit — human intervention, repetition, and friction

Source: 258 lead conversations exported from the live CRM (`exports/conversations.jsonl`). Phones masked to last 3 digits.

## Headline

| Measure | Leads | Events |
| --- | ---: | ---: |
| Human intervention | 51 | 96 |
| Bot repetition | 67 | 202 |
| Re-pitch after the customer already agreed | 8 | 23 |
| Bot dismissed / contradicted the customer | 4 | 4 |
| Customer complained about the bot | 5 | 6 |

Booking rate overall: **6.2%** (16 of 258).  
Booking rate where a human stepped in: **15.7%** (8 of 51).  
Booking rate where the bot repeated itself: **13.4%** (9 of 67).

## 1. Human interventions

| Kind | Events |
| --- | ---: |
| `tagged_staff_send` | 77 |
| `staff_file_upload` | 15 |
| `dashboard_confirm` | 3 |
| `handed_off_to_human` | 1 |

> **Blind spot.** The dashboard's free-text "send message" action (`bot/views/appointments.py`) logs the staff member's message as a plain `assistant` turn with no tag and no source field — identical to a bot reply. Silent takeovers are therefore **not counted anywhere above**; the real intervention figure is a floor, not a total.

### What the human actually had to say

**Apologising for / covering a bot mistake** — 11 messages

- lead `54` (pending): customer said *"Hello! Can I get more info on this?"* -> human sent *"Sorry could you please type"*
- lead `56` (pending): customer said *"Did i ?"* -> human sent *"sorry, did you?"*
- lead `58` (pending): customer said *"Let try to send the plan when I get home"* -> human sent *"Sorry, Network yanga ya dakwa, this text was meant for another client I meant to ask kuti which area are you located in?"*
- lead `60` (pending): customer said *"Hello! Can I get more info on this?"* -> human sent *"Sorry, Network yanga ya dakwa, I meant to ask kuti which service are you interested in? We offer: Bathroom renovation, New plumbing installation, or Kitchen renovation"*

**Handing the lead to a phone number or person** — 3 messages

- lead `62` (pending): customer said *"Any day"* -> human sent *"please text 0774819901 about your enquiry"*
- lead `860` (cancelled): customer said *"Sorry I think I asked for a quotation first"* -> human sent *"Good morning sir you can send your blue print or house plan on WhatsApp 0773871503 for quotation"*
- lead `876` (confirmed): customer said *"Mrs Ncube 9A madokero gardens"* -> human sent *"Hie Ms. Ncube lm Kudakwashe Marange can you please your location on WhatsApp 0773871503"*

**Asking for a photo or plan the bot never got** — 9 messages

- lead `54` (pending): customer said *"Tynwald South"* -> human sent *"for price ye toilet, the plumber will either need to come and see the place or you can send a plan(kunyange picture wenzvimbo yacho anoshanda) for him to give you a price ..."*
- lead `65` (pending): customer said *"Bathroom sets pictures, morden"* -> human sent *"Is it possible for you to send a picture of the bathroom you want to put a shower cubicle in?"*
- lead `66` (pending): customer said *"Ohk"* -> human sent *"Do you maybe have a picture of the bathroom you want to put the tub"*
- lead `67` (pending): customer said *"Do you sell tubs for small bathrooms"* -> human sent *"Do you maybe have a picture of the bathroom you want to put the tub in?"*

**Driving the booking the bot stalled on** — 15 messages

- lead `54` (pending): customer said *"Tynwald South"* -> human sent *"for price ye toilet, the plumber will either need to come and see the place or you can send a plan(kunyange picture wenzvimbo yacho anoshanda) for him to give you a price ..."*
- lead `75` (confirmed): customer said *"Yes"* -> human sent *"When were you hoping to get this done?"*
- lead `75` (confirmed): customer said *"A visit will do.l don't have a plan"* -> human sent *"What I meant to ask was, when were you hoping to get this done?"*
- lead `75` (confirmed): customer said *"It will depend on the quote"* -> human sent *"When were you looking to have the Plumbing site visit?"*

## 2. Repetition

| Lead | Status | Turns | Repeat events |
| --- | --- | ---: | ---: |
| `141` ...812 | confirmed | 54 | 18 |
| `663` ...200 | pending | 36 | 12 |
| `62` ...104 | pending | 41 | 11 |
| `75` ...868 | confirmed | 47 | 9 |
| `102` ...ing | pending | 24 | 9 |
| `84` ...429 | pending | 16 | 6 |
| `658` ...907 | confirmed | 23 | 6 |
| `54` ...883 | pending | 20 | 5 |
| `71` ...740 | pending | 17 | 5 |
| `89` ...064 | pending | 16 | 5 |
| `344` ...539 | confirmed | 22 | 5 |
| `840` ...211 | pending | 17 | 5 |

### The questions the bot re-asks most

| Times re-asked | Question (normalised) |
| ---: | --- |
| 17 | which service are you interested in |
| 9 | when would you be available for an appointment |
| 6 | which area are you in |
| 5 | when were you hoping to get this done |
| 4 | do you have a plan already or would you like us to do a site visit |
| 4 | do you have a plan already for the bathroom renovation or would you like us to do a site visit |
| 3 | do you have a plan already or would you like us to do a site visit to discuss tub options |
| 3 | roughly when are you thinking of getting it sorted |
| 3 | just to confirm is there any plumbing or water related work involved in this |
| 3 | that sit alright with your budget |

### Worst single case

Lead `141` (confirmed, 54 turns, 18 repeat events).

Word-for-word resends of the same message: **3**.

> Great! When were you hoping to get this done?

## 3. Where the bot fought the customer

### 3a. Re-pitching the visit after the customer already settled it

**Lead `75`** (confirmed) — 5 re-pitches after the customer already agreed to the visit at turn 10

- customer: *"Yes"*
- turn 11 (bot): *"Perfect. Do you have a plan already, or would you like us to do a site visit?"*
- turn 16 (bot): *"Perfect. Do you have a plan already, or would you like us to do a site visit?"*
- turn 19 (bot): *"Do you have a plan already, or would you like us to do a site visit?"*
- turn 23 (bot): *"I understand. To provide you with an accurate quote, do you have a plan already, or would you like us to do a site visit first?"*

**Lead `141`** (confirmed) — 2 re-pitches after the customer already agreed to the visit at turn 29

- customer: *"Ok thank u prices of plumbing"*
- turn 30 (bot): *"Here are our approximate prices 😊 🛁 *Bathroom Renovation* - Full renovation: from US$600 - Bathtub installation (with wall finishing): from US$80 - St ..."*
- turn 46 (bot): *"No, you don't need to pay for the quotation. Our site visit and quotation are provided free of charge. When would you be available for an appointment? ..."*

**Lead `197`** (pending) — 1 re-pitches after the customer already agreed to the visit at turn 4

- customer: *"Ok"*
- turn 6 (cron): *"[AUTO FOLLOW-UP] Hi there, what made you reach out? Most people don't message unless something's actually bothering them about their space. Help me un ..."*

**Lead `272`** (pending) — 7 re-pitches after the customer already agreed to the visit at turn 7

- customer: *"Yes"*
- turn 8 (bot): *"Monday the 13th or Tuesday the 14th better for the free site visit? We're booking up this week."*
- turn 10 (bot): *"Monday the 13th or Tuesday the 14th better for a free site visit? We're booking up this week."*
- turn 11 (cron): *"[AUTO FOLLOW-UP] Hi there, is it that you're not sure if the visit is worth it, or is it more of a timing thing? Either way, the visit is free, takes  ..."*
- turn 13 (bot): *"Monday the 13th or Tuesday the 14th for a free look at the bathroom? We're booking up this week."*

**Lead `325`** (pending) — 3 re-pitches after the customer already agreed to the visit at turn 7

- customer: *"Ok will let you know when you can visit I'm in Chitungwiza"*
- turn 14 (cron): *"[AUTO FOLLOW-UP] Hi there, the visit is free and takes about 20 minutes — it locks your price in before anything starts. Do you have a plan already, o ..."*
- turn 15 (cron): *"[AUTO FOLLOW-UP] Hi there, we recently wrapped up a job where the client had their plans sorted — it really sped things up for them. Would a quick fre ..."*
- turn 16 (cron): *"[AUTO FOLLOW-UP] Hi there, slots are filling up this week. Do you have a plan for the plumbing work sorted, or would it help to have us come out for a ..."*

**Lead `335`** (pending) — 2 re-pitches after the customer already agreed to the visit at turn 12

- customer: *"Yes l have plan let me send you"*
- turn 13 (bot): *"Perfect! You can send your plan whenever you're ready. Great, what works better for you — Monday the 20th or Tuesday the 21st — for us to come through ..."*
- turn 15 (cron): *"[AUTO FOLLOW-UP] Hi there, are you still keen on the bathroom installation? We're getting tight on slots this week — which day works for the site visi ..."*

**Lead `860`** (cancelled) — 2 re-pitches after the customer already agreed to the visit at turn 9

- customer: *"Ok"*
- turn 13 (bot): *"We can definitely sort out two 9-inch doors for your bathroom project. We'll take a look at the space when we come round and include that in the plan. ..."*
- turn 27 (bot): *"That time is for us to come round and have a quick look at the space, so we can give you a fixed quote on the spot. It takes about 20 minutes and ther ..."*

**Lead `872`** (pending) — 1 re-pitches after the customer signalled they wanted out at turn 33

- customer: *"Thank you for your help. We will talk another time"*
- turn 40 (cron): *"[AUTO FOLLOW-UP] Hi there, we're getting tight on slots this week. Would tomorrow or later this week work for a quick site visit? The plumber can give ..."*

### 3b. Bot telling the customer their message was invalid

- **lead `56`** ['calls the message uninformative'] — customer: *"Did i ?"* -> bot: *"I don't see any new information in your message. Which service are you interested in? We offer: Bathroom Renovation, New Plumbing Installation, or Kit ..."*
- **lead `62`** ['asks customer to rephrase'] — customer: *"I don't have a place for installation"* -> bot: *"I understand. Could you please clarify what type of property this installation is for - is it a house, apartment, or business?"*
- **lead `89`** ['denies customer message exists'] — customer: *"Did you see this👆"* -> bot: *"I didn't receive any previous message. Which service are you interested in? We offer: Bathroom Renovation, New Plumbing Installation, or Kitchen Renov ..."*
- **lead `141`** ['asks customer to rephrase'] — customer: *"Insuit and the other"* -> bot: *"I'm here to help with your plumbing needs. Could you please clarify which service you're interested in? We offer: Bathroom Renovation, New Plumbing In ..."*

### 3c. Customers who said so out loud

- **lead `62`** ['i said']
  - bot: *"I see you're in Harare Dzivarasekwa extension. Do you have a plan already for your new plumbing installation, or would you like us to do a site visit?"*
  - customer: *"No I said she's a student from kaguvi vocational training center. She needs a company to do her practical lessons with"*
- **lead `89`** ['i said']
  - bot: *"I didn't receive any previous message. Which service are you interested in? We offer: Bathroom Renovation, New Plumbing Installation, or Kitchen Renovation."*
  - customer: *"I said i wil contact you in due course mkufamba kwenguva"*
- **lead `320`** ['i said']
  - bot: *"No problem at all. Your appointment is still confirmed — we'll use this WhatsApp number for updates."*
  - customer: *"As I said earlier on its not a renovation its a new installation"*
- **lead `320`** ['i said']
  - bot: *"No problem at all. Your appointment is still confirmed — we'll use this WhatsApp number for updates."*
  - customer: *"As I said earlier on its not a renovation its a new installation"*
- **lead `411`** ['not answering']
  - bot: *"That looks like a freestanding bathtub. The supply price for a freestanding tub starts from US$400, the mixer from US$150, and installation is US$120. Which area are you  ..."*
  - customer: *"Ndikuita kunge ndikutaura ne chartbot answer my question direct"*
- **lead `872`** ['are you a bot']
  - bot: *"Yes, this is an AI assistant helping with enquiries. We're a real plumbing and construction company based in Harare, and I can pass your details to the team for a proper  ..."*
  - customer: *"Can i have number of a real person please"*

---

Detectors are lexical heuristics over 258 transcripts; counts carry false positives and, for silent human takeover, a known floor. Per-event evidence is in `exports/conversation_audit.json`.
