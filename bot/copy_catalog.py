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
#
# Names the plumber and says he handles the quotes (owner, 2026-09-21: the link
# message must say whose WhatsApp it opens). {who} is the tenant's plumber's
# name, else the business name; PLUMBER_QUOTE_OFFER_NAMELESS when neither is
# on file. The line saying where the link goes (LINK_OPENS_*) sits under it,
# then the link. plumber_link.quote_offer assembles it.
PLUMBER_QUOTE_OFFER = (
    "{who} handles our quotes and can give you a free online quote without "
    "coming out. Send measurements, a few photos or a plan of the space with "
    "what you need done, and you'll get a formal, itemised PDF of the full quote."
)
PLUMBER_QUOTE_OFFER_NAMELESS = (
    "You can also get a free online quote without anyone coming out. Send "
    "measurements, a few photos or a plan of the space with what you need "
    "done, and you'll get a formal, itemised PDF of the full quote."
)

# Where the link goes, said just above it in every message that carries it
# (owner, 2026-09-21): straight to the plumber's WhatsApp, with their details
# already typed in. {whose} is "Takudzwa's" or, with no name on file, "our
# quotes". The long per-lead link adds LINK_WHY_LONG_TAIL; the plumber's own
# short link carries his fixed message, not their details, so it gets
# LINK_OPENS_READY instead.
LINK_OPENS_WITH_DETAILS = (
    "The link below takes you straight to {whose} WhatsApp with your details "
    "already typed in, so you just tap it and press send."
)
LINK_OPENS_READY = (
    "The link below takes you straight to {whose} WhatsApp with a message ready "
    "to send."
)
LINK_WHY_LONG_TAIL = "That's also why the link is so long."

# The handoff: the SECOND automatic follow-up of a silence, the same for a
# lead who gave a delay signal and one with all three fields. The owner's own
# words (2026-09-21): the opener, then who handles the quotes. "he'll send"
# became "you'll get": the name is the tenant's plumber and nothing tells us
# which pronoun they use. The link and the plumber's number go at the BOTTOM
# (owner, same day), each on its own line. HANDOFF_QUOTES_HERE is the line for
# a tenant with no plumber or business name on file. Built by
# plumber_link.handoff_message.
HANDOFF_LEAVE_IT_HERE = (
    "I've messaged a couple of times, so I'll leave this here. If you'd still "
    "like a free price, no one needs to come round for it."
)
HANDOFF_WHO_HANDLES_QUOTES = (
    "{who} handles the quotes. Send a few photos, measurements or a rough plan "
    "and what you need done, and you'll get a clear price for your job as a PDF."
)
HANDOFF_QUOTES_HERE = (
    "Quotes are handled on the number below. Send a few photos, measurements or "
    "a rough plan and what you need done, and you'll get a clear price for your "
    "job as a PDF."
)

# A lead who asks what the plumber link is, or is wary of it ("is this a scam?",
# "why is the link so long?", "I'm not clicking that"). Owner, 2026-09-21: say
# what it is and why it is long, then give the plumber's number so they can
# skip the link; plumber_link.link_explanation builds it and the webhook sends
# a contact card after it. {who} is the plumber's or business's name, else
# "the person who handles our quotes". The "long" sentence is left out when
# the link is short.
LINK_WHAT_IT_IS = (
    "Fair question. That link just opens a WhatsApp chat with {who}, who "
    "handles our quotes, with your job details already typed in so you don't "
    "have to explain it all again."
)
LINK_WHY_LONG = (
    "That's the only reason it's so long: the message is written into the link. "
    "Nothing downloads and nothing else opens."
)
LINK_NOTHING_ELSE = "Nothing downloads and nothing else opens."
LINK_OR_MESSAGE_DIRECTLY = (
    "If you'd rather not tap it, you can message {who} directly on {number}."
)
# The same two lines for a tenant with no plumber or business name on file.
LINK_WHAT_IT_IS_NAMELESS = (
    "Fair question. That link just opens a WhatsApp chat with the person who "
    "handles our quotes, with your job details already typed in so you don't "
    "have to explain it all again."
)
LINK_OR_MESSAGE_DIRECTLY_NAMELESS = (
    "If you'd rather not tap it, you can message them directly on {number}."
)

# The last line: the plumber's number, so they can save it or message it
# themselves. WhatsApp makes a +number tappable.
HANDOFF_NUMBER_OF = "{who}'s number: {number}"
HANDOFF_NUMBER = "Number: {number}"

# A deferred lead asks for the portfolio on WhatsApp AGAIN after it went out.
# Answered where they are, not with the next scripted step: it is the PDF in the
# chat already, and it will be there when they can open it (a lead without data
# cannot download it yet). Sent through the model reader so it can fit their
# words and language (owner rule, 2026-09-21: context over the script).
PORTFOLIO_ALREADY_HERE = (
    "It's the PDF we sent just above in this chat, so it's there whenever "
    "you're ready to open it. If it didn't come through, let us know."
)

# A lead who is not ready: park them warmly, door open.
WHENEVER_YOURE_READY = (
    "No problem at all! Whenever you're ready, just drop us a message "
    "and we'll pick up right where we left off."
)
