# Homebase Plumbers — Answer Bank

Every question a lead has asked (from prod transcripts pinned in `scenarios/` and
TEST 0) plus every question they plausibly could ask, with the answer.

**Sources (all figures verbatim, nothing invented):**
`bot/tenant_config.py` (`HOMEBASE_FAQ_FACTS`, `HOMEBASE_PROFILE_FIELDS`,
`HOMEBASE_PRICE_ITEMS`), `bot/pricing_copy.py`, `bot/portfolio_catalog.py`,
`bot/sales_profiles/homebase.md`, `bot/out_of_scope_handler.py`, `bot/faq.py`.

**House rules applied to every answer below:** no emojis, WhatsApp-length, mirror
the lead's language (English/Shona), quote "from" prices and defer the exact
figure to the free on-site look, never volunteer a price that wasn't asked for.

Answers marked **[GAP]** are *not* in Homebase's data — they need Takudzwa's
sign-off before the bot says them. See §14.

---

## 1. Business basics

**Where are you based? / What's your address? / Which area are you in?**
> We're in Hatfield, Harare.

**Do you only work in Harare? / Do you come to <place>? / We're not in Harare**
> We travel — we cover Harare and out into the rest of the country, so where you
> are is usually not a problem. Whereabouts are you?

Serviceable: everywhere in Zimbabwe *except* the declined list — Gweru,
Bulawayo, Mutare, Masvingo, Victoria Falls (Vic Falls), Hwange, Beitbridge,
Plumtree. Real jobs quoted in Chitungwiza and Hurungwe (Magunje), so do **not**
answer "Harare only".

**Do you come to <declined city>?**
> That one's a bit far out for us to cover properly, so I'd rather be straight
> with you than send someone half-prepared. If the job's ever closer to Harare,
> we'd be glad to help.

**What are your hours? / Are you open on Sunday? / Saturday?**
> We're available Sunday to Friday, 8am–6pm.
> Easy to find a slot that fits you.

Saturday is the closed day. Sunday **is** a working day.

**Can I speak to a real person? / Can I call you? / Takudzwa's number?**
> You can reach Takudzwa directly on +263774819901 if you'd like to chat about
> the job first.

Business WhatsApp line: +263776255077.

**Do you speak Shona?**
Yes — answer in whichever language the lead used. Every price block has a Shona
rendering in `pricing_copy.py` (`sn_total_line`, `sn_breakdown_lines`,
`sn_cheapest_line`).

---

## 2. The free visit and the quote

**Is the quote free? / Do you charge to come out? / How much for an estimate?**
> Yes, the site visit and quote are completely free.
> We come to you, have a look, and give you a fixed price on the spot before any
> work starts.

**Can I get a quote?** (a *quote*, not a price figure)
Route to the free assessment, not the chat price list. "Quote" leans to the
visit; only *how much / price / cost* gets figures in chat
(`_asks_price_figure` vs `_asks_for_quote`).

**Will the price change once you start? / Any hidden costs?**
> The full price is confirmed before anything starts — parts and labour laid out
> clearly, and nothing begins without your go-ahead. No surprises later.

**How long does the visit take?**
> It's a quick look at the space, about twenty minutes. No obligation either way.

Keep the visit casual — a quick 20-minute look, never a formal "site visit"
pitch, and never re-pitch it to someone who already agreed.

**Do I have to be there?**
> Ideally someone who can let us in and point out what you want done. If that's a
> tenant or a family member, that's fine too.

---

## 3. Price sheet (the full set of figures)

All USD, all "from", parts charged separately unless marked all-in.

### Renovations and packages
| Service | Includes | From |
|---|---|---|
| Bathroom renovation | All fixtures + pipework | US$900 |
| Kitchen renovation | Sink, pipes, drainage, connections | US$600 |
| Full bathroom package | Shower cubicle + vanity + toilet + chamber + tub | US$800 |
| Facebook package | Freestanding tub + side chamber | US$800 |

