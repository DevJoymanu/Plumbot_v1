"""
THE SCENARIO SUITE, RUN OFFLINE, AS A COMMIT GATE.

WHAT: replays every file in `scenarios/` through the real inbound pipeline with
a mocked DeepSeek, and fails when a check that used to pass stops passing.

WHY IT EXISTS: `tests/test_bot_responses.py` TEST 0 pins RESOLVERS in isolation
("does `_area_from_reply('Bluffhill.')` return Bluffhill?"). Most regressions
this project actually ships are not resolver faults - they are CONVERSATION
faults: the right resolver ran, but the router picked the wrong branch, or the
sentence that went out was the wrong one of five copies of it. Only a replayed
conversation can see that, and the scenario suite was the one harness able to,
while being wired into no gate at all (it needed a live DEEPSEEK_API_KEY, so in
practice it ran approximately never). Every scenario in that directory was
written AFTER the bug it describes shipped, and then guarded nothing.

HOW: `bot.scenario_runner.run_scenario` is the same engine the CLI and the
Scenario Lab use - this module supplies only the offline half (the deterministic
DeepSeek stub from `tests/deepseek_mock.py`) and the pass/fail contract.

THE BASELINE, AND WHY IT IS NOT A LIST OF SKIPS
-----------------------------------------------
A scenario's expectations were written against the LIVE model, so some of them
can only pass when a real DeepSeek writes the reply. Under the stub those checks
fail for a reason that is not a regression. Two ways to handle that: triage all
30 files by hand into offline/live-only, or record what the suite does today and
fail on any CHANGE. This takes the second, because the first has to be redone by
hand every time a scenario is added and silently rots when someone guesses wrong.

`scenarios/offline_baseline.json` maps scenario -> check -> whether it passes
OFFLINE today. The gate fails on:

  * a check that passed and now fails   - the regression this exists to catch
  * a check missing from the run        - a scenario or turn was deleted
  * a scenario that errors

A check that FAILS in the baseline and now PASSES is not an error (the gate
reports it so the baseline can be tightened), and neither is a brand-new check.
So the baseline can only ever get stricter, never looser, without someone
deliberately rewriting it.

Regenerate after deliberately changing behaviour:

    PLUMBOT_SCENARIO_BASELINE=write python manage.py test bot.test_scenarios

and READ THE DIFF before committing it - a baseline flipped from true to false
is a regression being written down rather than fixed.
"""
import glob
import json
import os
import sys

from django.test import TransactionTestCase

BASELINE_PATH = os.path.join('scenarios', 'offline_baseline.json')
WRITE_BASELINE = os.environ.get('PLUMBOT_SCENARIO_BASELINE') == 'write'

# LIVE mode: the same replay against the REAL DeepSeek, run BY HAND ONLY and
# only with the owner's say-so each time: it makes several hundred paid API
# calls (~14 minutes), and the owner has declined scheduled live runs. There is
# deliberately no CI workflow for it. The baseline covers what the stub can
# reproduce, so the checks it records as failing are exactly the ones only a
# real model can pass; this is how those get checked. Live, EVERY expectation
# must hold, because that is what the scenarios were written against. It runs
# on the test database like the offline gate, using the key in .env.
LIVE = os.environ.get('PLUMBOT_SCENARIO_LIVE') == '1'


def _check_key(turn_index, kind, text):
    """Stable identity for one expectation.

    Keyed by TURN INDEX as well as kind+text because the same `reject: US$` can
    appear on several turns of one scenario and they are different assertions -
    keying on the text alone would let a regression on turn 4 hide behind a pass
    on turn 2.
    """
    return "{}|{}|{}".format(turn_index, kind, text)


