"""
bot/job_text.py
===============
Does this text describe a job, or is it chat?

WHAT: the one vocabulary of words that carry no content (acknowledgements,
reactions, pronouns, time talk) and ``describes_a_job(text)``, which is True
only when the text has at least one word outside it.

WHY: a lead's chat reply kept being saved as the job. "Ok thank you" was
stored as a project description (prod, 2026-09-18), and then "Ok\\nNow you are
talking", two taps reacting to our price breakdown, was stored as lead 1217's
job (barmak, 2026-09-20). It was then read back into the plumber-link message
as "I'm interested in ... Ok Now you are talking". Once ANY string sits on
``project_description`` the flow treats the job as known and never asks
again, so one bad write loses the real description for good. Lead 1217 had
described the real job an hour earlier.

HOW: ``describes_a_job`` tokenises the text and asks for one content word. It
is read at three places, so the rule cannot be missed by any of the paths
that write the field (the extractor, raw-message fallbacks, the webhook's
early capture, the dashboard):
  * ``ResponseMixin._looks_like_project_description_reply`` (the gate on the
    raw-message fallback),
  * ``Appointment.save``, the net: a description with no content is never
    stored, whoever set it,
  * ``plumber_link`` (the pre-filled message never quotes chat).
The area resolver reads the first three sets through ``ResponseMixin``
(``_AREA_*``), unchanged; the reaction words are for the job test only.

Pinned by the "job text" cases in TEST 0 and ``DescriptionNetTests``.
"""

import re

# Answers that are not answers: acknowledgements, yes/no, greetings.
NON_ANSWERS = frozenset({
    'hi', 'hello', 'hey', 'ok', 'okay', 'alright', 'cool', 'sharp',
    'thanks', 'thank', 'noted', 'yes', 'no', 'yep', 'nope', 'sure',
    'hongu', 'kwete', 'ndatenda', 'maita', 'basa',
})

# Time talk. A lead answering the area question with an AVAILABILITY answer
# ("whenever suits you") would otherwise be filed as a suburb.
TEMPORAL_WORDS = frozenset({
    'whenever', 'anytime', 'any', 'time', 'day', 'today', 'tomorrow',
    'tonight', 'morning', 'afternoon', 'evening', 'week', 'weekend',
    'month', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday',
    'saturday', 'sunday', 'asap', 'soon', 'later', 'now', 'am', 'pm',
    'mangwana', 'nhasi', 'manheru', 'mangwanani', 'svondo', 'muvhuro',
    'chipiri', 'chitatu', 'china', 'chishanu', 'mugovera', 'nguva',
    'chero',
})

# Ordinary chat words: pronouns, auxiliaries, the glue of a sentence.
COMMON_WORDS = frozenset({
    'a', 'an', 'the', 'and', 'but', 'or', 'of', 'to', 'for', 'in', 'at',
    'on', 'is', 'are', 'was', 'were', 'be', 'been', 'do', 'does',
    'did', 'can', 'could', 'will', 'would', 'should', 'have', 'has',
    'had', 'i', 'im', 'me', 'my', 'we', 'us', 'our', 'you', 'your',
    'it', 'its', 'they', 'them', 'their', 'he', 'she', 'this', 'that',
    'there', 'here', 'what', 'when', 'where', 'who', 'why', 'how',
    'suits', 'suit', 'works', 'work', 'fine', 'good', 'great', 'nice',
    'please', 'just', 'still', 'want', 'need', 'like', 'think', 'know',
    'get', 'got', 'going', 'go', 'come', 'send', 'call', 'text',
    'message', 'then', 'maybe', 'also', 'much', 'many', 'some', 'side',
    'not', 'dont', 'ill', 'lets', 'let', 'choose', 'chose',
    'pick', 'decide', 'up', 'down', 'over', 'out', 'anything',
    'whatever', 'mind', 'sarudzai', 'imi', 'zvakanaka',
    'really', 'very', 'quite', 'so', 'well', 'else',
})

# Reactions to what WE said: approval, surprise, agreement, laughter. Only the
# job test reads these. "Now you are talking" is praise for our price, not a
# job, and it carried no word the three sets above did not already hold except
# "talking".
REACTION_WORDS = frozenset({
    'talking', 'sounds', 'sound', 'perfect', 'awesome', 'excellent', 'lovely',
    'amazing', 'wow', 'true', 'right', 'agreed', 'agree', 'deal', 'done',
    'correct', 'exactly', 'indeed', 'understood', 'understand', 'see', 'seen',
    'cheers', 'bye', 'yeah', 'yebo', 'kk', 'k', 'lol', 'haha', 'hmm', 'oh',
    'ohh', 'ah', 'thankyou', 'thanx', 'thx', 'ty', 'more', 'it', 'reasonable',
    'fair', 'affordable', 'expensive', 'cheap', 'price', 'prices', 'more',
    'ehe', 'eya', 'aiwa', 'zvaita', 'mazvita', 'tatenda', 'zvakanaka',
    # Contractions, spelled as describes_a_job sees them (apostrophe dropped).
    'youre', 'thats', 'whats', 'theres', 'ive', 'id', 'weve', 'theyre',
    'cant', 'wont', 'isnt', 'its', 'lets', 'ok',
})

# Everything that carries no content, for the job test.
CONTENTLESS_WORDS = NON_ANSWERS | TEMPORAL_WORDS | COMMON_WORDS | REACTION_WORDS

_TOKEN_RE = re.compile(r"[a-z']+")


def describes_a_job(text) -> bool:
    """True when `text` says anything about work: one word that is not chat.

    Apostrophes are dropped first ("you're" is "youre", "don't" is "dont") so
    contractions meet the list the way it is spelled. Empty text is False.
    Deliberately a vocabulary test, not a job-noun list: "the one in the
    corner" and "same as the other bathroom" are descriptions with no plumbing
    word in them, and they pass.
    """
    tokens = [t.replace("'", '') for t in _TOKEN_RE.findall(str(text or '').lower())]
    tokens = [t for t in tokens if t]
    return any(t not in CONTENTLESS_WORDS for t in tokens)


# A room, a fixture or real work, named. Stricter than describes_a_job on
# purpose: "Hi, can you help me?", "I need a plumber" and "Two" all describe
# SOMETHING, but none of them says what the job is.
_JOB_NAMED_RE = re.compile(
    r"\b(?:bath ?rooms?|kitchens?|en-?suites?|toilets?|bath ?tubs?|tubs?|baths?"
    r"|showers?|cubicles?|basins?|sinks?|vanit(?:y|ies)|geysers?|taps?|pipes?"
    r"|drains?|chambers?|cisterns?|mixers?|tiles?|tiling|boreholes?|tanks?"
    r"|gutters?|reticulation|install\w*|replac\w*|fix\w*|repair\w*|renovat\w*"
    r"|redo\w*|re-do|remodel\w*|leak\w*|blocked|block(?:age|ed)|burst|unblock\w*"
    r"|fitt?(?:ed|ing)|new (?:build|building|house|home|property)"
    r"|build(?:ing)? a house|construction)\b", re.IGNORECASE)


def names_a_job(text) -> bool:
    """True when `text` names the job: a room, a fixture or real work.

    Read right after the opener ("What needs doing, and is it one room or a
    few?"), whose answer is the service type AND the description at once
    (owner, 2026-09-23). A reply that names a job is taken as the description,
    so "Can you tell me a bit more about the project?" goes ONLY when the
    answer names no job. Deterministic and English only; a Shona answer is
    left to the model's extraction as before. Pinned by the "opener answer"
    cases in TEST 0.
    """
    return bool(_JOB_NAMED_RE.search(str(text or '')))
