"""
Builds the Plumbot flow map from `flow_spec.py` and the code itself.

WHAT: reads the hand-written graph in `flow_spec.py` (every step, decision and
message of the sales process and the dashboard operations, plus the lead's
journey stages), resolves each node's code pointers against the real source
with Python's own parser, pulls the EXACT customer copy out of
`bot/copy_catalog.py` and out of the functions the node points at, and renders
three views of the same data:

  * The JOURNEY: about fifteen stages a lead moves through, the level at which
    the business makes decisions. Each stage opens into the bot's moves there.
  * The REPLY LADDER: the reply router as the ranked list it really is ("first
    match wins"), in the order the CODE runs it. The order is read from the
    anchor line numbers, never typed, so a reordered router reorders the
    ladder by itself.
  * The FULL MAP: every node and edge, section by section.

Outputs:
  * docs/flow/FLOW.md - committed. The journey, the ladder and the per-section
    Mermaid diagrams, with function names but NO line numbers, so it changes
    only when the flow or the wording changes and its diff means something.
  * the interactive HTML - NOT committed (it carries line numbers and code
    excerpts, so it would change on nearly every commit). It is built at
    deploy by start.sh and served to superusers at /platform/flow-map/
    (bot/views/flow_map.py), and can be built locally with --html.

WHY THE COPY IS READ FROM CODE: a diagram whose wording is typed by hand is
wrong the first time someone edits a sentence. The follow-up scripts are
f-strings inside functions, which is why this parses the AST instead of
importing modules (importing would also need Django settings and a database).

HOW IT STAYS CURRENT:
  * `.githooks/pre-commit` runs `--md` and stages FLOW.md.
  * CI runs `--check` (FLOW.md current) and `--wording-diff <previous
    checkout>`, which writes every changed customer line to the run summary.
  * `bot/test_flow_map.py` (in `manage.py test bot`) fails when a pointer no
    longer resolves, a copy_catalog sentence is on no node, a router or
    `generate_response` section header has no node, a Railway cron command is
    not on the map, or a journey stage names a node that does not exist.

Output is deterministic: no timestamps, no absolute paths, and f-string
placeholders are built from the AST shape rather than source slices (which
Python 3.9 gets wrong inside multi-line f-strings), so the committer's build
and CI's build are byte-identical.

Usage:
    python docs/flow/build_flow_map.py                  # FLOW.md + local HTML
    python docs/flow/build_flow_map.py --md             # FLOW.md only (hook)
    python docs/flow/build_flow_map.py --html PATH      # HTML only, to PATH
    python docs/flow/build_flow_map.py --check          # exit 1 if FLOW.md stale
    python docs/flow/build_flow_map.py --wording-diff OLD_CHECKOUT_DIR
"""
import ast
import importlib.util
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))
SPEC_PATH = os.path.join(HERE, 'flow_spec.py')
TEMPLATE_PATH = os.path.join(HERE, 'viewer_template.html')
MD_OUT = os.path.join(HERE, 'FLOW.md')
HTML_OUT = os.path.join(HERE, 'plumbot-flow.html')
CATALOG_PATH = 'bot/copy_catalog.py'

# How much source a node's detail panel shows. A 700-line function in full
# would make the page several megabytes and bury the branch the node is about.
EXCERPT_MAX_LINES = 70

# A section header comment inside a function: "# ── FAQ LAYER ───" or
# "# -- STEP 1b: ... ---". Ends an anchored block, and is what the coverage
# check reads to find router steps that have no node.
HEADER_RE = re.compile(r'^\s*#\s*(?:──|--)\s+\S')

# Calls whose string ARGUMENTS are logs, regexes or lookups, never copy.
_NON_COPY_CALLS = {
    'print', 'debug', 'info', 'warning', 'warn', 'error', 'exception',
    'critical', 'compile', 'search', 'match', 'fullmatch', 'findall', 'sub',
    'split', 'finditer', '_add_notes_tag', '_remove_notes_tag', 'write',
    'getenv', 'get', 'startswith', 'endswith', 'strftime', 'strptime',
    'filter', 'exclude', 'append_admin_note', '_append_admin_note', 'note_branch',
    'format_html', 'reverse', 'redirect', 'HttpResponse', 'JsonResponse',
    'add_message', 'success', 'Http404', 'ValueError', 'RuntimeError',
}

# Kinds that can be a rung of the reply ladder. Messages, emails and alerts
# are what a rung LEADS to, never a rung themselves.
_RUNG_KINDS = ('gate', 'decision', 'ai_decision', 'step', 'trigger', 'data', 'ai_write')


# ─── Source index ────────────────────────────────────────────────────────────