class OfflineScenarioSuite(TransactionTestCase):
    """One test method, not one per scenario: a single method keeps the stub
    installed once and keeps the report readable as a whole (which scenarios
    moved, together), rather than 30 separate failures with no shared summary.

    TransactionTestCase, NOT TestCase, and this is load-bearing. Several reply
    paths deliver from a daemon thread (`delayed_response`, the photo send),
    and each thread has its own DB connection. TestCase wraps the test in one
    transaction held open by the main thread, so on SQLite every one of those
    threads' writes failed with "database table is locked": the replies they
    carried never reached the transcript, and the suite was blind to every
    thread-delivered message while still reporting green. Autocommit plus the
    runner's per-turn `_settle` means the threads write while the main thread
    holds nothing. (The teardown flush re-runs post_migrate, so the homebase
    seed in bot.apps is back for whichever test runs next.)"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # The stub must be installed before the pipeline makes any LLM call.
        # install() is idempotent and monkeypatches the one shared client, which
        # is the same seam TEST 0 uses - so offline means offline here too.
        if LIVE:
            return
        sys.path.insert(0, os.path.abspath('.'))
        from tests.deepseek_mock import install
        install()

        # The reply threads sleep on purpose: a 3-7s "typing" gap between the
        # two halves of a split message, 0.5-1s between photos. Those gaps are
        # product behaviour and run for test senders too, and with `_settle`
        # waiting out every thread they took this gate from ~7s to ~80s, which
        # is how a commit gate ends up bypassed with --no-verify. So for the
        # offline run only, the webhook module's `time` is swapped for a proxy
        # whose sleep returns at once; everything else on `time` is forwarded.
        # The ORDER of sends is unchanged (the threads still run in sequence),
        # only the waiting goes. Scoped to bot.whatsapp_webhook, restored in
        # tearDownClass, and never applied in LIVE mode.
        import time as _real_time
        from bot import whatsapp_webhook

        class _NoWaitTime:
            def __getattr__(self, name):
                return getattr(_real_time, name)

            @staticmethod
            def sleep(_seconds):
                return None

        cls._real_webhook_time = whatsapp_webhook.time
        whatsapp_webhook.time = _NoWaitTime()

    @classmethod
    def tearDownClass(cls):
        real = getattr(cls, '_real_webhook_time', None)
        if real is not None:
            from bot import whatsapp_webhook
            whatsapp_webhook.time = real
        super().tearDownClass()

    def _run_all(self):
        from bot.models import Tenant
        from bot.scenario_runner import run_scenario

        tenant = Tenant.objects.filter(slug='homebase').first()
        self.assertIsNotNone(
            tenant, "homebase tenant missing - bot.apps seeds it in test mode")

        results = {}
        errors = []
        # What the bot actually said on each turn, kept so a LIVE failure can be
        # read without re-running anything: a failing check alone says what was
        # expected, never what came back instead.
        self.replies = {}
        for path in sorted(glob.glob('scenarios/*.txt')):
            name = os.path.basename(path)
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
            try:
                # media_wait=0: the runner's per-turn `_settle` already joins
                # every reply thread the turn started, so a turn still silent
                # after that is genuinely silent, and polling for 60s on top of
                # it would only make the gate slow.
                result = run_scenario(name, text, tenant=tenant, media_wait=0)
            except Exception as exc:          # noqa: BLE001 - reported, not raised
                errors.append("{}: {}: {}".format(name, type(exc).__name__, exc))
                continue
            checks = {}
            for i, turn in enumerate(result['turns']):
                self.replies[(name, i)] = (turn['message'], ' / '.join(turn['replies']))
                for check in turn['checks']:
                    checks[_check_key(i, check['kind'], check['text'])] = check['ok']
            results[name] = checks
        return results, errors

    def test_scenarios_do_not_regress(self):
        results, errors = self._run_all()

        if LIVE:
            failing = ["{}: {}".format(name, key)
                       for name, checks in sorted(results.items())
                       for key, ok in checks.items() if not ok]
            total = sum(len(c) for c in results.values())
            print("\nLIVE SCENARIO RUN: {}/{} checks pass".format(
                total - len(failing), total))
            problems = []
            if errors:
                problems.append("SCENARIOS THAT ERRORED:\n  " + "\n  ".join(errors))
            if failing:
                detail = []
                for name, checks in sorted(results.items()):
                    for key, ok in checks.items():
                        if ok:
                            continue
                        turn = int(key.split('|', 1)[0])
                        said, got = self.replies.get((name, turn), ('', ''))
                        detail.append("{}: {}\n      customer: {!r}\n      bot:      {!r}".format(
                            name, key, said[:120], got[:300]))
                problems.append("{} CHECK(S) FAIL AGAINST THE LIVE MODEL:\n  ".format(
                    len(failing)) + "\n  ".join(detail))
            self.assertFalse(problems, "\n\n".join(problems))
            return

        if WRITE_BASELINE:
            with open(BASELINE_PATH, 'w', encoding='utf-8') as fh:
                json.dump(results, fh, indent=2, sort_keys=True)
                fh.write('\n')
            passing = sum(1 for c in results.values() for ok in c.values() if ok)
            total = sum(len(c) for c in results.values())
            print("\nBASELINE WRITTEN: {}".format(BASELINE_PATH))
            print("  {} scenarios, {}/{} checks pass offline".format(
                len(results), passing, total))
            if errors:
                print("  ERRORED: " + "; ".join(errors))
            return

        self.assertTrue(
            os.path.exists(BASELINE_PATH),
            "{} is missing - regenerate with PLUMBOT_SCENARIO_BASELINE=write "
            "python manage.py test bot.test_scenarios".format(BASELINE_PATH),
        )
        with open(BASELINE_PATH, encoding='utf-8') as fh:
            baseline = json.load(fh)

        regressions, missing, improvements = [], [], []
        for name, expected_checks in baseline.items():
            if name not in results:
                missing.append("{}: scenario did not run".format(name))
                continue
            got = results[name]
            for key, was_ok in expected_checks.items():
                if key not in got:
                    missing.append("{}: check disappeared -> {}".format(name, key))
                elif was_ok and not got[key]:
                    regressions.append("{}: {}".format(name, key))
                elif not was_ok and got[key]:
                    improvements.append("{}: {}".format(name, key))

        if improvements:
            print("\n{} offline check(s) now PASS that the baseline records as "
                  "failing. Tighten the baseline:".format(len(improvements)))
            for line in improvements[:20]:
                print("  + " + line)
            print("  PLUMBOT_SCENARIO_BASELINE=write "
                  "python manage.py test bot.test_scenarios")

        problems = []
        if errors:
            problems.append("SCENARIOS THAT ERRORED:\n  " + "\n  ".join(errors))
        if regressions:
            problems.append(
                "{} CONVERSATION REGRESSION(S) - these checks passed offline "
                "before this change and now fail:\n  ".format(len(regressions))
                + "\n  ".join(regressions))
        if missing:
            problems.append("CHECKS THAT VANISHED (a scenario or turn was "
                            "removed?):\n  " + "\n  ".join(missing))
        self.assertFalse(problems, "\n\n".join(problems))
