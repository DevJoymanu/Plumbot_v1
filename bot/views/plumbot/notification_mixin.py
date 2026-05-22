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
from ...plumber_notifications import send_plumber_notification_email

try:
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
except ImportError:
    pass

import logging
logger = logging.getLogger(__name__)


class NotificationMixin:
        def notify_plumber_about_plan(self):
            """Send plan details to plumber via WhatsApp"""
            try:
                base_url = os.getenv("SITE_URL", "http://127.0.0.1:8000")

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

                plumber_numbers = [
                    '263774819901',  # ✅ international format
                ]

                for number in plumber_numbers:
                    whatsapp_api.send_text_message(number, plumber_message)
                    print(f"✅ Plan notification sent to plumber {number}")

            except Exception as e:
                print(f"❌ Error notifying plumber: {str(e)}")


        def send_confirmation_message(self, appointment_info, appointment_datetime):
            """Send booking confirmation to customer."""
            try:
                display_datetime = self.format_datetime_for_display(appointment_datetime)

                service_map = {
                    'bathroom_renovation':        'Bathroom Renovation',
                    'new_plumbing_installation':  'New Plumbing Installation',
                    'kitchen_renovation':         'Kitchen Renovation',
                }
                service_name = service_map.get(
                    appointment_info.get('project_type', ''),
                    (appointment_info.get('project_type') or 'Plumbing service')
                    .replace('_', ' ').title()
                )

                confirmation_message = (
                    f"✅ APPOINTMENT CONFIRMED\n\n"
                    f"📅 Date: {display_datetime.strftime('%A, %B %d, %Y')}\n"
                    f"🕐 Time: {display_datetime.strftime('%I:%M %p')}\n"
                    f"📍 Area: {appointment_info.get('area', 'Your area')}\n"
                    f"🔧 Service: {service_name}\n\n"
                    f"We will contact you before arrival.\n\n"
                    f"Questions? Just reply here.\n"
                    f"— Homebase Plumbers"
                )

                clean_phone = clean_phone_number(self.phone_number)
                whatsapp_api.send_text_message(clean_phone, confirmation_message)
                print(f"✅ Confirmation sent to {clean_phone}")

            except Exception as e:
                print(f"❌ Confirmation message error: {str(e)}")


        def notify_team(self, appointment_info, appointment_datetime):
                """Notify team about new appointment booking via WhatsApp."""
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
                        f"🔗 View: https://plumbotv1-production.up.railway.app/appointments/{self.appointment.id}/"
                    )

                    # Build recipient list from env var → appointment field → hardcoded fallback
                    team_numbers = []

                    env_numbers = os.environ.get('TEAM_NUMBERS', '')
                    for n in env_numbers.replace('\n', ',').split(','):
                        n = n.strip().replace('whatsapp:', '').replace('+', '')
                        if n:
                            team_numbers.append(n)

                    plumber_contact = getattr(self.appointment, 'plumber_contact_number', None)
                    if plumber_contact:
                        n = plumber_contact.replace('whatsapp:', '').replace('+', '').strip()
                        if n and n not in team_numbers:
                            team_numbers.append(n)

                    if not team_numbers:
                        team_numbers = ['263774819901']
                        print("⚠️ TEAM_NUMBERS env var not set — using hardcoded fallback")

                    print(f"📤 Sending booking notifications to {len(team_numbers)} team member(s)...")

                    sent_count = 0
                    for number in team_numbers:
                        try:
                            whatsapp_api.send_text_message(number, team_message)
                            print(f"✅ Booking notification sent to {number}")
                            sent_count += 1
                        except Exception as msg_error:
                            print(f"❌ Failed to send to {number}: {msg_error}")

                    if sent_count == 0:
                        print("❌ No booking notifications sent — check TEAM_NUMBERS env var and WhatsApp API config")

                    send_plumber_notification_email(
                        subject=f"New booking notification for {appointment_info.get('name', 'Unknown')}",
                        message=team_message,
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
            
                print(f"✅ Added to Google Calendar")
                return event_result
            
            except Exception as e:
                print(f"❌ Google Calendar Error: {str(e)}")
                return None


        def send_message(self, message_text):
            """Send WhatsApp message using Cloud API"""
            try:
                clean_phone = clean_phone_number(self.phone_number)
                result = whatsapp_api.send_text_message(clean_phone, message_text)
                print(f"✅ Message sent via Cloud API to {clean_phone}")
                return result
            except Exception as e:
                print(f"❌ Failed to send message: {str(e)}")
                raise

