"""
Scenario suite runner (CLI) — replays every conversation scenario through the
EXACT production pipeline with REAL DeepSeek calls, checks each bot reply
against the scenario's expectations, and prints a pass/fail report. The same
engine backs the Scenario Lab web page (`/scenario-lab/`).

Scenario format and engine: see `bot/scenario_runner.py`.

Usage:
    python manage.py run_scenarios                    # everything in scenarios/
    python manage.py run_scenarios scenarios/self_defer.txt
    python manage.py run_scenarios -t                 # show full transcripts

Exit code is non-zero when any expectation fails, so this can back a CI job
(needs a real DEEPSEEK_API_KEY secret there).

Windows note: set PYTHONIOENCODING=utf-8 first — the handlers print emoji.
"""
import glob
import os

from django.core.management.base import BaseCommand

from bot.scenario_runner import run_scenario


class Command(BaseCommand):
    help = ("Replay all conversation scenarios with real DeepSeek and report "
            "pass/fail per expectation.")

    def add_arguments(self, parser):
        parser.add_argument(
            "--tenant", default="homebase",
            help="Tenant slug to run as (default: homebase).",
        )
        parser.add_argument(
            "paths", nargs="*",
            help="Scenario files to run (default: scenarios/*.txt).",
        )
        parser.add_argument(
            "-t", "--transcript", action="store_true",
            help="Print the full transcript for every scenario, not just failures.",
        )

    def handle(self, *args, **opts):
        from bot.models import Tenant
        tenant = Tenant.objects.filter(slug=opts.get('tenant') or 'homebase').first()
        if tenant is None:
            self.stderr.write(f"Unknown tenant slug: {opts.get('tenant')}")
            return
        self.stdout.write(f'Running as tenant: {tenant.slug}')
        paths = opts["paths"] or sorted(glob.glob("scenarios/*.txt"))
        if not paths:
            self.stderr.write("No scenario files found (scenarios/*.txt).")
            return

        total_pass = total_fail = 0
        failed_scenarios = []

        for path in paths:
            name = os.path.basename(path)
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            print(f"\n{'=' * 64}\nSCENARIO: {name}\n{'=' * 64}")
            try:
                result = run_scenario(name, text, tenant=tenant)
            except ValueError as exc:
                self.stderr.write(f"PARSE ERROR: {exc}")
                total_fail += 1
                failed_scenarios.append(name)
                continue

            scenario_failed = result["failed"] > 0
            for turn in result["turns"]:
                for check in turn["checks"]:
                    print(f"  {'PASS' if check['ok'] else 'FAIL'}  "
                          f"{check['kind']}: {check['text']}")
            total_pass += result["passed"]
            total_fail += result["failed"]

            if scenario_failed or opts["transcript"]:
                print("\n--- transcript ---")
                for turn in result["turns"]:
                    print(f"CUSTOMER: {turn['message']}")
                    for r in turn["replies"]:
                        print(f"PLUMBOT:  {r}")
                print("--- end transcript ---")
            if scenario_failed:
                failed_scenarios.append(name)
                for turn in result["turns"]:
                    bad = [c for c in turn["checks"] if not c["ok"]]
                    if bad:
                        print(f"\nFAILED on customer message: {turn['message']!r}")
                        for c in bad:
                            print(f"  {c['kind']}: {c['text']}")
                        print(f"  reply was: {' / '.join(turn['replies'])[:400]!r}")

        print(f"\n{'=' * 64}")
        print(f"SCENARIO SUITE: {total_pass} passed, {total_fail} failed"
              + (f"  — failing: {', '.join(failed_scenarios)}" if failed_scenarios else ""))
        print("=" * 64)
        if total_fail:
            raise SystemExit(1)
