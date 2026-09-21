# scenarios/ — whole conversations, replayed

Each `.txt` file is one conversation replayed through the real inbound pipeline. **Every production conversation bug gets a file here**, written from the real transcript, before or with the fix. TEST 0 pins resolvers in isolation; only a replayed conversation catches the right resolver running on the wrong branch, or the wrong copy of a sentence going out.

## Format
```
# Comment block first: WHAT the rule is, WHY (the owner rule or the prod
# transcript, with date and lead), and which code enforces it. This header
# is the explanation of the fix that survives, so write it properly.

> customer message            (one turn; the debounce batches rapid taps, so
                               put two taps on one turn if prod saw them batched)
expect: text                  this turn's reply MUST contain it (case-insensitive)
reject: text                  this turn's reply must NOT contain it
```
Assert on the words that carry the rule (the figure, the question, the phrase that must never appear), not on whole sentences: a scenario that pins incidental wording fails on every harmless rewrite and gets ignored.

## How it runs
- **Commit gate (offline):** `bot/test_scenarios.py` replays every file against the DeepSeek stub and fails when a check that passes in `offline_baseline.json` stops passing. Runs in the pre-commit hook and in CI via `python manage.py test bot`.
- **Live, by hand, ONLY WITH THE OWNER'S PERMISSION EACH TIME:** `PLUMBOT_SCENARIO_LIVE=1 python manage.py test bot.test_scenarios` replays every file against real DeepSeek and requires EVERY check (the ones only a live model can pass included). One scenario: `python manage.py run_scenarios scenarios/<file>.txt -t`. Both are paid API calls, and there is deliberately no scheduled live run: ask before running either.

## After adding or changing a scenario
```
PLUMBOT_SCENARIO_BASELINE=write python manage.py test bot.test_scenarios
git diff scenarios/offline_baseline.json
```
New checks enter the baseline as whatever they do offline today. **Read the diff**: an existing check that flipped from `true` to `false` is a regression being recorded instead of fixed. Never rewrite the baseline to get a commit through.

A check can fail offline only because the stub is not faithful enough (see the notes in `tests/deepseek_mock.py`). If a check the stub SHOULD be able to reproduce fails, improve the stub, then re-run `PLUMBOT_GATE=1 python tests/test_bot_responses.py`, because TEST 0 shares it.