class SourceFile:
    """One parsed Python file: its lines, its AST, and every named definition.

    `defs` maps a dotted qualname ("ResponseMixin.generate_response",
    "Command._DELAY_NUDGE_MESSAGES") to the AST node, so a spec pointer can
    name a function, a method, a class, or a module/class-level constant.
    `root` is the checkout the file is read from: the working tree normally,
    an older checkout for --wording-diff.
    """

    def __init__(self, rel, root=ROOT):
        self.rel = rel
        with open(os.path.join(root, rel), encoding='utf-8') as fh:
            self.text = fh.read()
        self.lines = self.text.splitlines()
        self.tree = ast.parse(self.text)
        self.parent = {}
        for node in ast.walk(self.tree):
            for child in ast.iter_child_nodes(node):
                self.parent[child] = node
        self.defs = {}
        self._index(self.tree, [])

    def _index(self, node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                qual = prefix + [child.name]
                self.defs.setdefault('.'.join(qual), child)
                self._index(child, qual)
            elif isinstance(child, (ast.Assign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        self.defs.setdefault('.'.join(prefix + [t.id]), child)
            elif isinstance(child, (ast.If, ast.Try, ast.With)):
                # Defs under a module-level `if`/`try` keep the same prefix.
                self._index(child, prefix)


class Index:
    """Every bot/ source file under `root`, searchable by definition name.

    A pointer may be a bare name ("finalise_outbound") when that name is
    defined in exactly one file, or "path::name" when it is not. Tests and
    migrations are left out: the map describes the product, not its tests.
    """

    def __init__(self, root=ROOT):
        self.root = root
        self.files = {}
        self.by_name = {}
        for dirpath, dirnames, filenames in os.walk(os.path.join(root, 'bot')):
            dirnames[:] = sorted(d for d in dirnames
                                 if d not in ('migrations', '__pycache__'))
            for fn in sorted(filenames):
                if not fn.endswith('.py') or fn.startswith('test'):
                    continue
                rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, '/')
                try:
                    sf = SourceFile(rel, root)
                except SyntaxError:
                    continue
                self.files[rel] = sf
                for qual in sf.defs:
                    last = qual.split('.')[-1]
                    self.by_name.setdefault(last, []).append((rel, qual))
                    if '.' in qual:
                        self.by_name.setdefault(qual, []).append((rel, qual))

    def file(self, rel):
        if rel not in self.files:
            try:
                self.files[rel] = SourceFile(rel, self.root)
            except OSError:
                raise LookupError(f"no file {rel}")
        return self.files[rel]

    def resolve(self, ref):
        """A spec pointer -> (SourceFile, qualname, anchor) or raises LookupError.

        Forms: "name", "Class.name", "bot/x.py::name", any of them followed by
        "#anchor text" to point at the block under the first line inside the
        definition that contains that text.
        """
        anchor = None
        if '#' in ref:
            ref, anchor = ref.split('#', 1)
        if '::' in ref:
            rel, name = ref.split('::', 1)
            sf = self.file(rel)
            hits = [q for q in sf.defs
                    if q == name or q.endswith('.' + name)]
            if not hits:
                raise LookupError(f"{rel} has no definition '{name}'")
            if len(hits) > 1 and name not in hits:
                raise LookupError(f"'{name}' is ambiguous in {rel}: {hits}")
            return sf, (name if name in hits else hits[0]), anchor
        hits = sorted(set(self.by_name.get(ref, [])))
        if not hits:
            raise LookupError(f"no definition named '{ref}' under bot/")
        if len(hits) > 1:
            raise LookupError(f"'{ref}' is defined in several places, "
                              f"use path::name: {hits}")
        rel, qual = hits[0]
        return self.files[rel], qual, anchor


# ─── Locating a pointer and extracting its wording ──────────────────────────

def _span(sf, qual, anchor):
    """(start, end) 1-based inclusive lines a pointer covers.

    Without an anchor: the whole definition. With one: from the first line in
    the definition containing the anchor text. When that line starts a
    statement, the span is that statement; otherwise it runs to the line
    before the next section header at the same or a shallower indent (or the
    definition's end). Raises LookupError when the anchor text is gone, which
    is the signal that the code was restructured under the map.
    """
    node = sf.defs[qual]
    start, end = node.lineno, node.end_lineno
    if getattr(node, 'decorator_list', None):
        start = min([start] + [d.lineno for d in node.decorator_list])
    if not anchor:
        return start, end
    # A section header carrying the anchor wins over any earlier line that
    # merely mentions it: "STEP 1b" first appears in a comment inside the
    # proof step ("returns before the delay handler (STEP 1b)"), and matching
    # that put the delay rung above the proof step on the ladder and pointed
    # the node at the wrong code.
    hits = [i for i in range(start, end + 1) if anchor in sf.lines[i - 1]]
    if not hits:
        raise LookupError(f"anchor '{anchor}' not found in {sf.rel}::{qual}")
    headers = [i for i in hits if HEADER_RE.match(sf.lines[i - 1])]
    a_line = headers[0] if headers else hits[0]
    # An anchor on a statement ("if subtype == 'brush_off':") is that one
    # statement: without this the span ran on to the next header comment and
    # the brush-off node also showed the comparison-shopping reply. An `elif`
    # is part of its `if` in the AST, so the span stops where the elif starts.
    for sub in ast.walk(node):
        if isinstance(sub, ast.stmt) and sub.lineno == a_line \
                and not isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) \
                and sub.end_lineno > a_line:
            s_end = sub.end_lineno
            if isinstance(sub, ast.If) and sub.orelse:
                s_end = sub.orelse[0].lineno - 1
                if sf.lines[s_end - 1].strip() == 'else:':
                    s_end -= 1
            return a_line, s_end
    indent = len(sf.lines[a_line - 1]) - len(sf.lines[a_line - 1].lstrip())
    b_end = end
    for j in range(a_line + 1, end + 1):
        line = sf.lines[j - 1]
        if HEADER_RE.match(line) and len(line) - len(line.lstrip()) <= indent:
            b_end = j - 1
            break
    # Trim trailing blank lines so the excerpt ends on code.
    while b_end > a_line and not sf.lines[b_end - 1].strip():
        b_end -= 1
    return a_line, b_end


def _in_non_copy_call(sf, node):
    """True when a string sits inside a log/regex/lookup call or is a docstring."""
    cur = node
    while cur in sf.parent:
        par = sf.parent[cur]
        # Only an ARGUMENT of a log/regex/lookup call is skipped. The object a
        # call hangs off is still copy: in `{'english': "Sure...", ...}.get(lang)`
        # the sentences sit in the dict that `.get` is called on, and skipping
        # the whole call dropped every language-keyed reply from the map.
        if isinstance(par, ast.Call) and cur is not par.func:
            fn = par.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, 'id', '')
            if name in _NON_COPY_CALLS:
                return True
        if isinstance(par, ast.Expr) and isinstance(cur, ast.Constant):
            # A bare string statement: a docstring or a commented-out block.
            return True
        if isinstance(par, (ast.Compare, ast.Subscript)):
            return True
        if isinstance(par, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Module)):
            return False
        cur = par
    return False