### Individual fittings
| Item | Supply | Install | All-in |
|---|---|---|---|
| Shower cubicle (900×900) | US$130 | US$40 | **US$170** |
| Vanity unit | US$150 | US$30 | **US$180** |
| Toilet seat & cistern | US$50 | US$20 | **US$70** |
| Wall-hung toilet (= chamber install) | US$130 | US$30 | **US$160** |
| Side chamber | US$130 | US$30 | **US$160** |
| Built-in bathtub (incl. corner) | US$80 | US$80 | **US$160** |
| Freestanding tub | tub US$400 + mixer US$150 + install US$120 | — | **US$670** |
| Basin (pedestal / corner) | — | — | **US$70** flat |
| Geyser | US$80 | US$80 | **US$160** |

### Geyser services
| Service | Cost |
|---|---|
| Geyser supply & install | US$160 all-in |
| Full geyser replacement | US$350 all-in |
| Pressure valve replacement | US$25 labour + parts |
| Thermostat replacement | US$30 labour + parts |
| Element replacement | US$40 labour + parts |

### Repairs and maintenance
| Service | Cost |
|---|---|
| Leaking tap | US$15 labour |
| Toilet seat replacement | US$20 supply + US$10 fit |
| Cistern repair | US$20 labour + parts |
| Leaking toilet base | US$25 labour |
| Full toilet replacement | US$60 supply + US$40 install (US$100) |
| Drain unblocking (simple) | US$20 labour |
| Drain unblocking (severe) | US$50 labour |
| High-pressure jetting | US$80 |
| Minor pipe leak repair | US$20 labour |
| Burst pipe repair | US$40 labour |
| Pipe section replacement | US$50 labour |
| Site assessment | FREE |

**Two figures that have burned us in prod — never get these wrong:**
- A **corner tub is a built-in** → from US$160 all-in, *not* the US$670
  freestanding price.
- A **wall-hung / wall-mounted toilet is the chamber install** → from US$160
  all-in (US$130 + US$30), *never* the US$70 toilet-seat price.

---

## 4. Per-service price answers

Pattern for every one: breakdown line(s) → all-in total → the cheaper
install-only option → "exact figure once we've seen the space" → budget tie-down.

**How much for a bathtub? / How much tub?**
> Freestanding tub: Supply US$400 | Mixer US$150 | Install US$120 → from US$670 all-in
> Standard built-in tub: Supply from US$80 | Install from US$80 → from US$160 all-in
> Side chamber (add-on): Supply from US$130 | Install from US$30 → from US$160
>
> Full freestanding setup from US$670 all-in. Standard built-in tubs from US$160 all-in.
> That's approximate until we've seen the space. Does that sit inside the budget you had in mind?

**How much for a shower cubicle?**
> Shower cubicle: Supply from US$130, Install from US$40 → from US$170 all-in.
> Already have the cubicle? Install-only from US$40.

**Vanity?**
> Vanity unit: Supply from US$150, Install from US$30 → from US$180 all-in.
> Already have the unit? Install-only from US$30.

**Toilet?**
> Toilet seat: Supply from US$50, Install from US$20 → from US$70 all-in.
> Already have the toilet? Install-only from US$20.

**Wall-mounted / wall-hung toilet?**
> Wall-hung toilet (concealed chamber system): Supply from US$130, Install from
> US$30 → from US$160 all-in. Already have the unit? Install-only from US$30.

**Side chamber?**
> Side chamber: Supply from US$130, Install from US$30 → from US$160 all-in.

**Geyser install?**
> Geyser: Supply from US$80, Install from US$80 → from US$160 all-in.
> Already have the geyser? Install-only from US$80.

**My geyser isn't heating / leaking / tripping?**
> Thermostat replacement: from US$30 labour + parts
> Element replacement: from US$40 labour + parts
> Pressure valve replacement: from US$25 labour + parts
> Full geyser replacement: from US$350 (supply + install)
>
> Minor repairs start from US$25–30; if it needs replacing outright, full supply
> and install starts from US$350.

**Blocked drain?**
> Simple blockage (sink, basin, shower): Labour from US$20
> Severe blockage (main drain, sewer line): Labour from US$50
> High-pressure jetting (stubborn blockages): from US$80

**Leaking / burst pipe?**
> Minor leak repair (joint, fitting): Labour from US$20
> Burst pipe repair: Labour from US$40
> Pipe section replacement: Labour from US$50
> Leaking tap washer/cartridge: from US$15

