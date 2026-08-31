from django.conf import settings
from django.utils import timezone

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db.models import F, TextField, Value
from django.db.models.functions import Coalesce, Concat

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import requests
import pytz
import os
import json
import random
import re
import tempfile
import threading
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
from ...whatsapp_cloud_api import get_client_for_tenant, whatsapp_api
from ...plumber_notifications import send_plumber_notification_email

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:
    pass

import logging
logger = logging.getLogger(__name__)


def cancel_calendar_event(appointment):
    """Take a released visit off the plumber's calendar.

    Module-level (not a mixin method) because the delay flow releases a slot
    with nothing but the Appointment in hand. Best-effort and silent when the
    calendar isn't configured or no event was ever created — a calendar we
    can't reach must never block the state change that keeps the plumber from
    driving out to a visit the customer cancelled.
    """
    event_id = (getattr(appointment, 'google_calendar_event_id', '') or '').strip()
    if not event_id or not GOOGLE_CALENDAR_CREDENTIALS:
        return False
    try:
        credentials = service_account.Credentials.from_service_account_info(
            GOOGLE_CALENDAR_CREDENTIALS,
            scopes=['https://www.googleapis.com/auth/calendar'],
        )
        service = build('calendar', 'v3', credentials=credentials)
        service.events().delete(calendarId='primary', eventId=event_id).execute()
        appointment.google_calendar_event_id = ''
        appointment.save(update_fields=['google_calendar_event_id'])
        logger.info("Calendar event %s deleted — apt %s", event_id,
                    getattr(appointment, 'pk', None))
        return True
    except Exception:
        logger.exception("cancel_calendar_event failed — apt %s",
                         getattr(appointment, 'pk', None))
        return False