def _placeholder(expr):
    """A readable, version-independent name for an interpolated expression.

    `ast.get_source_segment` is wrong on Python 3.9 inside multi-line and
    implicitly concatenated f-strings (it returned slices like "NA, so write
    the whole message..."), and `ast.unparse` differs between versions. CI
    compares the committer's build with its own, so the placeholder is built
    from the AST shape alone: names, attribute chains, calls and constant
    subscripts are spelled out, anything else is "...".
    """
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return _placeholder(expr.value) + '.' + expr.attr
    if isinstance(expr, ast.Call):
        return _placeholder(expr.func) + '()'
    if isinstance(expr, ast.Subscript):
        key = expr.slice
        if isinstance(key, getattr(ast, 'Index', ())):  # 3.8 wraps the key; gone in 3.14
            key = key.value
        if isinstance(key, ast.Constant):
            return _placeholder(expr.value) + '[' + repr(key.value) + ']'
        return _placeholder(expr.value) + '[...]'
    if isinstance(expr, ast.Constant):
        return repr(expr.value)
    return '...'


def _render_fstring(node):
    """An f-string as the customer reads it, with {placeholders} for the
    interpolated parts ("Hi {name}, ..."). Format specs are dropped."""
    out = []
    for part in node.values:
        if isinstance(part, ast.Constant):
            out.append(str(part.value))
        elif isinstance(part, ast.FormattedValue):
            out.append('{' + _placeholder(part.value) + '}')
    return ''.join(out)


_REGEXY = re.compile(r'\\[bsdwS]|\(\?|\[a-z|\[\^|\|\(|\)\||\\\.')


def _looks_like_copy(text):
    """A sentence a person reads, rather than a key, tag, regex or SQL."""
    t = text.strip()
    if len(t) < 16 or len(t.split()) < 3:
        return False
    if not re.search(r'[A-Za-z]{3}', t):
        return False
    if _REGEXY.search(t):
        return False
    if re.fullmatch(r'\[[A-Z0-9_:\] ]+', t):
        return False
    return True


def extract_copy(sf, qual, anchor):
    """Every customer-readable string in a pointer's span, in source order.

    Returns [{'text', 'line'}]. Strings inside logs, regexes, lookups and
    docstrings are skipped; an f-string keeps its {placeholders}. Duplicates
    within one span are kept once.
    """
    start, end = _span(sf, qual, anchor)
    found, seen = [], set()
    for sub in ast.walk(sf.defs[qual]):
        if isinstance(sub, ast.JoinedStr):
            text = _render_fstring(sub)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            text = sub.value
        else:
            continue
        if isinstance(sf.parent.get(sub), ast.JoinedStr):
            continue
        line = getattr(sub, 'lineno', 0)
        if not (start <= line <= end):
            continue
        if _in_non_copy_call(sf, sub) or not _looks_like_copy(text):
            continue
        key = text.strip()
        if key in seen:
            continue
        seen.add(key)
        found.append({'text': text.strip('\n'), 'line': line})
    found.sort(key=lambda c: c['line'])
    return found


