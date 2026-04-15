#!/usr/bin/env python3
"""
Apply the availability date response fix to bot/views.py.

Run from the project root:
    python apply_availability_fix.py
"""

import re, sys, shutil
from pathlib import Path

VIEWS_PATH = Path("bot/views.py")

# ── 1. New methods to inject ──────────────────────────────────────────────────

NEW_METHODS = '''
    def _classify_availability_response(self, message: str, offered_days: list) -> dict:
        """
        Use DeepSeek to classify how the customer responded to a date offer.

        Intents:
          accepted_offered  – user chose one of the offered days
          suggested_new_day – user mentioned a completely different day
          rejected_both     – user rejected / is unavailable on both days
          unclear           – cannot determine
        """
        offered_str = (
            ", ".join(self._format_day(d) for d in offered_days)
            if offered_days else "the offered days"
        )

        prompt = f"""You are an intent classifier for a plumbing appointment chatbot.

The bot offered the customer these two days: {offered_str}

Customer replied: "{message}"

Classify into EXACTLY ONE of:
- accepted_offered  : customer accepted or chose one of the two offered days
- suggested_new_day : customer mentioned a different day (e.g. "Tuesday", "Monday", "next week")
- rejected_both     : customer said not available on either day, or rejected/declined both
- unclear           : none of the above is clear

Also extract the day name if mentioned (e.g. "Tuesday", null if none).

Rules:
- "Tues", "tues", "tue", "Tuesday" → suggested_new_day, day_mentioned="Tuesday"
- "not available", "can\'t do either", "neither works", "those don\'t work" → rejected_both
- Picks one of the offered days → accepted_offered
- Vague "ok" with no day mentioned → unclear

Return ONLY valid JSON (no markdown):
{{"intent": "accepted_offered|suggested_new_day|rejected_both|unclear", "day_mentioned": "DayName or null", "confidence": "HIGH|LOW"}}"""

        try:
            response = deepseek_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "Return ONLY valid JSON. No markdown."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
                max_tokens=60,
            )
            raw = response.choices[0].message.content.strip().replace("```json", "").replace("```", "").strip()
            result = json.loads(raw)
            print(f"🤖 Availability intent: {result}")
            return result
        except Exception as e:
            print(f"⚠️ Availability classification failed: {e}")
            return {"intent": "unclear", "day_mentioned": None, "confidence": "LOW"}

    def _handle_availability_date_response(self, message: str, retry_count: int):
        """
        Called when next_question == \'availability_date\' AND the bot has already
        offered two days (retry_count > 0).

        Returns a reply string when we should handle it here, or None to fall
        through to the normal retry / first-pass logic.
        """
        days = self._get_next_two_available_days()
        classification = self._classify_availability_response(message, days)
        intent = classification.get("intent", "unclear")
        day_mentioned = classification.get("day_mentioned")
        confidence = classification.get("confidence", "LOW")

        if intent == "rejected_both" and confidence == "HIGH":
            # Clear stored datetime so we don\'t re-offer the same days next turn
            if self.appointment.scheduled_datetime:
                self.appointment.scheduled_datetime = None
                self.appointment.save(update_fields=["scheduled_datetime"])
            self._set_question_retry_count("availability_date", 0)
            return "Oh okay 👍 when are you available? We\'re open Sunday–Friday, 8 AM–6 PM."

        if intent == "suggested_new_day" and day_mentioned and confidence == "HIGH":
            # Confirm the new day without repeating the original options
            return f"Do you mean this coming {day_mentioned}?"

        if (intent == "unclear" or confidence == "LOW") and retry_count >= 2:
            # After two failed attempts, ask open-ended rather than repeating
            return "When would work best for you? We\'re open Sunday–Friday, 8 AM–6 PM."

        # accepted_offered or first-retry unclear → fall through to normal logic
        return None

'''

# ── 2. Old block to find & replace in generate_contextual_response ─────────

OLD_BLOCK = """\
            if retry_count == 0:
                first_pass = self._get_first_pass_question(next_question)
                if first_pass:
                    self._set_question_retry_count(next_question, 1)
                    return first_pass

            new_retry = retry_count + 1"""

NEW_BLOCK = """\
            if retry_count == 0:
                first_pass = self._get_first_pass_question(next_question)
                if first_pass:
                    self._set_question_retry_count(next_question, 1)
                    return first_pass
            else:
                # retry_count >= 1: bot already offered dates once.
                # Classify the customer's reply before repeating the same options.
                if next_question == "availability_date":
                    handled = self._handle_availability_date_response(
                        incoming_message, retry_count
                    )
                    if handled is not None:
                        self._set_question_retry_count(next_question, retry_count + 1)
                        return handled

            new_retry = retry_count + 1"""

# ── 3. Anchor for injecting new methods ───────────────────────────────────────

INJECT_BEFORE = "    def _get_first_pass_question(self, next_question: str) -> str:"


def apply():
    if not VIEWS_PATH.exists():
        print(f"ERROR: {VIEWS_PATH} not found. Run from the project root.")
        sys.exit(1)

    src = VIEWS_PATH.read_text(encoding="utf-8")

    # Guard against double-patching
    if "_classify_availability_response" in src:
        print("Patch already applied — nothing to do.")
        return

    # Step A: inject new methods
    if INJECT_BEFORE not in src:
        print(f"ERROR: anchor not found:\n  {INJECT_BEFORE}")
        sys.exit(1)

    src = src.replace(INJECT_BEFORE, NEW_METHODS + INJECT_BEFORE)
    print("✅ New methods injected.")

    # Step B: patch generate_contextual_response
    if OLD_BLOCK not in src:
        print("ERROR: target block in generate_contextual_response not found.")
        print("       Manual patch may be required for the else-branch.")
        sys.exit(1)

    src = src.replace(OLD_BLOCK, NEW_BLOCK, 1)
    print("✅ generate_contextual_response patched.")

    # Backup + write
    shutil.copy(VIEWS_PATH, VIEWS_PATH.with_suffix(".py.bak"))
    VIEWS_PATH.write_text(src, encoding="utf-8")
    print(f"✅ {VIEWS_PATH} updated. Backup saved as views.py.bak")


if __name__ == "__main__":
    apply()