from django.conf import settings
from django.utils import timezone
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import requests
import pytz
import os
import json
import re
import tempfile
import base64
import logging

from ...models import (
    Appointment, Quotation, QuotationItem,
    QuotationTemplate, QuotationTemplateItem, ConversationMessage,
)
from ...services.clients import (
    deepseek_client, GOOGLE_CALENDAR_CREDENTIALS, DEEPSEEK_API_KEY,
)
from ...utils import (
    _to_decimal, _to_float,
    clean_phone_number, format_phone_number_for_storage,
    _append_admin_note,
)
from ...whatsapp_cloud_api import whatsapp_api

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:
    pass

import logging
logger = logging.getLogger(__name__)


class RescheduleMixin:
        # ── Which appointment are we moving? ──────────────────────────────
        # schedule_job_appointment keeps ONE row: it flips appointment_type and
        # fills job_scheduled_datetime, leaving the completed site visit in
        # scheduled_datetime. So a confirmed lead can hold two datetimes, and
        # every reschedule path has to resolve the live one. It didn't: a job
        # customer was quoted their old site-visit time and their move was
        # written to scheduled_datetime, while the jobs board and
        # send_job_reminders went on reading job_scheduled_datetime.
        def _reschedule_slot(self):
            """(field_name, current_datetime) for the appointment in play."""
            apt = self.appointment
            if apt.appointment_type == 'job_appointment' and apt.job_scheduled_datetime:
                return 'job_scheduled_datetime', apt.job_scheduled_datetime
            return 'scheduled_datetime', apt.scheduled_datetime

        def _slot_display(self, dt):
            """The slot as the customer reads it, in SAST."""
            if not dt:
                return ''
            return self.format_datetime_for_display(dt).strftime('%A, %B %d at %I:%M %p')

        def _reschedule_availability(self, new_datetime):
            """Availability for whichever appointment is being moved — a job
            occupies hours, not a single slot, so it checks its own duration."""
            field, _ = self._reschedule_slot()
            if field == 'scheduled_datetime':
                return self.check_appointment_availability(new_datetime)

            from ..jobs import check_job_availability
            is_free = check_job_availability(
                new_datetime,
                self.appointment.job_duration_hours or 4,
                exclude_appointment_id=self.appointment.id,
                appointment=self.appointment,
            )
            return bool(is_free), (None if is_free else 'conflict')

        # Keyword reschedule signals. Deliberately narrow: this list only runs
        # when DeepSeek is unreachable, and only on an already-confirmed
        # appointment, so a false positive costs a wrong "what day suits you?".
        _RESCHEDULE_KEYWORDS = (
            # English
            'reschedule', 're-schedule', 'rescheduling',
            'change the time', 'change the date', 'change the day',
            'change my appointment', 'change my booking',
            'move it', 'move the appointment', 'move my appointment',
            'move the booking', 'push it', 'push it back', 'postpone',
            'another day', 'another time', 'different day', 'different time',
            "can't make", 'cant make', 'not going to make', 'not gonna make',
            "won't be around", 'wont be around', 'something came up',
            'come later', 'come earlier', 'earlier instead', 'later instead',
            # Shona
            'chinja zuva', 'chinja nguva', 'chinja musi', 'kuchinja zuva',
            'kuchinja nguva', 'rimwe zuva', 'imwe nguva', 'handikwanisi',
            'handisi kuzokwanisa', 'handizokwanisa', 'sundidza',
        )

        def detect_reschedule_request(self, message):
            """Keyword reschedule detection.

            The offline fallback the AI detector reaches for when DeepSeek is
            down. It was called but never defined, so an API blip raised
            AttributeError out of the detector instead of degrading. Kept
            deterministic on purpose: it runs exactly when we cannot make
            another API call.
            """
            _, current = self._reschedule_slot()
            if self.appointment.status != 'confirmed' or not current:
                return False
            text = (message or '').lower()
            return any(keyword in text for keyword in self._RESCHEDULE_KEYWORDS)

        def _reschedule_breakdown_reply(self):
            """Last-resort reply when the reschedule machinery falls over.

            Gives the tenant's OWN number when there is one and no number at all
            when there isn't — the old copy sent every customer of every tenant
            to "(555) PLUMBING", a placeholder that belongs to nobody.
            """
            contact = (self.appointment.plumber_contact() or '').strip()
            if self._lead_language() == 'shona':
                if contact:
                    return ("Ndine urombo, pane dambudziko diki kudivi rangu. Ndinyorerei "
                            f"zuva nenguva yamunoda, kana kufona pa{contact}.")
                return ("Ndine urombo, pane dambudziko diki kudivi rangu. Ndinyorerei zuva "
                        "nenguva yamunoda uye ndichazvigadzirisa.")
            if contact:
                return ("Sorry, something's playing up on my side. Send me the day and time "
                        f"that suit you, or give us a ring on {contact}.")
            return ("Sorry, something's playing up on my side. Send me the day and time that "
                    "suit you and I'll get it moved.")

        def detect_reschedule_request_with_ai(self, message):
            """Use AI to intelligently detect rescheduling requests"""
            try:
                # Only check for reschedule if appointment is already confirmed
                _, current = self._reschedule_slot()
                if self.appointment.status != 'confirmed' or not current:
                    return False

                current_appt = self._slot_display(current)

                detection_prompt = f"""
                You are a rescheduling detection assistant for an appointment system.

                TASK: Determine if the customer's message is requesting to reschedule their existing appointment.

                CONTEXT:
                - Customer has a CONFIRMED appointment: {current_appt}
                - Customer message: "{message}"
                - Phone: {self.phone_number}

                DETECTION CRITERIA:
                Look for ANY indication the customer wants to:
                - Change their appointment time/date
                - Move their appointment to a different slot
                - Cancel and rebook for a different time
                - Express they can't make their current appointment
                - Request a different day or time

                EXAMPLES OF RESCHEDULE REQUESTS:
                - "Can we reschedule to Monday?"
                - "I need to change my appointment"
                - "Something came up, can we move it?"
                - "Can't make it tomorrow, how about Friday?"
                - "I'm busy that day, any other time?"
                - "Emergency came up"
                - "Can we do it earlier/later?"
                - "Different day would be better"
                - "Monday at 2pm instead?"

                EXAMPLES OF NON-RESCHEDULE MESSAGES:
                - "Thanks for confirming"
                - "Looking forward to it"
                - "What should I prepare?"
                - "Do you need directions?"
                - "How much will it cost?"

                RESPONSE FORMAT:
                Reply with ONLY:
                - "YES" if this is clearly a reschedule request
                - "NO" if this is not a reschedule request
                - "MAYBE" if it's ambiguous but could be a reschedule request

                Do not provide explanations, just the single word response.

                CUSTOMER MESSAGE: "{message}"
                """

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": "You are a precise detection assistant. Follow instructions exactly and respond with only YES, NO, or MAYBE."},
                        {"role": "user", "content": detection_prompt}
                    ],
                    temperature=0.1,  # Low temperature for consistency
                    max_tokens=10
                )

                ai_response = response.choices[0].message.content.strip().upper()

                if ai_response in ["YES", "MAYBE"]:
                    print(f"🤖 AI detected reschedule request: {ai_response}")
                    return True
                elif ai_response == "NO":
                    print(f"🤖 AI determined not a reschedule request: {ai_response}")
                    return False
                else:
                    print(f"🤖 AI gave unexpected response: {ai_response}, defaulting to False")
                    return False

            except Exception as e:
                print(f"❌ AI reschedule detection error: {str(e)}")
                # Fallback to keyword detection
                return self.detect_reschedule_request(message)


        def handle_reschedule_request_with_ai(self, message):
            """Use AI to handle the complete rescheduling process"""
            try:
                print(f"🤖 AI processing reschedule request: '{message}'")

                # Whichever appointment is live — the job if there is one
                _, current_appt = self._reschedule_slot()
                current_appt_str = self._slot_display(current_appt)

                # Try to extract new datetime
                new_datetime = self.parse_datetime_with_ai(message)

                if new_datetime:
                    # Check availability
                    is_available, conflict = self._reschedule_availability(new_datetime)

                    if is_available:
                        return self.process_successful_reschedule(current_appt, new_datetime)
                    else:
                        return self.handle_unavailable_reschedule_with_ai(new_datetime, message)
                else:
                    return self.request_reschedule_clarification_with_ai(current_appt_str, message)

            except Exception as e:
                print(f"❌ AI reschedule handling error: {str(e)}")
                return self._reschedule_breakdown_reply()


        def parse_datetime_with_ai(self, message):
            """Use DeepSeek AI to extract datetime from natural language"""
            try:
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                current_time = timezone.now().astimezone(sa_timezone)

                tomorrow_date_str = (current_time + timedelta(days=1)).strftime('%B %d, %Y')
                today_date_str = current_time.strftime('%B %d, %Y')

                # Build next-day lookup for each weekday name
                day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                next_days = {}
                for i, name in enumerate(day_names):
                    days_ahead = (i - current_time.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    next_days[name] = (current_time + timedelta(days=days_ahead)).strftime('%B %d, %Y')

                # This tenant's real week — a hardcoded "Saturday closed" made
                # the model reject days the tenant actually works.
                cfg = self.tenant_cfg
                closed_idx = cfg.closed_weekdays()
                closed_names = cfg.closed_day_names()
                day_lines = "\n".join(
                    f"        - {name.capitalize()}: {next_days[name]}"
                    + (" (CLOSED — do NOT use)" if i in closed_idx else " (open)")
                    for i, name in enumerate(day_names)
                )
                closed_rule = (
                    f'2. {" / ".join(closed_names)} → return CLOSED_DAY (we are closed then)'
                    if closed_names else
                    '2. Every day of the week is a working day — never reject a day'
                )

                datetime_extraction_prompt = f"""You are a datetime extraction assistant for appointment scheduling.

        TASK: Extract a complete date and time from the customer's message and convert it to YYYY-MM-DDTHH:MM format.

        CURRENT CONTEXT:
        - Current datetime: {current_time.strftime('%Y-%m-%d %H:%M')} (Africa/Johannesburg, UTC+2)
        - Business hours: {cfg.open_hour():02d}:00–{cfg.close_hour():02d}:00
        - Working days: {cfg.hours_sentence() or 'every day'}
        - Today is: {today_date_str} ({current_time.strftime('%A')})

        NEXT OCCURRENCE OF EACH DAY:
{day_lines}
        - Tomorrow: {tomorrow_date_str}

        EXTRACTION RULES:
        1. Return a complete datetime ONLY if BOTH date AND time are clearly specified.
        {closed_rule}
        3. A day marked "(open)" above is a valid working day — use its date.
        4. "tomorrow" → {tomorrow_date_str}
        5. "today" → {today_date_str}
        6. Time formats: "2pm"=14:00, "10am"=10:00, "2:30pm"=14:30, "14:00"=14:00
        7. Default minutes to 00 if not specified.
        8. Do NOT adjust timezone — return local Zimbabwe time.

        RESPONSE FORMAT (return ONLY one of these, no other text):
        - Complete datetime: YYYY-MM-DDTHH:MM
        - Closed day requested: CLOSED_DAY
        - Only partial info (missing date OR time): PARTIAL_INFO
        - No datetime found: NOT_FOUND

        CUSTOMER MESSAGE: "{message}"
        EXTRACTED DATETIME:"""

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a precise datetime extraction assistant. Return ONLY the format specified — a datetime string like 2025-11-03T14:00, or one of: CLOSED_DAY, PARTIAL_INFO, NOT_FOUND."
                        },
                        {"role": "user", "content": datetime_extraction_prompt}
                    ],
                    temperature=0.1,
                    max_tokens=30
                )

                ai_response = response.choices[0].message.content.strip()
                print(f"🤖 DeepSeek datetime extraction: '{message}' → {ai_response}")

                if ai_response in ("CLOSED_DAY", "SATURDAY_CLOSED"):
                    print("⚠️ Customer requested a day we're closed")
                    return None  # Caller will handle with alternatives

                if ai_response in ("PARTIAL_INFO", "NOT_FOUND"):
                    return None

                # Parse the returned datetime
                parsed_dt = datetime.strptime(ai_response, '%Y-%m-%dT%H:%M')
                localized_dt = sa_timezone.localize(parsed_dt)
                print(f"✅ Parsed datetime: {localized_dt}")
                return localized_dt

            except ValueError as e:
                print(f"❌ DeepSeek returned invalid datetime format: {ai_response} — {e}")
                return self.parse_datetime(message)  # fallback
            except Exception as e:
                print(f"❌ DeepSeek datetime extraction error: {e}")
                return self.parse_datetime(message)  # fallback


        def handle_unavailable_reschedule_with_ai(self, requested_datetime, original_message):
            """Reply when the slot they asked for is already taken.

            Deterministic: this is a first ask, so it uses the approved script.
            The old LLM version wrote in the "professional" register the copy
            rules ban, and its own except branch read `alternatives` before it
            was assigned (UnboundLocalError) and then quoted hardcoded
            "8 AM - 6 PM, Monday to Friday" hours at every tenant.
            """
            try:
                alternatives = self.get_alternative_time_suggestions(requested_datetime)
            except Exception as e:
                print(f"❌ Alternative slot lookup failed: {str(e)}")
                alternatives = []
            return self._build_reschedule_unavailable_reply(alternatives)

        def _build_reschedule_unavailable_reply(self, alternatives):
            """"That one's taken — here's what is open." Hours come from the
            lead's OWN tenant, never a hardcoded week."""
            from .response_mixin import _hours_clause

            is_shona = self._lead_language() == 'shona'
            options = [
                alt.get('display') for alt in (alternatives or [])
                if isinstance(alt, dict) and alt.get('display')
            ][:3]

            if options:
                listed = "\n".join(f"- {option}" for option in options)
                if is_shona:
                    return (f"Iyoyo nguva yatotorwa. Idzi dziripo:\n{listed}\n\n"
                            "Ndeipi iri nani kwamuri?")
                return (f"That time's already taken. These are open:\n{listed}\n\n"
                        "Which of those works better for you?")

            hours = _hours_clause(self).strip()
            if is_shona:
                return ("Iyoyo nguva yatotorwa. Pane rimwe zuva nenguva zvingakuitirai here?"
                        + (f" {hours}" if hours else ""))
            return ("That time's already taken. What other day and time would suit you?"
                    + (f" {hours}" if hours else ""))

        def request_reschedule_clarification_with_ai(self, current_appt_str, message):
            """Ask for the new day and time.

            Deterministic for the same reason as the unavailable reply: a first
            ask uses the approved script, not an LLM improvising a register.
            """
            return self._build_reschedule_clarification(current_appt_str)

        def _build_reschedule_clarification(self, current_appt_str):
            if self._lead_language() == 'shona':
                return (f"Hapana dambudziko — parizvino makanyoreswa {current_appt_str}. "
                        "Munoda kuti tiendese kupi? Nditumirei zuva nenguva "
                        "(semuenzaniso, 'Muvhuro na2pm').")
            return (f"No problem — you're down for {current_appt_str} at the moment. "
                    "What day and time would suit you better? Something like "
                    "'Monday at 2pm' is perfect.")

        def _build_reschedule_confirmation(self, old_datetime, new_datetime):
            """Confirm the move in plain words: no headings, no emojis, no
            named plumber, mirroring the language the lead wrote in."""
            when = self._slot_display(new_datetime)
            if self._lead_language() == 'shona':
                return (f"Zvakanaka, ndachinja — mava pa{when}. "
                        "Tichakufonerai tisati tasvika. Kana paine chimwe chinochinja, "
                        "ndinyorerei pano.")
            return (f"Done — I've moved you to {when}. "
                    "Someone will call you before we head over. If anything else "
                    "changes, just message me here.")

        def notify_team_about_reschedule(self, old_datetime, new_datetime):
            """Tell the plumber their diary moved.

            This is the one that mattered: the method was CALLED on every
            successful reschedule but never existed anywhere, so the caller's
            except swallowed an AttributeError, the customer was told their new
            time was confirmed, and the plumber drove to the old one.
            """
            from ...test_console import is_test_sender
            if is_test_sender(self.phone_number):
                print("🧪 Test lead — reschedule alert muted")
                return

            apt = self.appointment
            field, _ = self._reschedule_slot()
            kind = 'JOB' if field == 'job_scheduled_datetime' else 'SITE VISIT'
            customer_phone = clean_phone_number(self.phone_number)

            team_message = (
                f"⚠️ {kind} RESCHEDULED\n\n"
                f"Customer: {apt.customer_name or 'Not provided'}\n"
                f"Phone: +{customer_phone}\n"
                f"Area: {apt.customer_area or 'Not provided'}\n"
                f"Work: {apt.job_description or apt.project_type or 'Not specified'}\n\n"
                f"Was: {self._slot_display(old_datetime)}\n"
                f"Now: {self._slot_display(new_datetime)}\n\n"
                f"View: {settings.SITE_URL}/appointments/{apt.id}/"
            )

            # The tenant's own plumber line. Absent means the email alert is the
            # whole notification — never another tenant's number.
            contact = (apt.plumber_contact() or '').replace(
                'whatsapp:', '').replace('+', '').strip()
            if contact:
                try:
                    from ...whatsapp_cloud_api import get_client_for_tenant
                    get_client_for_tenant(apt.tenant).send_text_message(contact, team_message)
                    print(f"✅ Reschedule alert sent to plumber {contact}")
                except Exception as wa_error:
                    print(f"❌ Reschedule WhatsApp alert failed: {str(wa_error)}")
            else:
                print("⚠️ No plumber contact on tenant profile — reschedule alert by email only")

            try:
                from ...plumber_notifications import send_plumber_notification_email
                send_plumber_notification_email(
                    subject=f"Appointment rescheduled — {apt.customer_name or customer_phone}",
                    message=team_message,
                    tenant=getattr(apt, 'tenant', None),
                )
            except Exception as mail_error:
                print(f"❌ Reschedule email alert failed: {str(mail_error)}")

        def process_successful_reschedule(self, old_datetime, new_datetime):
            """Move the appointment, tell everyone who needs to know, confirm it."""
            field, _ = self._reschedule_slot()

            try:
                setattr(self.appointment, field, new_datetime)
                self.appointment.save(update_fields=[field])
                print(f"✅ {field} moved to {new_datetime}")
            except Exception as e:
                # The move itself failed — do NOT tell the customer it's done.
                print(f"❌ Error saving reschedule: {str(e)}")
                return self._reschedule_breakdown_reply()

            # Audit trail. reschedule_count / original_datetime were assigned
            # behind hasattr() guards for columns that do not exist on the
            # model, so every move went unrecorded; the note is what staff
            # actually read on the lead.
            try:
                _append_admin_note(
                    self.appointment,
                    f"Rescheduled by customer: {self._slot_display(old_datetime)} "
                    f"-> {self._slot_display(new_datetime)}",
                )
            except Exception as note_error:
                print(f"⚠️ Reschedule note error: {str(note_error)}")

            try:
                self.update_google_calendar_appointment(old_datetime, new_datetime)
            except Exception as cal_error:
                print(f"⚠️ Calendar update error: {str(cal_error)}")

            try:
                self.notify_team_about_reschedule(old_datetime, new_datetime)
            except Exception as team_error:
                print(f"⚠️ Team notification error: {str(team_error)}")

            return self._build_reschedule_confirmation(old_datetime, new_datetime)


        def log_ai_reschedule_decision(self, message, ai_decision, confidence=None):
            """Log AI reschedule decisions for monitoring and improvement"""
            try:
                log_entry = {
                    'timestamp': timezone.now().isoformat(),
                    'phone': self.phone_number,
                    'message': message,
                    'ai_decision': ai_decision,
                    'confidence': confidence,
                    'appointment_status': self.appointment.status,
                    'has_scheduled_time': bool(self.appointment.scheduled_datetime)
                }
            
                # You can save this to a log file or database for analysis
                print(f"🤖 AI Reschedule Decision: {log_entry}")
            
                # Optional: Save to database for analysis
                # RescheduleDecisionLog.objects.create(**log_entry)
            
            except Exception as e:
                print(f"Error logging AI decision: {str(e)}")