def referenced_constants(sf, qual, anchor):
    """Wording a span SENDS but does not spell out itself.

    Many reply builders return `copy_catalog.NAME` or look a sentence up in a
    module-level table (`_FALLBACK_CLARIFIERS.get(...)`). Without following
    those, a node for such a function would show no wording at all, or a spec
    author would have to list the constants by hand and keep the list in step
    with the code. Returns (catalog_names, module_constant_names), in source
    order, each once.
    """
    start, end = _span(sf, qual, anchor)
    cat, consts, seen = [], [], set()
    for sub in ast.walk(sf.defs[qual]):
        line = getattr(sub, 'lineno', 0)
        if not (start <= line <= end):
            continue
        if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                and sub.value.id == 'copy_catalog' and sub.attr not in seen:
            seen.add(sub.attr)
            cat.append((line, sub.attr))
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load) \
                and re.fullmatch(r'_?[A-Z][A-Z0-9_]+', sub.id) and sub.id not in seen \
                and sub.id in sf.defs:
            seen.add(sub.id)
            consts.append((line, sub.id))
    cat.sort()
    consts.sort()
    return [c for _, c in cat], [c for _, c in consts]


def read_catalog(index):
    """copy_catalog.py -> {NAME: (text, line)} for every UPPER_CASE string."""
    sf = index.file(CATALOG_PATH)
    out = {}
    for node in sf.tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 \
                and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id.isupper():
            try:
                val = ast.literal_eval(node.value)
            except Exception:
                continue
            if isinstance(val, str):
                out[node.targets[0].id] = (val, node.lineno)
    return out


# ─── Building the data ───────────────────────────────────────────────────────