**Toilet running / leaking at the base / cracked seat?**
> Cistern repair (filling valve, flush valve): from US$20 labour + parts
> Toilet seat replacement: Supply from US$20, fit from US$10
> Leaking toilet base: Labour from US$25
> Full toilet replacement: Supply from US$60, install from US$40

**Full bathroom / whole house?**
> Bathroom renovation from US$900 with all fixtures and pipework; the full
> bathroom package (shower cubicle, vanity, toilet, chamber and tub) is from
> US$800. The exact figure comes off the free look at the space.

**The Facebook package?**
> The Facebook package is US$800 — freestanding tub and side chamber.

**Two or more items at once ("tub and a vanity")**
Build a combined reply — line per item plus a total — rather than one price
(`_build_combined_price_reply`, `_names_multiple_products`).

**What's your cheapest option?**
Give the entry point for whatever they asked about: built-in tub US$160 all-in,
install-only where they already own the fitting, or a basic sink unblocking at
US$20 labour.

---

## 5. Sizes and specs

**How big are your tubs?** (list all three blocks when no type is named)
> Built-in bathtubs
> - Compact / Standard: 1700 × 700 mm
> - Large / Luxury: 1800 × 800 mm
>
> Free-standing bathtubs
> - Compact: 1440 × 570 mm
> - Standard: 1700 × 700 to 800 mm
> - Large / Luxury: 1800 to 1865 × 800 to 890 mm
>
> Corner bathtubs
> - Compact symmetrical: 1200 × 1200 mm to 1350 × 1350 mm
> - Standard symmetrical: 1500 × 1500 mm
> - Offset corner: 1500 to 1700 × 900 to 1000 mm

**What size are your shower cubicles?**
> The standard cubicle is 900 × 900mm.

**Will it fit my bathroom?**
> Send me the rough measurements, or we'll take them on the free look — then we
> match the size to the space before anything is ordered.

---

## 6. Trust, credentials, guarantee

**Are you licensed / registered / legit?**
> Yes, we're fully licensed and registered.
> (Credentials available on request.)

**How do I know the work will be good?**
> Satisfaction is guaranteed on every job, and I can send you photos of past work
> so you can judge the finish yourself.

**Can I see previous work? / Do you have pictures?**
Yes — send the gallery. Named pieces we can send individually:
Modern Open-Plan Kitchen · Navy Shaker Kitchen · Freestanding Tub & Wall-Hung
Toilet · Gold-Tap Double Vanity · Black Granite Vanity & Designer Tub · Backlit
Guest Toilet · Classic Toilet & Basin Suite · Vintage Clawfoot Tub Bathroom ·
Walk-In Rain Shower · Marble Built-In Bathtub · Marble Bathtub & Black-Tap Vanity.

**How much was the one in that photo?** (quoted photo)
Price the *quoted* item, covering everything visible in it, e.g.
> Here's the full pricing for that piece, covering everything in the photo:
> - freestanding tub from US$670 + wall-hung toilet from US$160; full bathroom
>   renovation from US$900

**Can I get references / talk to a past client?** **[GAP]**

---

## 7. Payment

**How do I pay? / Do you take EcoCash?**
> Cash, EcoCash, and bank transfer — all good.
> You'll get the full price before anything starts, no surprises.

**Do you take ZiG / bond / rands?** **[GAP]** — prices are quoted in USD; the
accepted-currency answer beyond USD isn't set.

**Do you need a deposit?** **[GAP]** — no deposit framing exists in the copy and
`homebase.md` requires owner sign-off before introducing any. Current safe
answer: "The full price is confirmed on the free visit before anything starts,
and we'll go through payment then."

**Can I pay in instalments / on completion?** **[GAP]**

**Do you invoice / issue receipts / are you VAT registered?** **[GAP]**

---

## 8. Timing and scheduling

**How long does the job take?**
> It depends on the scope of work — a small repair can be done in a few hours,
> while a full bathroom renovation typically takes a few days.

**How soon can you come?**
Offer a concrete slot assumptively rather than asking an open question:
> Would tomorrow at 9am work, or is later in the week better?

**Can you come today / it's an emergency, water everywhere** **[GAP]** — no
emergency or same-day policy and no call-out fee is defined. Safe holding line:
"Let me get Takudzwa onto this now — he's on +263774819901 if you need someone
straight away." Then alert the plumber.