class NotificationMixin:
        def _maybe_alert_plumber_date_no_time(self):
            """
            Email the plumber once when a lead has committed an appointment DATE
            but no time, so a human can call to pin the time down. Guarded by a
            [DATE_NO_TIME_ALERTED] note so it fires at most once per lead.

            A date-only booking is stored at midnight; a confirmed time is not.
            We compare in SAST because scheduled_datetime is read back in UTC.
            """
            apt = self.appointment
            if not apt or not apt.scheduled_datetime:
                return
            from ...test_console import is_test_sender
            if is_test_sender(self.phone_number):
                return
            notes = apt.internal_notes or ''
            if '[DATE_NO_TIME_ALERTED]' in notes:
                return
            sast = apt.scheduled_datetime.astimezone(pytz.timezone('Africa/Johannesburg'))
            if sast.hour != 0 or sast.minute != 0:
                return  # a real time is set — nothing to chase
            try:
                from ...plumber_notifications import send_plumber_followup_alert
                send_plumber_followup_alert(apt, reason='date_no_time')
                apt.internal_notes = f'{notes}\n[DATE_NO_TIME_ALERTED]'.strip()
                apt.save(update_fields=['internal_notes'])
                logger.info("Plumber alerted: date but no time — apt %s",
                            getattr(apt, 'pk', None))
            except Exception:
                logger.exception("_maybe_alert_plumber_date_no_time failed — apt %s",
                                 getattr(apt, 'pk', None))

        def notify_plumber_about_plan(self):
            """Send plan details to plumber via WhatsApp"""
            from ...test_console import is_test_sender
            if is_test_sender(self.phone_number):
                print("🧪 Test lead — plan-received plumber alert muted")
                return
            try:
                base_url = getattr(settings, "SITE_URL", "") or "http://127.0.0.1:8000"

                service_name = self.appointment.project_type.replace('_', ' ').title()
                customer_name = self.appointment.customer_name or "Customer"
                customer_phone = self.phone_number.replace('whatsapp:', '')

                details_url = (
                    f"{base_url}/appointments/"
                    f"{self.appointment.id}/documents/"
                )

                plumber_message = f"""📋 NEW PLAN RECEIVED!

        Customer: {customer_name}
        Phone: {customer_phone}
        Service: {service_name}
        Area: {self.appointment.customer_area}
        Property: {self.appointment.property_type}
        Timeline: {self.appointment.timeline}

        🔍 PLAN DETAILS:
        The customer has uploaded their plan via WhatsApp.

        Please:
        1. Review the uploaded plan materials
        2. Contact the customer within 24 hours
        3. Discuss project scope and provide a quote
        4. Book appointment once confirmed

        🔗 View full details:
        {details_url}

        Status: Plan uploaded — awaiting your review
        """

                # Phase 2.2: the tenant's own plumber line — no number on
                # file → skip the WhatsApp alert (email path still fires).
                _contact = self.appointment.plumber_contact().replace(
                    'whatsapp:', '').replace('+', '').strip()
                plumber_numbers = [_contact] if _contact else []
                if not plumber_numbers:
                    print("⚠️ No plumber contact on tenant profile — skipping plan WhatsApp alert")

                for number in plumber_numbers:
                    get_client_for_tenant(self.appointment.tenant).send_text_message(number, plumber_message)
                    print(f"✅ Plan notification sent to plumber {number}")

            except Exception as e:
                print(f"❌ Error notifying plumber: {str(e)}")


        # ── Booking confirmation pacing ──────────────────────────────────
        # The confirmation used to leave the instant the booking row saved,
        # which is the one thing a person never does. A short pause reads like
        # someone writing the details up and sending them over; keep it short
        # enough that the customer never wonders whether the booking took.
        CONFIRMATION_DELAY_MIN_SECONDS = int(os.getenv('CONFIRMATION_DELAY_MIN_SECONDS', '45'))
        CONFIRMATION_DELAY_MAX_SECONDS = int(os.getenv('CONFIRMATION_DELAY_MAX_SECONDS', '90'))

        def _confirmation_slot_marker(self, appointment_datetime):
            """The dedupe marker for one booked slot, in SAST so it reads the
            same as the time the customer was given."""
            sa_timezone = pytz.timezone('Africa/Johannesburg')
            when = appointment_datetime
            if when.tzinfo is None:
                when = sa_timezone.localize(when)
            return f"[CONFIRMATION_SENT:{when.astimezone(sa_timezone).strftime('%Y-%m-%dT%H:%M')}]"

        def _claim_confirmation_send(self, appointment_datetime):
            """Claim the confirmation for this slot; False = already claimed.

            A booking can be triggered more than once — a repeated "yes", the
            dashboard Confirm button pressed twice, a re-book landing on the
            same slot — and each one used to send its own confirmation. The
            claim is a single conditional UPDATE, so two workers racing cannot
            both win it, and it is keyed on the SLOT so a genuine reschedule to
            a new time still confirms.
            """
            apt = self.appointment
            if not apt or not apt.pk:
                return True  # nothing to dedupe against — send it
            marker = self._confirmation_slot_marker(appointment_datetime)
            claimed = Appointment.objects.filter(pk=apt.pk).exclude(
                internal_notes__contains=marker
            ).update(
                internal_notes=Concat(
                    Coalesce(F('internal_notes'), Value('')),
                    Value(f'\n{marker}'),
                    output_field=TextField(),
                )
            )
            # Keep the in-memory copy in step: a later full save() off this
            # instance would write the pre-claim notes back and let a second
            # confirmation through.
            apt.internal_notes = Appointment.objects.filter(pk=apt.pk).values_list(
                'internal_notes', flat=True).first()
            return bool(claimed)

        def _lead_language(self):
            """The language the lead last wrote in — the confirmation mirrors it."""
            try:
                from ...repeated_question_detector import detect_language_simple
                for entry in reversed(self.appointment.conversation_history or []):
                    if entry.get('role') == 'user' and (entry.get('content') or '').strip():
                        return detect_language_simple(entry['content'])
            except Exception as e:
                print(f"⚠️ Confirmation language check failed: {e}")
            return 'english'

        def _build_confirmation_message(self, appointment_info, appointment_datetime):
            """The booking confirmation written the way a person would text it —
            no headings, no labelled fields, no emojis.

            Framed as "in writing" because the booking turn has usually already
            acknowledged the slot in conversation; this lands a beat later as the
            written version, not as the same news told twice.
            """
            display_datetime = self.format_datetime_for_display(appointment_datetime)
            when = (
                f"{display_datetime.strftime('%A')}, "
                f"{display_datetime.strftime('%d %B').lstrip('0')} at "
                f"{display_datetime.strftime('%I:%M %p').lstrip('0')}"
            )

            service_map = {
                'bathroom_renovation':        'Bathroom Renovation',
                'new_plumbing_installation':  'New Plumbing Installation',
                'kitchen_renovation':         'Kitchen Renovation',
            }
            raw_service = (appointment_info.get('project_type') or '').strip()
            service = service_map.get(
                raw_service, raw_service.replace('_', ' ')
            ).strip().lower()
            area = (appointment_info.get('area') or '').strip()

            # Absent means omit: a lead with no area on file is not told we are
            # coming to "your area".
            if self._lead_language() == 'shona':
                opener = f"Zvanyorwa pasi — {when}."
                if service and area:
                    detail = f"Tichauya ku{area} kuzotarisa {service}."
                elif service:
                    detail = f"Tichauya kuzotarisa {service}."
                elif area:
                    detail = f"Tichauya ku{area}."
                else:
                    detail = ""
                closing = ("Tichakufonerai tisati tasvika. Kana paine chinochinja, "
                           "ndinyorerei pano.")
            else:
                opener = f"Just so you've got it in writing — you're booked for {when}."
                if service and area:
                    detail = f"We'll come through to {area} to have a look at the {service}."
                elif service:
                    detail = f"I've got you down for the {service}."
                elif area:
                    detail = f"We'll come through to {area}."
                else:
                    detail = ""
                closing = ("Someone will call you before we head over. If anything "
                           "changes, just message me here.")

            first_line = f"{opener} {detail}".strip()
            return f"{first_line}\n\n{closing}"

        def send_confirmation_message(self, appointment_info, appointment_datetime):
            """Queue the booking confirmation to the customer.

            Deliberately not sent inline: it goes out through delayed_response
            after a short pause, so it lands like a person following up rather
            than a receipt printed the moment the record saved. That path also
            logs the turn and stamps the outbound WAMID, so a customer who
            highlights the confirmation gets it resolved back.
            """
            try:
                if not self._claim_confirmation_send(appointment_datetime):
                    print("↩️ Confirmation already queued for this slot — skipping duplicate")
                    return

                confirmation_message = self._build_confirmation_message(
                    appointment_info, appointment_datetime)
                clean_phone = clean_phone_number(self.phone_number)
                delay = random.randint(self.CONFIRMATION_DELAY_MIN_SECONDS,
                                       self.CONFIRMATION_DELAY_MAX_SECONDS)

                from ...whatsapp_webhook import delayed_response
                threading.Thread(
                    target=delayed_response,
                    args=(clean_phone, confirmation_message, delay),
                    kwargs={'tenant': self.appointment.tenant},
                    daemon=True,
                ).start()
                print(f"✅ Confirmation queued for {clean_phone} — sending in {delay}s")

            except Exception as e:
                print(f"❌ Confirmation message error: {str(e)}")


        def notify_team(self, appointment_info, appointment_datetime):
                """Notify team about new appointment booking via WhatsApp."""
                from ...test_console import is_test_sender
                if is_test_sender(self.phone_number):
                    print("🧪 Test lead — team booking alert muted")
                    return
                try:
                    import os

                    # Format datetime for display
                    display_datetime = self.format_datetime_for_display(appointment_datetime)

                    service_name = appointment_info.get('project_type', 'Plumbing service')
                    if service_name:
                        service_map = {
                            'bathroom_renovation': 'Bathroom Renovation',
                            'new_plumbing_installation': 'New Plumbing Installation',
                            'kitchen_renovation': 'Kitchen Renovation'
                        }
                        service_name = service_map.get(service_name, service_name.replace('_', ' ').title())

                    plan_status = "Not specified"
                    if appointment_info.get('has_plan') is not None:
                        plan_status = "Has existing plan" if appointment_info['has_plan'] else "Needs site visit"

                    customer_phone = (
                        self.phone_number
                        .replace('whatsapp:+', '')
                        .replace('whatsapp:', '')
                        .replace('+', '')
                    )

                    team_message = (
                        f"🚨 NEW APPOINTMENT BOOKED!\n\n"
                        f"👤 Customer: {appointment_info.get('name', 'Unknown')}\n"
                        f"📞 Phone: +{customer_phone}\n"
                        f"💬 WhatsApp: wa.me/{customer_phone}\n\n"
                        f"📋 APPOINTMENT DETAILS:\n"
                        f"  📅 Date/Time: {display_datetime.strftime('%A, %B %d at %I:%M %p')}\n"
                        f"  🔧 Service: {service_name}\n"
                        f"  📍 Area: {appointment_info.get('area', 'Not provided')}\n"
                        f"  🏠 Property: {appointment_info.get('property_type', 'Not specified')}\n"
                        f"  ⏰ Timeline: {appointment_info.get('timeline', 'Not specified')}\n"
                        f"  📐 Plan: {plan_status}\n\n"
                        f"🔗 View: {settings.SITE_URL}/appointments/{self.appointment.id}/"
                    )

                    # Build recipient list from env var → appointment field → hardcoded fallback
                    team_numbers = []

                    env_numbers = os.environ.get('TEAM_NUMBERS', '')
                    for n in env_numbers.replace('\n', ',').split(','):
                        n = n.strip().replace('whatsapp:', '').replace('+', '')
                        if n:
                            team_numbers.append(n)

                    plumber_contact = self.appointment.plumber_contact()
                    if plumber_contact:
                        n = plumber_contact.replace('whatsapp:', '').replace('+', '').strip()
                        if n and n not in team_numbers:
                            team_numbers.append(n)

                    if not team_numbers:
                        print("⚠️ No TEAM_NUMBERS env and no tenant plumber contact — skipping booking WhatsApp alerts")

                    print(f"📤 Sending booking notifications to {len(team_numbers)} team member(s)...")

                    sent_count = 0
                    for number in team_numbers:
                        try:
                            get_client_for_tenant(self.appointment.tenant).send_text_message(number, team_message)
                            print(f"✅ Booking notification sent to {number}")
                            sent_count += 1
                        except Exception as msg_error:
                            print(f"❌ Failed to send to {number}: {msg_error}")

                    if sent_count == 0:
                        print("❌ No booking notifications sent — check TEAM_NUMBERS env var and WhatsApp API config")

                    # HTML version with Call/WhatsApp-customer CTA buttons so
                    # the plumber can reach the customer in one tap. Falls back
                    # to plain-text only if the builder fails.
                    booking_html = None
                    try:
                        from ...customer_emails import build_plumber_booking_email_html
                        site_url = getattr(settings, 'SITE_URL', '').rstrip('/')
                        booking_html = build_plumber_booking_email_html(
                            customer_name=appointment_info.get('name', 'Unknown'),
                            customer_phone_digits=customer_phone,
                            datetime_str=display_datetime.strftime('%A, %B %d at %I:%M %p'),
                            service=service_name,
                            area=appointment_info.get('area'),
                            property_type=appointment_info.get('property_type'),
                            timeline=appointment_info.get('timeline'),
                            plan_status=plan_status,
                            view_url=f"{site_url}/appointments/{self.appointment.id}/",
                            apt=self.appointment,
                        )
                    except Exception as html_error:
                        print(f"⚠️ Booking email HTML build failed: {html_error}")

                    send_plumber_notification_email(
                        subject=f"New booking — {appointment_info.get('name', 'Unknown')}",
                        message=team_message,
                        html_message=booking_html,
                        tenant=getattr(self.appointment, 'tenant', None),
                    )

                except Exception as e:
                    print(f"❌ Team notification error: {str(e)}")
                    import traceback
                    traceback.print_exc()


        def add_to_google_calendar(self, appointment_info, appointment_datetime):
            """Add appointment to Google Calendar"""
            try:
                # Skip if no credentials configured
                if not GOOGLE_CALENDAR_CREDENTIALS:
                    print("⚠️ Google Calendar credentials not configured")
                    return None
                
                # Initialize Google Calendar service
                credentials = service_account.Credentials.from_service_account_info(
                    GOOGLE_CALENDAR_CREDENTIALS,
                    scopes=['https://www.googleapis.com/auth/calendar']
                )
                service = build('calendar', 'v3', credentials=credentials)
            
                # Create event description
                description_parts = []
                if appointment_info.get('project_type'):
                    description_parts.append(f"Service: {appointment_info['project_type']}")
                if appointment_info.get('area'):
                    description_parts.append(f"Area: {appointment_info['area']}")
                if appointment_info.get('property_type'):
                    description_parts.append(f"Property: {appointment_info['property_type']}")
                if appointment_info.get('timeline'):
                    description_parts.append(f"Timeline: {appointment_info['timeline']}")
                if appointment_info.get('has_plan') is not None:
                    plan_status = "Has existing plan" if appointment_info['has_plan'] else "Needs site visit"
                    description_parts.append(f"Plan Status: {plan_status}")
                
                description_parts.append(f"Phone: {self.phone_number}")
            
                # Create event
                event = {
                    'summary': f"Plumbing Appointment - {appointment_info.get('name', 'Customer')}",
                    'description': "\n".join(description_parts),
                    'start': {
                        'dateTime': appointment_datetime.isoformat(),
                        'timeZone': 'Africa/Johannesburg',
                    },
                    'end': {
                        'dateTime': (appointment_datetime + timedelta(hours=2)).isoformat(),
                        'timeZone': 'Africa/Johannesburg',
                    },
                    'attendees': [
                        {'email': 'team@plumbingcompany.com'},
                    ],
                    'reminders': {
                        'useDefault': False,
                        'overrides': [
                            {'method': 'email', 'minutes': 24 * 60},
                            {'method': 'popup', 'minutes': 30},
                        ],
                    },
                }
            
                # Insert event
                event_result = service.events().insert(
                    calendarId='primary',
                    body=event
                ).execute()

                # Keep the event handle. Without it a reschedule has nothing to
                # move, which is why google_calendar_event_id sat unused on the
                # model while the calendar kept every stale time.
                try:
                    event_id = (event_result or {}).get('id')
                    if event_id:
                        self.appointment.google_calendar_event_id = event_id
                        self.appointment.save(update_fields=['google_calendar_event_id'])
                except Exception as save_error:
                    print(f"⚠️ Could not store calendar event id: {save_error}")

                print(f"✅ Added to Google Calendar")
                return event_result

            except Exception as e:
                print(f"❌ Google Calendar Error: {str(e)}")
                return None


        def update_google_calendar_appointment(self, old_datetime, new_datetime):
            """Move this lead's calendar event to the new time.

            Called on every reschedule. It never existed, so the caller's except
            swallowed an AttributeError and the plumber's calendar kept the old
            slot. With no event id on file (anything booked before the id was
            stored) it creates the event at the new time instead.
            """
            try:
                if not GOOGLE_CALENDAR_CREDENTIALS:
                    print("⚠️ Google Calendar credentials not configured")
                    return None

                event_id = (self.appointment.google_calendar_event_id or '').strip()
                if not event_id:
                    print("ℹ️ No calendar event on file — creating one at the new time")
                    return self.add_to_google_calendar(
                        self.extract_appointment_details(), new_datetime)

                credentials = service_account.Credentials.from_service_account_info(
                    GOOGLE_CALENDAR_CREDENTIALS,
                    scopes=['https://www.googleapis.com/auth/calendar']
                )
                service = build('calendar', 'v3', credentials=credentials)

                # A job books its own duration; a site visit is the standard 2h.
                hours = 2
                if self.appointment.appointment_type == 'job_appointment':
                    hours = self.appointment.job_duration_hours or 4

                updated = service.events().patch(
                    calendarId='primary',
                    eventId=event_id,
                    body={
                        'start': {
                            'dateTime': new_datetime.isoformat(),
                            'timeZone': 'Africa/Johannesburg',
                        },
                        'end': {
                            'dateTime': (new_datetime + timedelta(hours=hours)).isoformat(),
                            'timeZone': 'Africa/Johannesburg',
                        },
                    },
                ).execute()

                print(f"✅ Google Calendar event moved to {new_datetime}")
                return updated

            except Exception as e:
                print(f"❌ Google Calendar update error: {str(e)}")
                return None


        def send_message(self, message_text):
            """Send WhatsApp message using Cloud API"""
            try:
                clean_phone = clean_phone_number(self.phone_number)
                _tenant = getattr(getattr(self, 'appointment', None), 'tenant', None)
                result = get_client_for_tenant(_tenant).send_text_message(clean_phone, message_text)
                print(f"✅ Message sent via Cloud API to {clean_phone}")
                return result
            except Exception as e:
                print(f"❌ Failed to send message: {str(e)}")
                raise

