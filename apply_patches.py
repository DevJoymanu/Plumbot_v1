#!/usr/bin/env python3
"""
apply_patches.py
Run this from your project root:  python apply_patches.py

Applies 3 fixes to bot/views.py in-place.
"""

import re, shutil, sys
from pathlib import Path

TARGET = Path("bot/views.py")

if not TARGET.exists():
    sys.exit(f"ERROR: {TARGET} not found. Run from project root.")

src = TARGET.read_text(encoding="utf-8")
original = src

# ── PATCH 1 ─────────────────────────────────────────────────────────────────
# Fix 3 bug: escaped newlines in _get_delay_acknowledgment
OLD1 = "            \"we'll pick up right where we left off.\\\\n\\\\n\"\n"
NEW1 = "            \"we'll pick up right where we left off.\\n\\n\"\n"
if OLD1 in src:
    src = src.replace(OLD1, NEW1, 1)
    print("✅ PATCH 1 applied — _get_delay_acknowledgment newlines fixed")
else:
    print("⚠️  PATCH 1 — pattern not found (may already be fixed, or check spacing)")

# ── PATCH 2 ─────────────────────────────────────────────────────────────────
# Fix 2 bug: escaped newlines in handle_service_inquiry site-visit close
OLD2 = '                    "\\\\n\\\\nWould you like us to come do a *free site visit* and give you "\n'
NEW2 = '                    "\\n\\nWould you like us to come do a *free site visit* and give you "\n'
if OLD2 in src:
    src = src.replace(OLD2, NEW2, 1)
    print("✅ PATCH 2 applied — handle_service_inquiry newlines fixed")
else:
    print("⚠️  PATCH 2 — pattern not found (may already be fixed, or check spacing)")

# ── PATCH 3 ─────────────────────────────────────────────────────────────────
# Fix 6: Inject project_type inference block at the top of get_next_question_to_ask
# Finds the first line of the method body (right after the docstring or def)

FIX6_BLOCK = '''\
        # ── FIX 6: Infer project_type from sent_pricing_intents ──────────────
        # If the bot already answered a specific pricing question (toilet, tub,
        # chamber…) but project_type was never saved, recover it now so we
        # never re-ask "which service?".
        if not self.appointment.project_type:
            _intent_map = {
                "toilet":               "bathroom_renovation",
                "chamber":              "bathroom_renovation",
                "standalone_tub":       "bathroom_renovation",
                "bathtub_installation": "bathroom_renovation",
                "tub_sales":            "bathroom_renovation",
                "shower_cubicle":       "bathroom_renovation",
                "vanity":               "bathroom_renovation",
                "geyser":               "bathroom_renovation",
            }
            _sent = list(getattr(self.appointment, \'sent_pricing_intents\', None) or [])
            for _intent in _sent:
                if _intent in _intent_map:
                    self.appointment.project_type = _intent_map[_intent]
                    self.appointment.save(update_fields=["project_type"])
                    print(
                        f"✅ FIX 6: Inferred project_type=\'{self.appointment.project_type}\'"
                        f" from sent_pricing_intents"
                    )
                    break

'''

# Target: the first "if not self.appointment.project_type:" inside get_next_question_to_ask
# We identify it by finding the method definition and then the first occurrence after it.
METHOD_SIG = '    def get_next_question_to_ask(self):\n'
ANCHOR = '        if not self.appointment.project_type:\n            return "service_type"\n'

method_pos = src.find(METHOD_SIG)
if method_pos == -1:
    print("⚠️  PATCH 3 — get_next_question_to_ask method not found")
else:
    anchor_pos = src.find(ANCHOR, method_pos)
    if anchor_pos == -1:
        print("⚠️  PATCH 3 — anchor pattern not found inside method (Fix 6 may already be applied)")
    else:
        # Check this is really the FIRST occurrence (not a second smart_booking_check copy)
        # by confirming we're within ~100 lines of the method start
        lines_before = src[method_pos:anchor_pos].count('\n')
        if lines_before > 80:
            print("⚠️  PATCH 3 — anchor found but too far from method start; skipping to avoid wrong insertion")
        else:
            src = src[:anchor_pos] + FIX6_BLOCK + src[anchor_pos:]
            print("✅ PATCH 3 applied — Fix 6 project_type inference block inserted")

# ── WRITE OUTPUT ─────────────────────────────────────────────────────────────
if src != original:
    shutil.copy(TARGET, TARGET.with_suffix(".py.bak"))
    TARGET.write_text(src, encoding="utf-8")
    print(f"\n✅ Patched file written to {TARGET}")
    print(f"   Backup saved as {TARGET.with_suffix('.py.bak')}")
else:
    print("\nℹ️  No changes made — all patches were already applied or patterns not found.")