**Do you work evenings / weekends?**
The busy-lead reply currently says: "plenty of our clients are at work during the
day, so we also do evenings and weekends." Note this sits slightly outside the
published Sun–Fri 8am–6pm hours — **worth confirming with Takudzwa** (§14).

**Can I move / cancel my appointment?**
> No problem at all, what day suits you better?
Handled by the reschedule flow; confirm the new slot and re-notify the plumber.

**I'll send my house plan / drawings**
> Send it through whenever you're ready and I'll get it in front of Takudzwa.
Plan-upload flow with a nudge if it doesn't arrive.

---

## 9. Scope — what we do and don't do

**What services do you offer? / Do you do X?**
> Yes, we handle all plumbing work — vanities, tubs, geysers, showers, toilets,
> renovations, repairs, new installations, you name it.

Full list used for scope checks: bathroom renovation, kitchen renovation, new
plumbing installation, toilet supply and fitting, geyser installation, shower
cubicle, vanity unit, bathtub installation, pipe repair, drain unblocking.

**Out of scope** — garages/carports, painting, electrical, roofing, tiling,
carpentry/furniture, landscaping/gardening, pest control, security/alarms, air
conditioning, solar panels, boreholes. Answer by reframing to the plumbing side
rather than a flat no:
> That part isn't our line, but if there's plumbing that goes with it — the
> pipework, the drainage, the fittings — that's exactly what we do. What's the
> job?

**Do you supply the materials or do I buy them?**
> Either works. Our prices show supply and install separately, so if you've
> already bought the fitting we just do the install — for example a cubicle
> you've got already is install-only from US$40.

**Do you do the tiling in a bathroom renovation?** **[GAP]** — tiling is on the
out-of-scope keyword list, yet the renovation package and the portfolio photos
clearly involve tiled finishes. Needs a clear owner answer.

**Do you install solar geysers?** **[GAP]** — "solar" is out-of-scope but geysers
are core. Confirm.

**Do you do boreholes / pumps / tanks?** **[GAP]** — borehole is out-of-scope;
booster pumps and tank installs aren't addressed either way.

**Do you sell fittings without installing?** **[GAP]**

---

## 10. Objections and stalls

**"Let me get other quotes / I'm comparing"**
> Smart to compare. I'm sending through some of our past jobs now so you can
> weigh us on quality, not just price.
>
> One tip while you compare — check the others are all-in (parts + labour) and
> guarantee the work; that's usually where cheaper quotes catch people out. Ours
> is fixed before we start, nothing added later.
>
> When are you hoping to get it sorted by?

**"That's expensive / too much"**
> Fair enough. Two things worth weighing: that figure is all-in, parts and
> labour, fixed before we start — and a leak left alone usually costs more in
> water damage and repeat call-outs than the fix does. What budget did you have
> in mind and I'll tell you honestly what we can do inside it?

**"I'll think about it / maybe later"**
> Totally fair — before you do, can I ask: is it the price, the timing, or
> something else that's making you want to sit on it? I'd rather help you weigh
> it up properly than leave you to it.
>
> Either way, we've got a portfolio of past projects plus a more detailed pricing
> guide that's worth a look while you decide. Want me to email it over? Just
> share your email and I'll send it across.

**"I'm busy / at work during the day"**
> Totally understand — plenty of our clients are at work during the day, so we
> also do evenings and weekends. When would suit you best to get this sorted?

**"No one will be home / I need to arrange access / speak to my wife/tenant"**
> No problem at all — just sort the access on your side and we'll work around
> you. When would be a good time for us to come through?
Then set a concrete near-term check-in rather than a long reactivation.

**"I'm travelling / abroad / out of town"**
> No problem at all. Roughly when do you think you'll be back?

**"I'm still building / not plastered yet"**
Treat as an access/readiness deferral: agree, get a rough date, check back then.

**"I just wanted to save your number"**
Soft brush-off — offer the portfolio and pricing guide by email and capture the
address instead of letting the lead go.

**Any stall at all:** if the *current* message contains a real signal — a named
product, an explicit price ask, "this one?" on a quoted photo — that signal wins
over the parked state. The customer's own words override every gate.

