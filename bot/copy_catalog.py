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

# ── First contact: the opener ────────────────────────────────────────────────
# The owner's opener (2026-09-22), replacing "Hello, How may we assist you on
# plumbing services", which a quarter of ad leads never answered. One line,
# the way a person texts: no "thanks for reaching out", no list of what we
# supply, and nothing that assumes they already have a project. It asks what
# needs doing (the job description, from which the job TYPE is worked out)
# and how many rooms, so a lead doing three en-suites or a new build reads it
# as meant for them too. OPENER_INFO answers a lead who asked for info ("Can
# I get more info on this?"), which "sure" answers; a bare "hi" gets
# OPENER_HELLO. {room} is "bathroom" when the ad they came from is about
# bathrooms, else "room" ("imba" in Shona): a kitchen or catalog ad, or no ad
# data. Filled in by response_mixin.build_cold_opener. No price, one question.
OPENER_INFO = "Hi, sure. What needs doing, and is it one {room} or a few?"
OPENER_HELLO = "Hi, what needs doing, and is it one {room} or a few?"
OPENER_INFO_SN = "Mhoro, hongu. Chii chinoda kugadziriswa, uye i{room} imwe here kana dzakawanda?"
OPENER_HELLO_SN = "Mhoro, chii chinoda kugadziriswa, uye i{room} imwe here kana dzakawanda?"

# ── A contact card from the lead (owner, 2026-09-22) ─────────────────────────
# They sent a card AND told us to contact that person: the plumber is emailed
# a call script at once and the lead hears CONTACT_WILL_CALL. A card with no
# instruction gets CONTACT_ASK; a yes to it gets CONTACT_WILL_CALL, a no gets
# CONTACT_KEPT. {name} is the card's first name. bot/shared_contact.py.
CONTACT_WILL_CALL = "Thanks, we'll give {name} a call."
CONTACT_ASK = "Thanks for {name}'s number. Should we give them a call about the job?"
CONTACT_KEPT = "No problem, we've kept {name}'s number on file."

# ── Voice notes (owner, 2026-09-22) ──────────────────────────────────────────
# We cannot play a voice note, so the reply asks them to type it and, when we
# were waiting on an answer, names it so the chat is never a dead end ("We
# were just asking which area you're in."). VOICE_NOTE_CONTEXT is filled with
# a topic from whatsapp_webhook._voice_note_topic; VOICE_NOTE_ASKED quotes our
# last question when no topic fits. One question mark in all.
VOICE_NOTE_ASK = "We can't play voice notes on this line. Could you type it out?"
VOICE_NOTE_CONTEXT = "We were just asking {topic}."
VOICE_NOTE_ASKED = 'We had just asked: "{question}"'

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

# A general price question from a lead who has given the three fields (owner,
# 2026-09-21): the price guide PDF instead of a price block, then the choice.
# PRICE_GUIDE_INTRO goes before the PDF; PRICE_GUIDE_ALREADY_SENT when the PDF
# is already in the chat; PRICE_CHOICE_ASK after it. "Visit or online": a
# visit answer gets the booking question, an online one the plumber handoff.
# bot/price_guide.py assembles and routes them.
PRICE_GUIDE_INTRO = "Here's our price guide, with past jobs and our starting prices."
PRICE_GUIDE_ALREADY_SENT = (
    "Our starting prices are in the price guide we sent just above."
)
PRICE_CHOICE_ASK = (
    "Would you rather get a personalised quote from a quick look at the space, "
    "or a quote online first?"
)

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

# The job-date ladder's EMAILS (owner, 2026-09-22), one per touch. The lead
# named a date more than a week out; the first email goes 7 days before it and
# the second 3 days before. Both say back what the lead told us (they would be
# going ahead around that date), sound like we care rather than chase, and
# offer a free quote: the ONLINE quote from photos, measurements or a plan,
# which is free for every tenant (see PLUMBER_QUOTE_OFFER), never the visit,
# which some tenants charge for. Paragraphs split on a blank line; {hi} is
# "Hi Jane" or "Hi there", {job} their job after "the", {when} the date in
# the owner's form ("the 14th of October"). The WhatsApp touches are
# job_date_ladder.touch_message and are not these. Built by
# job_date_ladder.touch_email, with LADDER_GET_QUOTE_BUTTON first.
LADDER_EMAIL_FIRST = (
    "{hi},\n\n"
    "When we last spoke, you mentioned you'd be going ahead with the {job} "
    "around {when}. That's a week away now, so we wanted to check in and make "
    "sure you have everything you need before then.\n\n"
    "We know a project like this takes some planning, so there's no pressure "
    "at all. If it would help to have a clear price in hand first, you're "
    "welcome to get a free quote from us. Just send a few photos, measurements "
    "or a plan of the space and we'll put it together for you.\n\n"
    "Tap Get quote below whenever you're ready, or reach us on WhatsApp or by "
    "phone. We're happy to help either way."
)
LADDER_EMAIL_SECOND = (
    "{hi},\n\n"
    "You mentioned you'd be going ahead with the {job} around {when}, and "
    "that's only a few days away now, so we wanted to see how your plans are "
    "coming along.\n\n"
    "If the timing has moved, that's completely fine. Just let us know and "
    "we'll work around you. And if you'd still like a price before then, "
    "you're welcome to get a free quote from us from a few photos, "
    "measurements or a plan of the space.\n\n"
    "Are you still keen to go ahead around then, or has the timing moved?"
)
LADDER_GET_QUOTE_BUTTON = "Get quote"