def load_spec():
    spec = importlib.util.spec_from_file_location('plumbot_flow_spec', SPEC_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _resolve_copy(n, index, catalog, errors):
    """A node's `copy` pointers -> [{'source','file','line','texts','via'?}].

    Catalog pointers give their one sentence. Code pointers give every
    sentence in their span, then the catalog lines and module tables that span
    sends by reference, each labelled with the function that sends it. A
    pointer that yields nothing is an error: the wording moved away from it.
    """
    copies = []
    for ref in n.get('copy', []):
        if ref.startswith('catalog:'):
            name = ref.split(':', 1)[1]
            if name not in catalog:
                errors.append(f"node '{n['id']}': copy_catalog has no '{name}'")
                continue
            text, line = catalog[name]
            copies.append({'source': f'copy_catalog.{name}', 'file': CATALOG_PATH,
                           'line': line, 'texts': [text]})
            continue
        try:
            sf, qual, anchor = index.resolve(ref)
            found = extract_copy(sf, qual, anchor)
            cat_refs, const_refs = referenced_constants(sf, qual, anchor)
        except (LookupError, KeyError) as exc:
            errors.append(f"node '{n['id']}': copy '{ref}': {exc}")
            continue
        before = len(copies)
        if found:
            copies.append({'source': qual + (f' ({anchor})' if anchor else ''),
                           'file': sf.rel, 'line': found[0]['line'],
                           'texts': [c['text'] for c in found]})
        listed = {c['source'] for c in copies}
        for name in cat_refs:
            src = f'copy_catalog.{name}'
            if name in catalog and src not in listed:
                text, line = catalog[name]
                copies.append({'source': src, 'file': CATALOG_PATH, 'line': line,
                               'texts': [text], 'via': qual.split('.')[-1]})
                listed.add(src)
        for name in const_refs:
            c_found = extract_copy(sf, name, None)
            if c_found and name not in listed:
                copies.append({'source': name, 'file': sf.rel, 'line': c_found[0]['line'],
                               'texts': [c['text'] for c in c_found],
                               'via': qual.split('.')[-1]})
                listed.add(name)
        if len(copies) == before:
            errors.append(f"node '{n['id']}': copy '{ref}' holds no customer sentence")
    # One node can reach the same sentence twice (listed in the spec AND
    # returned by a function the spec lists): keep the first.
    seen, out = set(), []
    for c in copies:
        if c['source'] not in seen:
            seen.add(c['source'])
            out.append(c)
    return out


def _ladders(spec, index, nodes_out, errors):
    """The reply router, and generate_response inside it, as ranked lists.

    A rung is a node anchored inside one of spec.LADDERS' functions, ordered
    by the line its anchor sits on, so the ladder is in the order the code
    actually runs. One rung per anchor line; a rung kind is preferred over a
    message on the same line (the message is where the rung leads).
    """
    out = []
    by_loc = {}
    for n in nodes_out:
        if n['kind'] not in _RUNG_KINDS:
            continue
        for loc in n['code']:
            if loc['anchor']:
                by_loc.setdefault((loc['file'], loc['name']), []).append((loc['line'], n))
    for lad in spec.LADDERS:
        try:
            sf, qual, _ = index.resolve(lad['function'])
        except LookupError as exc:
            errors.append(f"ladder '{lad['id']}': {exc}")
            continue
        seen_lines, rungs = set(), []
        for line, n in sorted(by_loc.get((sf.rel, qual), []), key=lambda t: t[0]):
            if line in seen_lines or any(r['node'] == n['id'] for r in rungs):
                continue
            seen_lines.add(line)
            rungs.append({'node': n['id']})
        out.append({'id': lad['id'], 'title': lad['title'], 'blurb': lad['blurb'],
                    'function': qual, 'file': sf.rel, 'rungs': rungs})
    return out


def build(root=ROOT, strict=True):
    """Resolve the spec against the code under `root`.

    Returns (data, errors, index, catalog, spec). The test suite reads
    `errors` to say exactly what broke; --wording-diff builds an OLD checkout
    with strict=False, where pointers the old code lacked are simply absent.
    """
    spec = load_spec()
    index = Index(root)
    catalog = read_catalog(index)
    errors = []

    section_ids = [s['id'] for s in spec.SECTIONS]
    node_ids = set()
    for n in spec.NODES:
        if n['id'] in node_ids:
            errors.append(f"duplicate node id '{n['id']}'")
        node_ids.add(n['id'])
        if n['section'] not in section_ids:
            errors.append(f"node '{n['id']}' has unknown section '{n['section']}'")
        if n['kind'] not in spec.KINDS:
            errors.append(f"node '{n['id']}' has unknown kind '{n['kind']}'")

    nodes_out = []
    for n in spec.NODES:
        locs, excerpt = [], None
        for ref in n.get('code', []):
            try:
                sf, qual, anchor = index.resolve(ref)
                a, b = _span(sf, qual, anchor)
            except (LookupError, KeyError) as exc:
                errors.append(f"node '{n['id']}': code '{ref}': {exc}")
                continue
            locs.append({'file': sf.rel, 'name': qual, 'anchor': anchor or '',
                         'line': a, 'end': b})
            if excerpt is None:
                stop = min(b, a + EXCERPT_MAX_LINES - 1)
                excerpt = {'file': sf.rel, 'start': a,
                           'text': '\n'.join(sf.lines[a - 1:stop]),
                           'truncated': stop < b}
        nodes_out.append({
            'id': n['id'], 'kind': n['kind'], 'label': n['label'],
            'section': n['section'], 'note': n.get('note', ''),
            'example': n.get('example', ''),
            'code': locs, 'copy': _resolve_copy(n, index, catalog, errors),
            'excerpt': excerpt,
        })

    edges_out = []
    for e in spec.EDGES:
        for end in (e['from'], e['to']):
            if end not in node_ids:
                errors.append(f"edge {e['from']} -> {e['to']}: unknown node '{end}'")
        edges_out.append({'from': e['from'], 'to': e['to'],
                          'label': e.get('label', ''), 'kind': e.get('kind', 'flow')})

    stage_ids = {s['id'] for s in spec.JOURNEY}
    for s in spec.JOURNEY:
        for nid in s['nodes']:
            if nid not in node_ids:
                errors.append(f"journey stage '{s['id']}' names unknown node '{nid}'")
        for nxt, _label in s.get('next', []):
            if nxt not in stage_ids:
                errors.append(f"journey stage '{s['id']}' leads to unknown stage '{nxt}'")

    data = {
        'title': spec.TITLE,
        'repo': spec.GITHUB_BLOB,
        'localRoot': spec.LOCAL_REPO_ROOT,
        'sections': spec.SECTIONS,
        'kinds': spec.KINDS,
        'journey': spec.JOURNEY,
        'ladders': _ladders(spec, index, nodes_out, errors),
        'nodes': nodes_out,
        'edges': edges_out,
    }
    return data, errors, index, catalog, spec


# ─── Coverage: what makes the map WRONG rather than stale ───────────────────

def coverage_errors(data, index, catalog, spec, root=ROOT):
    """The checks `bot/test_flow_map.py` runs.

    1. Every copy_catalog sentence is on at least one node (a new sentence the
       customer can receive is a new thing the map must show).
    2. Every section header in the reply router and in generate_response has a
       node anchored on it (a new router step is a new rung on the ladder).
    3. Every command a Railway cron runs is on a node (a new cron is a new
       proactive send path).
    """
    errs = []
    used_catalog = {c['source'].split('.', 1)[1]
                    for n in data['nodes'] for c in n['copy']
                    if c['source'].startswith('copy_catalog.')}
    for name in sorted(set(catalog) - used_catalog):
        errs.append(f"copy_catalog.{name} is on no node: add it to a node's copy "
                    f"in docs/flow/flow_spec.py")

    anchors = {}
    for n in data['nodes']:
        for loc in n['code']:
            anchors.setdefault((loc['file'], loc['name']), []).append(loc['anchor'])
    for lad in spec.LADDERS:
        sf, qual, _ = index.resolve(lad['function'])
        node = sf.defs[qual]
        mine = [a for a in anchors.get((sf.rel, qual), []) if a]
        for i in range(node.lineno, node.end_lineno + 1):
            line = sf.lines[i - 1]
            if HEADER_RE.match(line) and len(line) - len(line.lstrip()) <= lad['header_indent']:
                if not any(a in line for a in mine):
                    errs.append(f"{sf.rel}:{i} router section '{line.strip()[:70]}' "
                                f"has no node anchored on it in flow_spec.py")

    try:
        with open(os.path.join(root, 'railway.json'), encoding='utf-8') as fh:
            crons = json.load(fh).get('_cronsReference', [])
    except (OSError, ValueError):
        crons = []
    cron_cmds = set()
    for c in crons:
        m = re.match(r'PLUMBOT_CRON=([\w,]+)', c.get('command', ''))
        if m:
            cron_cmds.update(x for x in m.group(1).split(',') if x)
    mapped = {loc['file'] for n in data['nodes'] for loc in n['code']}
    for cmd in sorted(cron_cmds):
        path = f'bot/management/commands/{cmd}.py'
        if path not in mapped:
            errs.append(f"cron command '{cmd}' ({path}) is on no node")
    return errs


# ─── The wording diff (CI run summary) ───────────────────────────────────────

def wording_inventory(data):
    """{(node label, source): [texts]} for every customer line on the map."""
    inv = {}
    for n in data['nodes']:
        for c in n['copy']:
            inv[(n['label'], c['source'])] = list(c['texts'])
    return inv


def wording_diff(old_root, new_root=ROOT):
    """Markdown listing every customer line added, removed or changed between
    two checkouts, read through the CURRENT spec.

    WHY: commits go straight to main, so nothing reviews a wording change
    before it ships, and the owner's rule is that their copy is never
    "improved" unasked. CI writes this to the run summary on every push, which
    makes every change to what customers read visible in one place. Lines the
    old checkout did not have (a new node) count as added.
    """
    old, _e1, *_ = build(old_root, strict=False)
    new, _e2, *_ = build(new_root, strict=False)
    a, b = wording_inventory(old), wording_inventory(new)
    rows = []
    for key in sorted(set(a) | set(b)):
        before, after = a.get(key, []), b.get(key, [])
        gone = [t for t in before if t not in after]
        came = [t for t in after if t not in before]
        if gone or came:
            rows.append((key, gone, came))
    if not rows:
        return "### Customer wording\n\nNo customer-facing line changed in this push.\n"
    out = [f"### Customer wording changed ({len(rows)} place{'s' if len(rows) != 1 else ''})\n"]
    for (label, source), gone, came in rows:
        out.append(f"**{label}** · `{source}`\n")
        for t in gone:
            out.append("```diff\n" + "\n".join("- " + ln for ln in t.splitlines()) + "\n```")
        for t in came:
            out.append("```diff\n" + "\n".join("+ " + ln for ln in t.splitlines()) + "\n```")
        out.append("")
    return "\n".join(out) + "\n"


# ─── Rendering: FLOW.md ──────────────────────────────────────────────────────

_MERMAID_SHAPES = {
    'trigger': ('(["', '"])'), 'step': ('["', '"]'), 'decision': ('{"', '"}'),
    'ai_decision': ('{{"', '"}}'), 'gate': ('[\\"', '"/]'), 'send': ('[/"', '"/]'),
    'email': ('[\\"', '"\\]'), 'ai_write': ('[["', '"]]'), 'data': ('[("', '")]'),
    'staff': ('[/"', '"\\]'), 'notify': ('>"', '"]'), 'loop': ('(("', '"))'),
    'wait': ('("', '")'), 'end': ('((("', '")))'),
}


def _mq(text):
    return (text.replace('"', '#quot;').replace('\n', ' ')
            .replace('<', '#lt;').replace('>', '#gt;'))


def _cell(text):
    return text.replace('|', '\\|').replace('\n', ' ')


def _slug(title):
    s = title.lower()
    s = re.sub(r'[^\w\s-]', '', s)
    return re.sub(r'\s+', '-', s.strip())


def _first_line(node):
    """The first ENGLISH line a node says, for the ladder's outcome column.

    Many builders list the Shona variant first in the source; the English
    line is the one most readers of the map can check at a glance.
    """
    lines = [' '.join(t.split()) for c in node['copy'] for t in c['texts']]
    english = [t for t in lines
               if re.search(r'\b(the|we|you|your|to|and|for|is|it)\b', t, re.I)]
    t = (english or lines or [''])[0]
    return t if len(t) <= 110 else t[:107].rstrip() + '...'


def _outcomes(data, nid):
    """Where a rung leads when it fires: its non-fall-through edges."""
    by_id = {n['id']: n for n in data['nodes']}
    sec = {s['id']: s['title'] for s in data['sections']}
    here = by_id[nid]['section']
    res = []
    for e in data['edges']:
        if e['from'] != nid or e['kind'] == 'no':
            continue
        t = by_id[e['to']]
        res.append({'label': e['label'], 'to': t,
                    'elsewhere': sec[t['section']] if t['section'] != here else ''})
    return res


def render_md(data):
    """FLOW.md: the journey, the ladders, then the full map by section.

    No line numbers anywhere: they move on almost every commit, and a file
    that changes on every commit is one whose diff nobody reads. Function
    names and anchors are stable, and GitHub links go to the file.
    """
    kinds = data['kinds']
    sec_title = {s['id']: s['title'] for s in data['sections']}
    by_id = {n['id']: n for n in data['nodes']}
    n_copy = sum(len(c['texts']) for n in data['nodes'] for c in n['copy'])
    out = [f"# {data['title']}\n",
           "<!-- GENERATED by docs/flow/build_flow_map.py from docs/flow/flow_spec.py and the code.\n"
           "     Do not edit by hand: edit flow_spec.py (the pre-commit hook rebuilds this file). -->\n",
           "Plumbot as one system, at three levels of detail. Start at the top and go down only as far as "
           "the question you are answering needs.\n",
           "1. **[The lead's journey](#1-the-leads-journey)**: the stages a lead moves through, and what the "
           "bot does at each. The level for business decisions.",
           "2. **[The reply ladder](#2-the-reply-ladder)**: the order the bot checks things in when a message "
           "arrives. The first rung that matches answers; everything below it never runs.",
           "3. **[The full map](#3-the-full-map)**: every step, decision and message, section by section.\n",
           "**The interactive version** (click any step for the exact wording, its code and its neighbours) is "
           "at `/platform/flow-map/` on the dashboard for superusers, built from the deployed code. Locally: "
           "`python docs/flow/build_flow_map.py`, then open `docs/flow/plumbot-flow.html`.\n",
           f"**{len(data['journey'])} stages · {sum(len(l['rungs']) for l in data['ladders'])} ladder rungs · "
           f"{len(data['nodes'])} steps · {len(data['edges'])} links · {n_copy} customer lines**, all read "
           "from the code on every build.\n"]

    # ── 1. Journey ──
    out.append("## 1. The lead's journey\n")
    out.append("The main path runs left to right. The stages underneath are where a lead goes when they "
               "step off it, and where they come back from.\n")
    out.append("```mermaid\nflowchart LR")
    for s in data['journey']:
        shape = ('(["', '"])') if s['lane'] == 'main' else ('["', '"]')
        out.append(f"  {s['id']}{shape[0]}{_mq(s['title'])}{shape[1]}")
        out.append(f"  class {s['id']} {s['lane']}")
    for s in data['journey']:
        for nxt, label in s.get('next', []):
            arrow = '-->' if s['lane'] == 'main' and by_stage(data, nxt)['lane'] == 'main' else '-.->'
            lab = f'|"{_mq(label)}"|' if label else ''
            out.append(f"  {s['id']} {arrow}{lab} {nxt}")
    out.append("  classDef main fill:#c9e6ff,stroke:#006591,color:#0b1c30")
    out.append("  classDef side fill:#eff4ff,stroke:#6e7881,stroke-dasharray:4 3,color:#0b1c30")
    out.append("```\n")
    out.append("| Stage | How we know a lead is here | What the bot does |\n|---|---|---|")
    for s in data['journey']:
        moves = ', '.join(by_id[nid]['label'] for nid in s['nodes'][:8])
        more = f" (+{len(s['nodes']) - 8} more)" if len(s['nodes']) > 8 else ''
        out.append(f"| **{_cell(s['title'])}**<br>{_cell(s['blurb'])} | {_cell(s['marker'])} | "
                   f"{_cell(moves)}{more} |")
    out.append("")

    # ── 2. Ladders ──
    out.append("## 2. The reply ladder\n")
    out.append("Read top to bottom. Each rung is checked in this order, in the order the code runs it (the "
               "order is read from the code, so it cannot drift). The first rung that matches sends its "
               "reply and the turn ends. Moving a rung up means it wins over everything it passes.\n")
    for lad in data['ladders']:
        out.append(f"### {lad['title']}\n")
        out.append(f"{lad['blurb']} (`{lad['function']}` in `{lad['file']}`)\n")
        out.append("| # | When | Then | Kind |\n|---|---|---|---|")
        rung_ids = {r['node'] for r in lad['rungs']}
        for i, r in enumerate(lad['rungs'], 1):
            n = by_id[r['node']]
            when = f"**{_cell(n['label'])}**"
            if n['example']:
                when += f"<br><sub>e.g. \"{_cell(n['example'])}\"</sub>"
            thens = []
            for o in _outcomes(data, n['id']):
                t = o['to']
                # Outcomes only, as in the viewer: handing on to the next rung
                # or to a plain step in the same section is the ladder itself.
                if t['id'] in rung_ids or (not o['elsewhere'] and t['kind'] in
                                           ('step', 'data', 'trigger', 'ai_write', 'wait')):
                    continue
                line = _first_line(t)
                s = (f"{_cell(o['label'])}: " if o['label'] else '') + _cell(t['label'])
                if o['elsewhere']:
                    s += f" <sub>({_cell(o['elsewhere'])})</sub>"
                if line and t['kind'] in ('send', 'email', 'ai_write'):
                    s += f"<br><sub>\"{_cell(line)}\"</sub>"
                thens.append(s)
            out.append(f"| {i} | {when} | {'<br>'.join(thens) or 'carries on'} | "
                       f"{kinds[n['kind']]['label']} |")
        out.append("")

    # ── 3. Full map ──
    out.append("## 3. The full map\n")
    out.append("| Shape | Kind | Meaning |\n|---|---|---|")
    for k, v in kinds.items():
        out.append(f"| {v['shape']} | **{v['label']}** | {v['meaning']} |")
    out.append("")
    out.append("Edges: solid = the flow, dotted = the \"no\" / fall-through branch, thick = a loop back or a "
               "hand-off to another section.\n")
    for s in data['sections']:
        out.append(f"- [{s['title']}](#{_slug(s['title'])}): {s['blurb']}")
    out.append("")
    for s in data['sections']:
        sid = s['id']
        mine = [n for n in data['nodes'] if n['section'] == sid]
        ids = {n['id'] for n in mine}
        out.append(f"### {s['title']}\n")
        out.append(s['blurb'] + "\n")
        out.append("```mermaid\nflowchart TD")
        for n in mine:
            o, c = _MERMAID_SHAPES[n['kind']]
            out.append(f"  {n['id']}{o}{_mq(n['label'])}{c}")
            out.append(f"  class {n['id']} k_{n['kind']}")
        ext = set()
        for e in data['edges']:
            a_in, b_in = e['from'] in ids, e['to'] in ids
            if not (a_in or b_in):
                continue
            for other, inside in ((e['from'], a_in), (e['to'], b_in)):
                if not inside and other not in ext:
                    ext.add(other)
                    on = by_id[other]
                    out.append(f"  {other}([\"↗ {_mq(sec_title[on['section']])}: {_mq(on['label'])}\"])")
                    out.append(f"  class {other} ext")
            arrow = {'no': '-.->', 'loop': '==>', 'handoff': '==>'}.get(e['kind'], '-->')
            lab = f"|\"{_mq(e['label'])}\"|" if e['label'] else ''
            out.append(f"  {e['from']} {arrow}{lab} {e['to']}")
        for k, v in kinds.items():
            out.append(f"  classDef k_{k} fill:{v['fill']},stroke:{v['stroke']},color:#14212b")
        out.append("  classDef ext fill:#ffffff,stroke:#8aa0ab,stroke-dasharray:4 3,color:#46606c")
        out.append("```\n")
        out.append("| Step | Kind | Where in the code | Wording |\n|---|---|---|---|")
        for n in mine:
            where = '<br>'.join(
                f"[`{l['file'].split('/')[-1]}`](../../{l['file']}) `{l['name'].split('.')[-1]}`"
                + (f" at \"{_cell(l['anchor'])}\"" if l['anchor'] else '')
                for l in n['code']) or '-'
            words = ', '.join(
                (f"`{c['source'].split('.', 1)[1]}`" if c['source'].startswith('copy_catalog.')
                 else f"{len(c['texts'])} line(s) in `{c['source'].split(' (')[0].split('.')[-1]}`")
                for c in n['copy']) or '-'
            out.append(f"| **{_cell(n['label'])}**<br><sub>`{n['id']}`</sub> | "
                       f"{kinds[n['kind']]['label']} | {where} | {words} |")
        out.append("")
    return '\n'.join(out).rstrip() + '\n'


def by_stage(data, sid):
    return next(s for s in data['journey'] if s['id'] == sid)


# ─── Rendering: the interactive HTML ────────────────────────────────────────

def render_html(data):
    with open(TEMPLATE_PATH, encoding='utf-8') as fh:
        tpl = fh.read()
    payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))
    # A "</script>" inside a copy string would end the data block early.
    payload = payload.replace('</', '<\\/')
    return tpl.replace('/*__FLOW_DATA__*/null', payload)