---

## 11. Booking-flow questions the lead asks back

**"What do you need from me?"**
> Just the area you're in, roughly what the job is, and a day that suits — that's
> enough to get Takudzwa out to you.

**"Why do you need my name / area?"**
> Only so Takudzwa knows who he's meeting and can plan the trip. Nothing else.

**"Are you a bot?"** **[GAP]** — no approved line exists. Suggested, pending
sign-off: "I'm the assistant that handles bookings for Homebase — Takudzwa is
the plumber and he's on +263774819901 any time you'd rather talk to him."

**"Where did you get my number?"** **[GAP]**

**"Stop messaging me"**
Acknowledge, stop the follow-ups, leave the door open. No re-pitch.

---

## 12. Shona quick reference

| English | Shona line already in the system |
|---|---|
| Tub all-in | "Full freestanding setup kubva US$670. Standard tub kubva US$160 all-in." |
| Geyser | "Geysers dzinotangira paUS$160 all-in — supply ne install." |
| Shower | "Shower cubicles dzinotangira paUS$170 all-in — supply ne install." |
| Vanity | "Vanities dzinotangira paUS$180 all-in — supply ne install." |
| Toilet | "Zvingangoita US$70 yezvinhu zvese pa standard toilet replacement." |
| Chamber | "Zvingangoita US$160 yezvinhu zvese pa standard chamber setup." |
| Drains | "Zvingangoita US$20 kubva pa labour — zvichienderana nekubinya uye nzvimbo." |
| Photo pricing header | "Hezvino mutengo wakazara wechikamu ichi, nezvese zviri mupicture:" |

Acknowledgements recognised as trivial (never treated as a new question):
hongu, kwete, zvakanaka, zvaita, ndatenda, maita basa, mazvita, ndinzwisisa,
ehe, shuwa.

---

## 13. Real prod questions, pinned

| Lead's actual words | Correct answer |
|---|---|
| "How much is the charge for installing a wall mounted toilet system?" | US$160 all-in wall-hung/chamber — **not** US$70 |
| "how much tub" | Full breakdown + budget tie-down |
| "how big are your tubs" | All three measurement blocks |
| "I would like to request a quote for plumbing services" | Free visit + area question, **no** chat prices |
| "Not in Harare but in Hurungwe (Magunje) to be precise." | Serviceable — offer a slot |
| "We are in Chitungwiza" | Serviceable — offer a slot, no price |
| "Most probably during the weekend, l will get in touch." | Park gracefully with a check-back, don't push for a day |
| "Bathroom and kitchen installations." | Ask about the project, no price, no on-site pitch yet |

---

## 14. Open items for Takudzwa

Answers the bot currently cannot give. Each needs a one-line decision:

1. **Deposit** — any deposit required, and how much?
2. **Warranty / guarantee** — "satisfaction guaranteed" is the claim; is there a
   defined workmanship period (30 days? 6 months?) we can state?
3. **Emergency / same-day call-outs** — do we do them, and is there a call-out fee?
4. **Evenings and weekends** — the objection reply promises them but published
   hours are Sun–Fri 8am–6pm. Which is true?
5. **Travel charge outside Harare** — Chitungwiza and Hurungwe were quoted; is
   travel free, or is there a distance surcharge?
6. **Currency** — USD only, or ZiG / bond / rand accepted?
7. **Invoices, receipts, VAT registration** — can we issue them?
8. **Payment timing** — full on completion, staged, instalments?
9. **Tiling** — included in a bathroom renovation or genuinely out of scope?
10. **Solar geysers** — do we install them?
11. **Boreholes, pumps, tanks** — any of these in scope?
12. **Supply-only sales** — will we sell a fitting without installing it?
13. **References** — can a lead be given a past client to call?
14. **"Are you a bot?"** — approved wording.
15. **Insurance / liability cover** — do we carry it, and can we say so?
16. **Tub size discrepancy** — `homebase.md` lists the standard bathtub as
    1500×700; the seeded size block says compact/standard 1700×700. Which is right?

Once answered, add each as a `faq_facts` entry on Homebase's `TenantProfile`
(and a TEST 0 case if it changes routing) rather than hardcoding it in the flow.
