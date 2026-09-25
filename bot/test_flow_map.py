"""
The flow map (docs/flow/) must describe the code as it is.

WHAT: builds the map from `docs/flow/flow_spec.py` in memory and fails when it
has gone WRONG rather than merely stale:
  * a node points at a function, method, constant or anchor text that no
    longer exists (the code was renamed or restructured under the map),
  * a `bot/copy_catalog.py` sentence is on no node (a new thing a customer can
    receive that the map does not show),
  * a section header in the reply router (`_generate_and_schedule_reply`) or
    in `generate_response` has no node anchored on it (a new router step),
  * a command a Railway cron runs is on no node (a new proactive send path),
  * a journey stage names a node that does not exist,
  * the reply ladder is out of code order (it is read from the anchors, so
    this guards the reader, not a hand-typed list).

WHY HERE: this suite runs in the pre-commit hook and in CI, so an edit that
changes the flow cannot land without the map changing with it. Staleness (the
committed FLOW.md not rebuilt) is NOT checked here, on purpose: a wording
edit in progress would fail the local test run before it is finished. The
hook rebuilds FLOW.md on every commit and CI runs `build_flow_map.py --check`.
The interactive page is not committed at all: it is built at deploy and
served at /platform/flow-map/ (FlowMapViewTests).

HOW TO FIX A FAILURE: edit `docs/flow/flow_spec.py` (its docstring explains
the pointer syntax), then run `python docs/flow/build_flow_map.py`.
"""
import importlib.util
import os

from django.test import SimpleTestCase

_BUILDER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        'docs', 'flow', 'build_flow_map.py')


def _load_builder():
    spec = importlib.util.spec_from_file_location('plumbot_flow_builder', _BUILDER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FlowMapTests(SimpleTestCase):
    """The map resolves against the code and covers every send path."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # One build for every test below: it parses every file under bot/.
        cls.builder = _load_builder()
        cls.data, cls.errors, cls.index, cls.catalog, cls.spec = cls.builder.build()

    def test_every_pointer_resolves(self):
        self.assertEqual(
            self.errors, [],
            "The flow map points at code that no longer exists. Fix "
            "docs/flow/flow_spec.py:\n  " + "\n  ".join(self.errors))

    def test_every_send_path_is_on_the_map(self):
        gaps = self.builder.coverage_errors(self.data, self.index, self.catalog, self.spec)
        self.assertEqual(
            gaps, [],
            "The flow changed and the map did not. Add the node(s) to "
            "docs/flow/flow_spec.py:\n  " + "\n  ".join(gaps))

    def test_customer_copy_is_read_from_the_code(self):
        # The map's reason to exist is exact wording; a node that sends a
        # message and shows none has lost it (a pointer moved off the copy).
        silent = [n['id'] for n in self.data['nodes']
                  if n['kind'] == 'send' and not n['copy'] and not n['note']]
        self.assertEqual(silent, [],
                         f"WhatsApp-message nodes with no wording and no note: {silent}")

    def test_the_ladder_is_in_code_order(self):
        # A rung's position is where its anchor sits in the router. Anchors
        # that matched an earlier COMMENT instead of their own header once put
        # the delay rung above the proof step; headers now win, and this
        # keeps the order honest.
        by_id = {n['id']: n for n in self.data['nodes']}
        for lad in self.data['ladders']:
            lines = []
            for r in lad['rungs']:
                locs = [l['line'] for l in by_id[r['node']]['code']
                        if l['name'] == lad['function'] and l['anchor']]
                lines.append(min(locs))
            self.assertEqual(lines, sorted(lines), lad['id'])
            self.assertGreater(len(lad['rungs']), 10, lad['id'])

    def test_every_journey_stage_has_moves(self):
        empty = [s['id'] for s in self.data['journey'] if not s['nodes']]
        self.assertEqual(empty, [])

    def test_edges_join_real_nodes(self):
        ids = {n['id'] for n in self.data['nodes']}
        dangling = [(e['from'], e['to']) for e in self.data['edges']
                    if e['from'] not in ids or e['to'] not in ids]
        self.assertEqual(dangling, [])