def build_html():
    """The interactive page as a string, or raises ValueError with the broken
    pointers. Used by bot/views/flow_map.py when no deploy-time build exists."""
    data, errors, *_ = build()
    if errors:
        raise ValueError('; '.join(errors[:5]))
    return render_html(data)


def _write(path, text):
    with open(path, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write(text)


def main(argv):
    if '--wording-diff' in argv:
        old = argv[argv.index('--wording-diff') + 1]
        sys.stdout.write(wording_diff(os.path.abspath(old)))
        return 0
    data, errors, *_ = build()
    if errors:
        print('Flow map has broken pointers (fix docs/flow/flow_spec.py):')
        for e in errors:
            print('  -', e)
        return 1
    md = render_md(data)
    if '--check' in argv:
        try:
            # Line endings are normalised: a Windows checkout with
            # core.autocrlf holds CRLF copies of LF files.
            with open(MD_OUT, encoding='utf-8', newline='') as fh:
                current = fh.read().replace(chr(13) + chr(10), chr(10)) == md
        except OSError:
            current = False
        if not current:
            print('docs/flow/FLOW.md is out of date. Run: python docs/flow/build_flow_map.py --md')
            return 1
        print('Flow map is current.')
        return 0
    if '--html' in argv:
        path = argv[argv.index('--html') + 1]
        _write(path, render_html(data))
        print(f"Flow map HTML -> {path}")
        return 0
    _write(MD_OUT, md)
    written = ['docs/flow/FLOW.md']
    if '--md' not in argv:
        _write(HTML_OUT, render_html(data))
        written.append('docs/flow/plumbot-flow.html')
    n_copy = sum(len(c['texts']) for n in data['nodes'] for c in n['copy'])
    print(f"Flow map built: {len(data['journey'])} stages, "
          f"{sum(len(l['rungs']) for l in data['ladders'])} rungs, {len(data['nodes'])} nodes, "
          f"{n_copy} customer lines -> {', '.join(written)}")
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
