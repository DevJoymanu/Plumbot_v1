"""
Guards for `bot/copy_catalog.py`: one home per customer-facing sentence.

WHY: misphrasing kept shipping because the same sentence lived in several
places and a fix reached only some of them (see the module docstring of
copy_catalog for the cases). These tests make "put it in the catalog" a gate
rather than a habit. They read source with the AST, so a sentence split across
adjacent literals (which Python folds into one constant) is seen whole, and
f-string text is seen too.

What each test catches:
  * test_catalogued_lines_live_only_in_the_catalog - a catalogued sentence
    written out again somewhere else (the drift, starting over).
  * test_no_placeholder_left_uninterpolated - `{copy_catalog.X}` typed into a
    PLAIN string. Adjacent literals fold into one f-string in the AST but each
    segment keeps its own prefix, so a placeholder in an un-prefixed segment is
    sent to the customer as the literal text "{copy_catalog.X}". This happened
    six times while the catalog was being introduced, and nothing else in the
    gate would have noticed.
  * test_catalog_obeys_copy_rules - an emoji, dash punctuation or "the plumber"
    in a catalogued English line. The outbound chain would repair the last two
    on the way out, but copy is written clean at source (CLAUDE.md), so that
    chain stays a net.
  * test_no_new_duplicated_sentences - a NEW customer-looking sentence written
    in two places without going through the catalog. A ratchet: the duplicates
    that already existed are listed with the reason each is tolerated, and the
    test fails if the list gets longer, AND tells you to shorten the list when
    one is cleaned up, so it can only tighten.
"""
import ast
import collections
import glob
import os
import re

from django.test import SimpleTestCase

from bot import copy_catalog

HERE = os.path.dirname(os.path.abspath(__file__))


def _catalog():
    """{NAME: value} for every string constant the catalog defines."""
    return {k: v for k, v in vars(copy_catalog).items()
            if k.isupper() and isinstance(v, str)}


def _source_files():
    """Every non-test, non-migration Python file under bot/, catalog excluded."""
    out = []
    for path in glob.glob(os.path.join(HERE, '**', '*.py'), recursive=True):
        rel = os.path.relpath(path, HERE).replace(os.sep, '/')
        base = os.path.basename(path)
        if (rel.startswith('migrations/') or base.startswith('test')
                or base == 'copy_catalog.py'):
            continue
        out.append((rel, path))
    return sorted(out)


def _string_constants(path):
    """Yield (lineno, value) for every str constant, f-string text included.

    Docstrings are skipped: they describe code, and quoting a sentence in one
    ("the reply ends on 'Which works better for you?'") is documentation, not a
    second copy of the copy.
    """
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, 'body', None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            yield node.lineno, node.value


# The modules that write what a CUSTOMER reads: the conversation engine, the
# pricing copy, the follow-up and reminder crons and the customer emails. The
# duplicate ratchet is scoped to these, because that is where a drifted copy is
# a misphrasing sent to a lead. Staff screens and argparse help repeat
# themselves too, but a drifted admin toast is not what this guards. A new
# module that writes customer copy goes on this list.
CUSTOMER_COPY_MODULES = (
    'views/plumbot/*.py',
    'whatsapp_webhook.py', 'out_of_scope_handler.py', 'controller.py',
    'controller_templates.py', 'faq.py', 'models.py', 'availability_ask.py',
    'visit_slots.py', 'materials_list.py', 'lead_handoff.py', 'plumber_link.py',
    'job_date_ladder.py', 'tenant_config.py',
    'pricing_copy.py', 'customer_emails.py', 'post_visit.py', 'plan_quote.py',
    'visit_proposal.py', 'response_check.py',
    'management/commands/send_followups.py', 'management/commands/send_reminders.py',
    'management/commands/send_job_reminders.py',
)


def _customer_copy_files():
    wanted = set()
    for pattern in CUSTOMER_COPY_MODULES:
        wanted.update(os.path.relpath(p, HERE).replace(os.sep, '/')
                      for p in glob.glob(os.path.join(HERE, pattern)))
    return [(rel, path) for rel, path in _source_files() if rel in wanted]


# The same "does this look like a sentence a customer reads?" test the audit
# that found the duplicates used: prose-shaped, one line, not a log line, a
# regex, SQL or a URL. It deliberately errs towards flagging, because the ratchet
# below is where anything that is not really customer copy gets a reason.
_LOG_MARKS = ('✅', '⚠', '🔎', '❌', '🤖', '📅', '→', '🧪', '🔒')


def _looks_customer_facing(s):
    s = s.strip()
    if len(s) < 25 or len(s) > 260 or ' ' not in s or '\n\n' in s:
        return False
    if re.search(r'[\\^$|]{2}|\(\?|%s|SELECT |http', s) or s[:1] in '[{<#':
        return False
    if re.match(r'^[A-Z_ ]+:', s) or any(m in s for m in _LOG_MARKS):
        return False
    return bool(re.search(r'[a-z]', s)) and bool(re.search(r'[.?!]$', s))