# A lead who says no to email in the delay flow takes the portfolio on
# WhatsApp with the plumber handoff beside it (owner, 2026-09-22): the ack,
# then who handles quotes, what the link opens and why it is long, the link,
# the number, and, ONLY for a job-date lead with no email, the call question.
# Short lines on purpose (owner: "keep the lines short"); each is one idea.
# Built by plumber_link.portfolio_handoff and
# out_of_scope_handler._portfolio_on_whatsapp_ack.
PORTFOLIO_HERE_ACK = "That's fine, we've sent the portfolio here."
# The offer, on Hormozi's value equation (owner, 2026-09-22: "they can get a
# free online quote first"): the outcome (an itemised price), certainty (they
# know the cost before committing), no delay and no effort (nobody comes out,
# a few photos is enough). No time promise and no "fixed": those are one
# tenant's to make, and the copy fence would reject an invented one.
PORTFOLIO_HANDOFF_FREE_FIRST = "You can get a free online quote first. No one needs to come out."
PORTFOLIO_HANDOFF_WHO = (
    "Send {who} a few photos, measurements or a plan, and you'll get an "
    "itemised price as a PDF."
)
PORTFOLIO_HANDOFF_WHO_NAMELESS = (
    "Send a few photos, measurements or a plan to the number below, and you'll "
    "get an itemised price as a PDF."
)
PORTFOLIO_HANDOFF_CERTAINTY = (
    "That way you know exactly what it costs before you commit to anything."
)
PORTFOLIO_LINK_LONG = (
    "This link opens {whose} WhatsApp with your details typed in, which is why "
    "it's long."
)
PORTFOLIO_LINK_DETAILS = "This link opens {whose} WhatsApp with your details typed in."
PORTFOLIO_LINK_READY = "This link opens {whose} WhatsApp with a message ready to send."
# The call is ASKED, never announced (owner: no call without permission), and
# only to a job-date lead with no email; {call_day} is job - 2. The answer is
# read by out_of_scope_handler._handle_call_permission_answer.
LADDER_CALL_ASK = (
    "Would it be okay if we called you on {call_day}, just to see if you've got "
    "the help you need?"
)
# Their answer. A yes confirms the day; a no is respected (no call is set up
# for the plumber) and leaves the door open.
LADDER_CALL_YES = "Perfect, we'll give you a call on {call_day}."
LADDER_CALL_NO = "No problem, we won't call. Just message us here whenever you're ready."

# Said after a price that has no supply/labour split on file (owner rule,
# 2026-09-23: every price shows materials and labour, never only all-in;
# decision B.1: a one-figure item says the figure covers both). Lower-case,
# because it always follows a figure mid-sentence. PriceSplitRuleTests.
MATERIALS_LABOUR_INCLUDED = "materials and labour included"

# A plan lead, an hour after the plumber taps "I've sent the quote" (owner
# decision H1, 2026-09-23): one short line, one question. Speaks as WE ("the
# quote we sent"). Sent by plan_quote._send_quote_check.
PLAN_QUOTE_CHECK = (
    "{hi}, just checking the quote we sent for the {job} came through okay. "
    "Any questions on it?"
)

# The photo or plan ask (owner, 2026-09-23, decisions 7/7a/7b): once the lead
# has said what needs doing, the reply that asks their area first asks for a
# picture of what is there, named from THEIR words, as a request rather than a
# question, so the area stays the one question. Asked once, never chased; a
# lead with nothing just carries on. Chosen and inserted by bot/photo_ask.py.
# English only until the owner approves the Shona drafts.
# Shape (owner, 2026-09-23): open with "You can send us a picture", end
# with "if you have one", then the area question finishes the message.
PHOTO_ASK_EXISTING = "You can send us a picture of {thing} if you have one."
PHOTO_ASK_NEW_SPOT = "You can send us a picture of {spot} where it's going, if you have one."
PHOTO_ASK_NEW_BUILD = "You can send us a plan, drawings or a picture of the site, if you have one."
PHOTO_ASK_UNCLEAR = "You can send us a picture of what's there now, or a plan, if you have one."

# Hesitation about the visit gets the free online quote (owner decision 1B,
# 2026-09-23): live chats lead with the visit; a lead who hesitates ("can't you
# just quote?", "not sure yet" to the slot offer) is offered the online quote
# ONCE, with the plumber's pre-filled link and number under it
# (plumber_link.portfolio_handoff). Built by bot/hesitation.py.
HESITATION_ACK = "No pressure at all on the visit."
ONLINE_QUOTE_STILL_OPEN = (
    "No problem. You can still get a free online quote first: send a few photos, "
    "measurements or a plan to {who} on {number}."
)
ONLINE_QUOTE_STILL_OPEN_NAMELESS = (
    "No problem. You can still get a free online quote first: send a few photos, "
    "measurements or a plan to {number}."
)
# Pushed for an exact figure (owner decisions 6B and D, 2026-09-23): the soft
# line, then the online quote, then the plumber's link and number.
FIRM_PRICE_NO_GUESS = "We'd rather not guess. A quick look and you get a firm price."
FIRM_PRICE_ONLINE = "Or if it's easier, send a few photos for a free online quote first."

# THE availability ask with two real slots (owner wording, 2026-09-23). Sent
# straight after the proof photos once the area is in, and on its own: the
# free-visit line no longer replaces it (ensure_visit_price_note). {offer} is
# "tomorrow 9am or this Thursday 2pm", or "tomorrow 9am or 2pm" on one day.
# Built by ResponseMixin._scripted_availability_ask.
SLOT_ASK_TWO = (
    "What works better for you, {offer}, for us to come through and have a "
    "quick look at the space?"
)
