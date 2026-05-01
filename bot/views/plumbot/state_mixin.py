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
    twilio_client, deepseek_client,
    TWILIO_WHATSAPP_NUMBER, GOOGLE_CALENDAR_CREDENTIALS,
    DEEPSEEK_API_KEY,
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


class StateMixin:
        def _time_confirmed(self) -> bool:
            """
            Returns True when a specific time (not just a date) has been stored on
            scheduled_datetime.  We consider the time confirmed if scheduled_datetime
            has a non-midnight hour OR if the flag TIME_CONFIRMED is present in
            internal_notes.
            """
            dt = self.appointment.scheduled_datetime
            if dt is None:
                return False
            sa_tz = pytz.timezone('Africa/Johannesburg')
            local_dt = dt.astimezone(sa_tz) if dt.tzinfo else sa_tz.localize(dt)
            # Only treat a time as confirmed if it is non-midnight in local time.
            if local_dt.hour != 0 or local_dt.minute != 0:
                return True
            # Fallback flag written when we auto-assign a time
            return 'TIME_CONFIRMED' in (self.appointment.internal_notes or '')


        def _mark_time_confirmed(self):
            notes = self.appointment.internal_notes or ''
            if 'TIME_CONFIRMED' not in notes:
                self.appointment.internal_notes = (notes + '\n[TIME_CONFIRMED]').strip()
                self.appointment.save(update_fields=['internal_notes'])


        def _customer_name_declined(self) -> bool:
            return 'NAME_DECLINED' in (self.appointment.internal_notes or '')


        def _mark_customer_name_declined(self):
            notes = self.appointment.internal_notes or ''
            if 'NAME_DECLINED' not in notes:
                self.appointment.internal_notes = (notes + '\n[NAME_DECLINED]').strip()
                self.appointment.save(update_fields=['internal_notes'])


        def _mark_delay_signal(self):
            """Pause automated follow-ups until customer re-engages."""
            notes = self.appointment.internal_notes or ''
            if '[DELAY_SIGNAL]' not in notes:
                self.appointment.internal_notes = (notes + '\n[DELAY_SIGNAL]').strip()
                self.appointment.save(update_fields=['internal_notes'])
            print(f"⏸️ Follow-ups paused for {self.appointment.id} — delay signal written")


        def _clear_customer_name_declined(self):
            notes = self.appointment.internal_notes or ''
            if 'NAME_DECLINED' in notes:
                cleaned = notes.replace('\n[NAME_DECLINED]', '').replace('[NAME_DECLINED]\n', '').replace('[NAME_DECLINED]', '')
                self.appointment.internal_notes = cleaned.strip()
                self.appointment.save(update_fields=['internal_notes'])


        def _email_pending(self) -> bool:
            return '[EMAIL_PENDING]' in (self.appointment.internal_notes or '')


        def _mark_email_pending(self):
            notes = self.appointment.internal_notes or ''
            if '[EMAIL_PENDING]' not in notes:
                self.appointment.internal_notes = (notes + '\n[EMAIL_PENDING]').strip()
                self.appointment.save(update_fields=['internal_notes'])


        def _clear_email_pending(self):
            notes = self.appointment.internal_notes or ''
            if '[EMAIL_PENDING]' in notes:
                cleaned = (notes
                           .replace('\n[EMAIL_PENDING]', '')
                           .replace('[EMAIL_PENDING]\n', '')
                           .replace('[EMAIL_PENDING]', ''))
                self.appointment.internal_notes = cleaned.strip()
                self.appointment.save(update_fields=['internal_notes'])


        def _extract_email_from_text(self, text: str):
            import re
            m = re.search(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', text or '')
            return m.group(0).lower() if m else None


        def _declines_sharing_email(self, text: str) -> bool:
            msg  = (text or '').strip().lower()
            skips = {'skip', 'no', 'nope', 'nah', 'dont have', "don't have",
                     'prefer not', 'rather not', 'whatsapp', 'here', 'na'}
            return any(s in msg for s in skips) and '@' not in msg


        @staticmethod
        def _is_excluded_city(area_text: str):
            """
            Return the canonical city name if the area is outside our service zone,
            or None if it's a valid Harare area.

            Handles edge cases:
            - "Bulawayo Road"  → None  (street in Harare — continue booking)
            - "Bulawayo"       → "Bulawayo" (the city — dismiss)
            - "Gweru"          → "Gweru"
            - "Harare Mutare Road" → None (Harare mentioned — continue)
            """
            _EXCLUDED = {'gweru', 'bulawayo', 'mutare', 'masvingo'}
            _STREET_WORDS = {
                'road', 'rd', 'avenue', 'ave', 'crescent', 'drive', 'dr',
                'street', 'st', 'close', 'lane', 'way', 'park', 'gardens',
                'heights', 'view', 'court', 'ct', 'place', 'grove', 'row',
                'terrace', 'boulevard', 'blvd', 'circle', 'extension', 'ext',
            }
            words = set(area_text.lower().split())
            # Explicit Harare mention → always valid
            if 'harare' in words:
                return None
            # Street/road suffix alongside city name → it's a Harare address
            has_street = bool(words & _STREET_WORDS)
            for city in _EXCLUDED:
                if city in words:
                    if has_street:
                        return None
                    return city.capitalize()
            return None


        def _confirm_or_request_email(self):
            """
            Called when a customer name has just been captured on a confirmed booking.
            If we already have their email → send confirmation + email.
            If not → ask for email first (sets EMAIL_PENDING state).
            """
            if self.appointment.customer_email:
                from bot.customer_emails import send_booking_confirmation_email
                send_booking_confirmation_email(self.appointment)
                return self._build_named_booking_confirmation()
            self._mark_email_pending()
            return (
                "What email should I send your booking confirmation to? "
                "Just say 'skip' if you'd prefer not to."
            )


        def _handle_email_capture(self, message: str):
            """
            Process the customer's email reply while EMAIL_PENDING is active.
            Returns the final WhatsApp confirmation message.
            """
            if self._declines_sharing_email(message):
                self._clear_email_pending()
                reply = self._build_named_booking_confirmation()
            else:
                email = self._extract_email_from_text(message)
                if email:
                    self.appointment.customer_email = email
                    self.appointment.save(update_fields=['customer_email'])
                    self._clear_email_pending()
                    from bot.customer_emails import send_booking_confirmation_email
                    send_booking_confirmation_email(self.appointment)
                    reply = self._build_named_booking_confirmation()
                else:
                    reply = (
                        "That doesn't look like an email address — could you "
                        "double-check it? Or just say 'skip' if you'd prefer not to share."
                    )
            self.appointment.add_conversation_message("user", message)
            self.appointment.add_conversation_message("assistant", reply)
            return reply


        def _get_question_retry_counts(self) -> dict:
            notes = self.appointment.internal_notes or ''
            pattern = r'\[QUESTION_RETRY_COUNTS\](\{.*?\})'
            match = re.search(pattern, notes, re.DOTALL)
            if not match:
                return {}
            try:
                data = json.loads(match.group(1))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}


        def _save_question_retry_counts(self, counts: dict):
            notes = self.appointment.internal_notes or ''
            cleaned = re.sub(r'\n?\[QUESTION_RETRY_COUNTS\]\{.*?\}', '', notes, flags=re.DOTALL).strip()
            payload = f"[QUESTION_RETRY_COUNTS]{json.dumps(counts, sort_keys=True)}"
            self.appointment.internal_notes = f"{cleaned}\n{payload}".strip() if cleaned else payload
            self.appointment.save(update_fields=['internal_notes'])


        def _get_question_retry_count(self, question: str) -> int:
            counts = self._get_question_retry_counts()
            try:
                return max(0, int(counts.get(question, 0)))
            except Exception:
                return 0


        def _set_question_retry_count(self, question: str, count: int):
            counts = self._get_question_retry_counts()
            counts[question] = max(0, int(count))
            self._save_question_retry_counts(counts)


        def _sync_retry_count_field(self, question: str):
            if not self._appointment_has_field('retry_count'):
                return
            current = self._get_question_retry_count(question)
            self.appointment.retry_count = current
            self.appointment.save(update_fields=['retry_count'])


        def _appointment_has_field(self, field_name: str) -> bool:
            """Return True only if the Appointment model has this concrete field."""
            return any(f.name == field_name for f in self.appointment._meta.concrete_fields)


        def _delay_signal_active(self) -> bool:
            """Return True if customer previously gave a delay signal."""
            return '[DELAY_SIGNAL]' in (self.appointment.internal_notes or '')

