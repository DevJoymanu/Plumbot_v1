"""
The one home of every customer-facing sentence that is said from more than one
place.

WHY THIS EXISTS: a sentence written out at two call sites drifts. It happened
here repeatedly and was written down each time instead of being prevented:
"five OTHER copies of that sentence remain on untouched paths", two copied
`all_day_phrases` lists "that had already drifted apart", and two copies of
`get_availability_error_message` that told a lead different things about how
much notice we need (now one copy). A copy fix made in
one place and not the others is a misphrasing that ships to half the
conversations. So a sentence the customer reads, once it is needed in a second
place, lives HERE and both places import it.

HOW IT IS ENFORCED: `bot/test_copy_catalog.py`, in the commit gate, fails when
  * a value below is written out as a literal anywhere else under bot/,
  * a NEW customer-facing sentence appears in two places without coming here, or
  * a value here breaks a copy rule: an emoji, dash punctuation, or "the plumber"
    in an English line (speak as WE).

RULES FOR ADDING A LINE
  * The value is the EXACT wording the customer receives today. Moving a
    sentence here is not a chance to reword it; a wording change is its own
    commit, made on purpose (see the plumbot-sales-flow skill first).
  * Name the constant for what the line DOES in the conversation, not for its
    words, so a later rewording does not make the name a lie.
  * Pair a Shona line with its English line (`_SN` suffix) when one exists.
  * The outbound chain (`finalise_outbound`: speak-as-WE, dash stripping, the
    fee and free-visit strippers) still runs on everything. Write lines here
    already clean, so that chain is a net, not the author.

Two English values were stored in the form the chain was already sending
rather than their old source spelling, so what customers receive is unchanged:
STARTING_PRICES_DISCLAIMER ("once the plumber sees the space" at source,
rewritten to "once we see the space" by speak_as_we on every send) and
EMERGENCY_OFFER (a dash at source, sent as a full stop by strip_dashes).
"""

# ── Qualification: asks the flow makes ──────────────────────────────────────

# Asked straight after a booking lands. Every path that books ends on this.
NAME_ASK_AFTER_BOOKING = (
    "One last thing, what name should we put on the booking? "
    "If you'd rather not share it, just say no."
)

# The area ask when the lead said "no" to something else first, so it opens by
# accepting that answer before moving on.
AREA_ASK_AFTER_NO = "All good, what area are you in?"

# The area ask on the media and plan paths, where the lead has just sent
# something and "What area are you in?" reads abrupt.
AREA_ASK_WHEREABOUTS = "Whereabouts are you based?"

# What is the job, when the lead has sent a photo but not said what it is for.
DESCRIBE_THE_JOB = "Could you describe what you'd like done? Just a few words is fine."

# Property-scope tie-down once a job is on the table.
OTHER_WORK_WHILE_THERE = "Any other work around the place you'd want sorted while we're there?"

# Email capture on the delay / portfolio path.
BEST_EMAIL_ASK = "What's the best email for it?"


# ── Scheduling ──────────────────────────────────────────────────────────────

# Closes a this-or-that offer the reply has just laid out.
WHICH_WORKS_BETTER = "Which works better for you?"

# Closes a list of alternative times.
WHICH_TIME_WORKS = "Which time works best for you?"

# The time ask once the day is known and the diary has both standard slots.
TIME_ASK_TWO_SLOTS = "What time works best for you, 9am or 2pm?"

# Offered after a list of slots, so a lead who can do none of them has a way in.
SUGGEST_ANOTHER_SLOT = "Or feel free to suggest a different date and time!"

# The lead named a day we cannot do.
CHOOSE_ANOTHER_DAY = "Could you please choose a different day that works for you?"

# Availability refusals, read by get_availability_error_message (one copy, in
# availability_mixin.py; the drifted model copy was deleted) and by both copies
# of format_availability_response.
TIME_UNAVAILABLE_SHORT = "That time isn't available."
TIME_UNAVAILABLE = "That time isn't available. Please choose a different time."
TIME_UNAVAILABLE_SUGGEST = "That time isn't available. Please suggest another time."
SLOT_UNAVAILABLE = "That time slot isn't available. Please choose a different time."
TIME_IN_THE_PAST = "That time has already passed. Please choose a future time."
# The slot holds someone else's booking. Deliberately says nothing about that
# booking: the old line named the other customer and their time.
TIME_ALREADY_BOOKED = "That time is already booked. Please choose a different time."
TOO_FAR_AHEAD = "We can only book appointments up to 3 months in advance. Please choose a sooner date."
AVAILABILITY_CHECK_FAILED = (
    "There was a technical issue checking availability. Please try a different time or call us."
)

# Appended (after a space) to a "that time doesn't work" reply, ONLY for a
# tenant with the 24/7 emergency tick, so a refusal becomes an opening.
EMERGENCY_OFFER = "If it's an emergency though, we're on call 24/7. Just say the word."


# ── Pricing ─────────────────────────────────────────────────────────────────

# Follows any starting-price block. Says the price is a starting point without
# re-pitching the visit (no "site visit" wording, by owner rule).
STARTING_PRICES_DISCLAIMER = (
    "These are starting prices. The exact price is confirmed once we see the space."
)
# "kana taona nzvimbo" = "once WE have seen the place". This said "kana
# muplumber aona nzvimbo" (once the plumber has seen it) and went out that way,
# because speak_as_we only rewrites English, so the WE rule has to be met at
# source in Shona. test_catalog_obeys_copy_rules checks every _SN line for it.
STARTING_PRICES_DISCLAIMER_SN = (
    "Aya ndiwo mapurice ekutanga. Mutengo chaiwo unosimbiswa kana taona nzvimbo."
)


# ── Recovery and parking ────────────────────────────────────────────────────

# Something failed on our side mid-turn; ask them to resend rather than guess.
DROPPED_MESSAGE = "Sorry, dropped that on our end. Could you send that again?"

# The portfolio PDF has gone out to a lead who is not ready yet. Light on
# purpose: no narrating the send, no check-back date (we follow up ourselves).
PORTFOLIO_SENT_ACK = "Have a look whenever suits, and if anything changes just send a message."

# The plumber's own line, offered beside the portfolio on a delay and as the
# ghosted lead's second follow-up (handoff brief, owner 2026-09-21). Said as
# WE: "message us directly", never "message the plumber on his number". The
# conditions are spelled out because the quote is online only when the lead
# supplies measurements, photos or a plan; the formal PDF is the other thing
# the direct line can do. `plumber_link.quote_offer` puts the link under it on
# its own line. English only: the Shona delay copy does not carry it yet.
PLUMBER_QUOTE_OFFER = (
    "You can also message us directly for a free online quote. Send "
    "measurements, a few photos or a plan of the space with what you need "
    "done, and we can price it without coming out. We can send you a formal, "
    "itemised PDF of the full quote too."
)

# A lead who is not ready: park them warmly, door open.
WHENEVER_YOURE_READY = (
    "No problem at all! Whenever you're ready, just drop us a message "
    "and we'll pick up right where we left off."
)