# Sentences already written in more than one place when the catalog arrived,
# each with the reason it is tolerated. Shrink this list; never grow it. A
# genuinely new duplicate belongs in copy_catalog.py instead.
KNOWN_DUPLICATES = {
    # LLM instructions, not customer copy. They never reach a lead, and each
    # prompt owns its own wording.
    "Return ONLY valid JSON. No markdown.":
        "LLM prompt instruction, not customer copy",
    "Return ONLY valid JSON. No markdown, no explanation.":
        "LLM prompt instruction, not customer copy",
    "You are a yes/no classifier. Reply with only the single word 'yes' or 'no'.":
        "LLM prompt instruction, not customer copy",
    "Detect the language of this message. Reply with ONLY 'shona', 'english', or 'mixed'.":
        "LLM prompt instruction, not customer copy",
    # An f-string tail after the interpolated hours, in _closed_day_message and
    # in the reply that offers another day. (Its two siblings went when the
    # duplicated get_availability_error_message was merged into one copy.)
    ". Could you suggest a different day and time?":
        "f-string tail after interpolated hours, two sites",
    "You are a data extraction assistant. Return ONLY valid JSON with no formatting "
    "or explanations. NEVER extract plan_status unless actively asking about it RIGHT NOW.":
        "LLM prompt instruction, not customer copy",
    # The price table in pricing_copy writes one row per product, each ending on
    # the same tail after its own figure. One table in one module, so a rewording
    # is one search in one file; restructuring the table is a separate change.
    "all-in — supply and install.":
        "per-row tail of pricing_copy's price table (one module)",
    "all-in — supply ne install.":
        "per-row tail of pricing_copy's price table (one module)",
    # Homebase seed data, mirrored from migrations into two seed dicts
    # (FAQ facts and profile fields). Data, not a code path.
    "We're in Hatfield, Harare.":
        "Homebase seed data, mirrored from the migrations",
}


def _duplicated_sentences():
    """{sentence: [file:line, ...]} for customer-looking sentences in 2+ places."""
    hits = collections.defaultdict(list)
    for rel, path in _customer_copy_files():
        for lineno, value in _string_constants(path):
            if _looks_customer_facing(value):
                hits[value.strip()].append('{}:{}'.format(rel, lineno))
    return {s: locs for s, locs in hits.items() if len(locs) > 1}


class CopyCatalogTests(SimpleTestCase):

    def test_catalogued_lines_live_only_in_the_catalog(self):
        catalog = _catalog()
        offenders = []
        for rel, path in _source_files():
            for lineno, value in _string_constants(path):
                for name, line in catalog.items():
                    if line in value:
                        offenders.append('{}:{} writes out copy_catalog.{} ({!r})'.format(
                            rel, lineno, name, line[:60]))
        self.assertFalse(
            offenders,
            "Catalogued sentences written out again instead of imported. Use "
            "copy_catalog.NAME (or {copy_catalog.NAME} inside an f-string):\n  "
            + "\n  ".join(offenders))

    def test_no_placeholder_left_uninterpolated(self):
        offenders = []
        for rel, path in _source_files():
            for lineno, value in _string_constants(path):
                if '{copy_catalog.' in value:
                    offenders.append('{}:{} {!r}'.format(rel, lineno, value[:80]))
        self.assertFalse(
            offenders,
            "These strings would send the literal text '{copy_catalog...}' to a "
            "customer: the segment holding the placeholder is missing its f "
            "prefix (adjacent literals each keep their own prefix):\n  "
            + "\n  ".join(offenders))

    def test_catalog_obeys_copy_rules(self):
        from bot.utils import speak_as_we, strip_dashes
        emoji = re.compile('[\U0001F300-\U0001FAFF☀-➿]')
        problems = []
        for name, line in _catalog().items():
            if emoji.search(line):
                problems.append('{}: contains an emoji'.format(name))
            if strip_dashes(line) != line:
                problems.append('{}: dash punctuation (strip_dashes changes it)'.format(name))
            if not name.endswith('_SN') and speak_as_we(line) != line:
                problems.append('{}: not in the WE voice (speak_as_we changes it)'.format(name))
            # speak_as_we only rewrites English, so a Shona line naming the
            # plumber ("muplumber", "plumber wedu") would go out exactly as
            # written. The disclaimer did, until it was reworded to "kana taona".
            if name.endswith('_SN') and 'plumber' in line.lower():
                problems.append('{}: names the plumber; Shona copy speaks as WE too'.format(name))
        self.assertFalse(problems, "\n".join(problems))

    def test_no_new_duplicated_sentences(self):
        dupes = _duplicated_sentences()
        new = {s: l for s, l in dupes.items() if s not in KNOWN_DUPLICATES}
        gone = [s for s in KNOWN_DUPLICATES if s not in dupes]
        self.assertFalse(
            new,
            "Customer-facing sentence(s) now written in more than one place. Move "
            "each into bot/copy_catalog.py and import it from every site:\n"
            + "\n".join('  {!r}\n      {}'.format(s[:100], ', '.join(l))
                        for s, l in new.items()))
        self.assertFalse(
            gone,
            "No longer duplicated; delete from KNOWN_DUPLICATES so the ratchet "
            "tightens:\n  " + "\n  ".join(repr(s) for s in gone))
