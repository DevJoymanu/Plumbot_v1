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


class BookingMixin:
        def process_alternative_time_selection(self, message):
            """Use DeepSeek to detect and parse when customer selects an alternative time slot"""
            from datetime import datetime, timedelta
            try:
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                now = timezone.now().astimezone(sa_timezone)

                # Build next-day lookup
                day_names = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
                next_days = {}
                for i, name in enumerate(day_names):
                    days_ahead = (i - now.weekday()) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    next_days[name] = (now + timedelta(days=days_ahead)).strftime('%B %d, %Y')

                prompt = f"""You are a datetime extraction assistant.

        The customer was shown a list of available appointment slots and is replying to choose one, 
        or suggesting a new time. Extract the date and time they want.

        CURRENT DATETIME: {now.strftime('%Y-%m-%d %H:%M')} (Africa/Johannesburg)
        WORKING DAYS: Sunday–Friday (Saturday CLOSED)

        NEXT OCCURRENCE OF EACH DAY:
        - Monday: {next_days['monday']}
        - Tuesday: {next_days['tuesday']}
        - Wednesday: {next_days['wednesday']}
        - Thursday: {next_days['thursday']}
        - Friday: {next_days['friday']}
        - Saturday: {next_days['saturday']} ← CLOSED, do not use
        - Sunday: {next_days['sunday']}
        - Tomorrow: {(now + timedelta(days=1)).strftime('%B %d, %Y')}

        CUSTOMER MESSAGE: "{message}"

        Return ONLY one of:
        - YYYY-MM-DDTHH:MM  (if both date and time are clear)
        - SATURDAY_CLOSED   (if they picked Saturday)
        - NOT_FOUND         (if no clear selection)

        No other text."""

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "Return only a datetime string YYYY-MM-DDTHH:MM, SATURDAY_CLOSED, or NOT_FOUND."
                        },
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.1,
                    max_tokens=100
                )

                ai_response = response.choices[0].message.content.strip()
                print(f"🤖 DeepSeek alternative selection: '{message}' → {ai_response}") 

                if ai_response in ("SATURDAY_CLOSED", "NOT_FOUND"):
                    msg = (message or '').strip().lower()

                    if 'tomorrow' in msg:
                        candidate = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                        if candidate.weekday() == 5:
                            return None
                        print(f"✅ Manual day selection captured from 'tomorrow': {candidate}")
                        return candidate

                    for i, name in enumerate(day_names):
                        if name in msg:
                            if name == 'saturday':
                                return None
                            days_ahead = (i - now.weekday()) % 7
                            if days_ahead == 0:
                                days_ahead = 7
                            candidate = (now + timedelta(days=days_ahead)).replace(hour=0, minute=0, second=0, microsecond=0)
                            print(f"✅ Manual day selection captured from '{name}': {candidate}")
                            return candidate

                    return None

                parsed_dt = datetime.strptime(ai_response, '%Y-%m-%dT%H:%M')
                localized_dt = sa_timezone.localize(parsed_dt)
                print(f"✅ Parsed alternative selection: {localized_dt}")
                return localized_dt

            except Exception as e:
                print(f"❌ DeepSeek alternative selection error: {e}")
                return None


        def book_appointment_with_selected_time(self, selected_datetime):
            """Book appointment with a customer-selected alternative time.

            Delegates entirely to book_appointment() which handles status update,
            confirmation message, team alert, and calendar — no duplicate sends.
            """
            try:
                print(f"🔄 Booking appointment with selected time: {selected_datetime}")

                is_available, conflict_info = self.check_appointment_availability(selected_datetime)

                if is_available:
                    self.appointment.scheduled_datetime = selected_datetime
                    self.appointment.save(update_fields=['scheduled_datetime'])

                    # book_appointment() handles everything from here
                    result = self.book_appointment(message=None)

                    if result['success']:
                        print(f"✅ Appointment booked via selected time: {selected_datetime}")
                        return result

                    alternatives = self.get_alternative_time_suggestions(selected_datetime)
                    return {'success': False, 'error': 'Time became unavailable', 'alternatives': alternatives}

                else:
                    print(f"❌ Selected time not available: {conflict_info}")
                    alternatives = self.get_alternative_time_suggestions(selected_datetime)
                    return {'success': False, 'error': 'Selected time not available', 'alternatives': alternatives}

            except Exception as e:
                print(f"❌ Error booking with selected time: {str(e)}")
                return {'success': False, 'error': str(e)}


        def smart_booking_check(self):
            """
            Ready to book when all 5 required fields are present.
            has_plan is NOT required — it is no longer part of the booking flow.
            """
            has_service  = bool(self.appointment.project_type)
            has_desc     = bool(self.appointment.project_description)
            has_datetime = (
                bool(self.appointment.scheduled_datetime) and self._time_confirmed()
            )
            has_area     = bool(self.appointment.customer_area)

            has_all = has_service and has_desc and has_datetime and has_area

            missing = []
            if not has_service:
                missing.append("service type")
            if not has_desc:
                missing.append("project description")
            if not has_datetime:
                missing.append("availability")
            if not has_area:
                missing.append("area")

            return {
                'ready_to_book':         has_all,
                'missing_fields':        missing,
                'completion_percentage': ((4 - len(missing)) / 4) * 100,
            }


        def _build_named_booking_confirmation(self):
            """Build the final customer-facing confirmation after capturing a name."""
            display_datetime = self.format_datetime_for_display(self.appointment.scheduled_datetime)
            customer_name = self.appointment.customer_name or "there"
            customer_area = self.appointment.customer_area or "your area"
            formatted_datetime = display_datetime.strftime('%A, %B %d, %Y at %I:%M %p')

            return (
                f"Perfect — thanks, {customer_name}. You're all set for your "
                f"*free on-site assessment* on **{formatted_datetime}** in {customer_area}. "
                "Our senior plumber will call you 30 minutes before arrival to confirm. "
                "See you then!"
            )


        def book_appointment(self, message):
            """Book an appointment using the stored datetime - FIXED TIMEZONE"""
            try:
                print(f"🔄 Starting appointment booking process...")
            
                # Use the stored datetime from AI extraction
                appointment_datetime = self.appointment.scheduled_datetime
            
                if not appointment_datetime:
                    print("❌ No datetime available - booking cancelled")
                    return {'success': False, 'error': 'No appointment time set'}

                print(f"📅 Using appointment time: {appointment_datetime}")

                # Ensure proper timezone handling
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                if appointment_datetime.tzinfo is None:
                    appointment_datetime = sa_timezone.localize(appointment_datetime)
                else:
                    appointment_datetime = appointment_datetime.astimezone(sa_timezone)

                print(f"📅 Timezone-corrected appointment time: {appointment_datetime}")

                # Check availability
                is_available, conflict_info = self.check_appointment_availability(appointment_datetime)
            
                if not is_available:
                    print(f"❌ Time slot not available: {conflict_info}")
                    alternatives = self.get_alternative_time_suggestions(appointment_datetime)
                
                    return {
                        'success': False, 
                        'error': 'Time not available', 
                        'alternatives': alternatives
                    }
            
                # SUCCESS PATH: Update appointment
                self.appointment.status = 'confirmed'
                self.appointment.scheduled_datetime = appointment_datetime
                self.appointment.save()
            
                print(f"💾 Appointment confirmed and saved: {appointment_datetime}")
            
                # Extract appointment details
                appointment_details = self.extract_appointment_details()
            
                # Send notifications
                try:
                    print("📤 Sending notifications...")
                    self.send_confirmation_message(appointment_details, appointment_datetime)
                    self.notify_team(appointment_details, appointment_datetime)
                    print("✅ Notifications sent")
                except Exception as notify_error:
                    print(f"⚠️ Notification error: {notify_error}")
            
                # Add to calendar (optional)
                try:
                    if GOOGLE_CALENDAR_CREDENTIALS:
                        self.add_to_google_calendar(appointment_details, appointment_datetime)
                except Exception as cal_error:
                    print(f"⚠️ Calendar error: {cal_error}")
            
                # FIX: Format datetime for display
                display_datetime = self.format_datetime_for_display(appointment_datetime)
            
                return {
                    'success': True,
                    'datetime': display_datetime.strftime('%B %d, %Y at %I:%M %p')
                }

            except Exception as e:
                print(f"❌ Booking Error: {str(e)}")
                import traceback
                traceback.print_exc()
                return {'success': False, 'error': str(e)}


        def _handle_all_day_response(self) -> str:
            """
            Customer said they're available all day.
            Auto-assign next available time slot at or after 12:00 (noon).
            """
            import pytz as _pytz
            from datetime import datetime as dt_cls
    
            sa_tz = _pytz.timezone('Africa/Johannesburg')
            date_obj = self._get_selected_local_date()
            if not date_obj:
                return "What time works best for you — morning or afternoon?"
    
            # Try 12:00 first, then 13, 14, 15, 16
            for h in [12, 13, 14, 15, 16]:
                candidate = sa_tz.localize(
                    dt_cls.combine(date_obj, dt_cls.min.time().replace(hour=h))
                )
                is_avail, _ = self.check_appointment_availability(candidate)
                if is_avail:
                    self.appointment.scheduled_datetime = candidate
                    self._mark_time_confirmed()
                    self.appointment.save(update_fields=['scheduled_datetime', 'internal_notes'])
                    hour_str = candidate.strftime('%I%p').lstrip('0')
                    day_label = self._format_day(date_obj)
                    return (
                        f"Perfect, please expect us anytime after {hour_str} on {day_label}. "
                        f"What area are you in?"
                    )
    
            # No slot found — ask them to pick a time
            return (
                "We're quite booked that day from noon onwards. "
                "What time works best for you — morning or afternoon?"
            )


        def handle_early_datetime_provision(self, message):
            """Handle cases where customer provides date/time before we ask for availability"""
            try:
                # Extract datetime using existing method
                parsed_datetime = self.parse_datetime_with_ai(message)
            
                if parsed_datetime:
                    # Store the datetime for later use
                    self.appointment.scheduled_datetime = parsed_datetime
                    if self._appointment_has_field('retry_count'):
                        self.appointment.save(update_fields=['retry_count'])
                
                    print(f"📅 Early datetime provision captured: {parsed_datetime}")
                
                    # Check if we can book immediately
                    booking_status = self.smart_booking_check()
                
                    if booking_status['ready_to_book']:
                        print("🎯 All information available, proceeding with booking...")
                        return self.attempt_immediate_booking()
                    else:
                        missing = ", ".join(booking_status['missing_fields'])
                        print(f"📋 Still need: {missing}")
                        return None  # Continue with normal flow
            
                return None
            
            except Exception as e:
                print(f"❌ Error handling early datetime: {str(e)}")
                return None


        def attempt_immediate_booking(self):
            """Attempt to book appointment when all information is available"""
            try:
                if not self.appointment.scheduled_datetime:
                    return None
                
                # Check availability
                is_available, conflict_info = self.check_appointment_availability(self.appointment.scheduled_datetime)
            
                if is_available:
                    # Book the appointment
                    self.appointment.status = 'confirmed'
                    self.appointment.save(update_fields=['status'])
                
                    # Get appointment details for response
                    appointment_details = self.extract_appointment_details()
                
                    # Add to calendar and notify team
                    try:
                        self.send_confirmation_message(appointment_details, self.appointment.scheduled_datetime)
                        self.add_to_google_calendar(appointment_details, self.appointment.scheduled_datetime)
                        self.notify_team(appointment_details, self.appointment.scheduled_datetime)
                    except Exception as notify_error:
                        print(f"⚠️ Notification error: {notify_error}")
                
                    # Generate confirmation message
                    if self.appointment.customer_name:
                        return self._build_named_booking_confirmation()
                    else:
                        return (
                            f"Perfect! I've booked your appointment for "
                            f"{self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')}. "
                            f"I've also sent your confirmation details here on WhatsApp."
                        )
            
                else:
                    # Handle conflict
                    alternatives = self.get_alternative_time_suggestions(self.appointment.scheduled_datetime)
                    if alternatives:
                        alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                        return f"That time isn't available. Here are some alternatives:\n{alt_text}\n\nWhich works better for you?"
                    else:
                        return "That time isn't available. Could you suggest another time? Our hours are 8 AM - 6 PM, Monday to Friday."
            
            except Exception as e:
                print(f"❌ Error attempting immediate booking: {str(e)}")
                return None


        def validate_information_completeness(self):
            """Validate that all required information is present and correct"""
            try:
                validation_results = {
                    'valid': True,
                    'errors': [],
                    'warnings': []
                }
            
                # Check required fields
                if not self.appointment.project_type:
                    validation_results['errors'].append("Service type not specified")
                    validation_results['valid'] = False
            
                if self.appointment.has_plan is None:
                    validation_results['errors'].append("Plan preference not specified")
                    validation_results['valid'] = False
            
                if not self.appointment.customer_area:
                    validation_results['errors'].append("Customer area not provided")
                    validation_results['valid'] = False
            
                if not self.appointment.property_type:
                    validation_results['errors'].append("Property type not specified")
                    validation_results['valid'] = False
            
                if not self.appointment.scheduled_datetime:
                    validation_results['errors'].append("Appointment time not scheduled")
                    validation_results['valid'] = False
            
                # Check data quality
                if self.appointment.scheduled_datetime:
                    if self.appointment.scheduled_datetime <= timezone.now():
                        validation_results['errors'].append("Appointment time is in the past")
                        validation_results['valid'] = False
            
                if self.appointment.customer_name:
                    if not self.is_valid_name(self.appointment.customer_name):
                        validation_results['warnings'].append("Customer name may not be valid")
            
                return validation_results
            
            except Exception as e:
                print(f"Error validating information: {str(e)}")
                return {'valid': False, 'errors': [str(e)], 'warnings': []}


        def is_valid_name(self, name):
            """Validate if a string looks like a real person's name"""
            if not name or len(name.strip()) < 2:
                return False
        
            # Remove common non-name words
            name_clean = name.strip().lower()
            invalid_words = ['yes', 'no', 'ok', 'sure', 'thanks', 'hello', 'hi', 'good', 'fine', 
                            'sharp', 'cool', 'noted', 'great', 'alright', 'okay', 'perfect', 'nice']        
            if name_clean in invalid_words:
                return False
            
            # Check if it contains mostly letters and spaces
            if not re.match(r'^[a-zA-Z\s]+$', name):
                return False
            
            return True


        def parse_datetime(self, message):
            """Parse date and time from message - ENHANCED VERSION"""
            try:
                from datetime import datetime
                import pytz
                import re

                # Use South Africa timezone consistently
                sa_timezone = pytz.timezone('Africa/Johannesburg')
                now = timezone.now().astimezone(sa_timezone)

                # Day mapping
                day_mapping = {
                    'monday': 0, 'tuesday': 1, 'wednesday': 2, 'thursday': 3,
                    'friday': 4, 'saturday': 5, 'sunday': 6
                }

                message_lower = message.lower()
                appointment_date = None

                # Enhanced relative day detection
                for day_name, day_num in day_mapping.items():
                    # Look for patterns like "next Monday", "this Friday", "coming Tuesday"
                    patterns = [
                        rf'(next week|next)\s+{day_name}',
                        rf'(this|coming)\s+{day_name}',
                        rf'{day_name}(?!\s+(?:last|past))',  # Just the day name, not "last Monday"
                    ]
                
                    for pattern in patterns:
                        match = re.search(pattern, message_lower)
                        if match:
                            modifier = match.group(1) if match.groups() else None
                            base_day = now.weekday()
                            days_ahead = (day_num - base_day) % 7

                            if modifier in ['next', 'next week']:
                                days_ahead = days_ahead + 7 if days_ahead == 0 else days_ahead + 7
                            elif modifier in ['this', 'coming']:
                                if days_ahead == 0 and now.hour >= 12:  # If it's the same day but afternoon
                                    days_ahead = 7  # Next week
                                elif days_ahead == 0:
                                    days_ahead = 0  # Today
                            elif not modifier:  # Just "Monday"
                                if days_ahead == 0:  # If today is Monday
                                    if now.hour < 18:  # Before 6pm, could mean today
                                        days_ahead = 0
                                    else:  # After 6pm, probably next Monday
                                        days_ahead = 7
                                # If days_ahead > 0, it's this week

                            appointment_date = now + datetime.timedelta(days=days_ahead)
                            print(f"Parsed relative day: {day_name} with modifier '{modifier}' = {appointment_date.date()}")
                            break
                
                    if appointment_date:
                        break

                # Handle "tomorrow" and "today"
                if not appointment_date:
                    if 'tomorrow' in message_lower:
                        appointment_date = now + datetime.timedelta(days=1)
                        print(f"Parsed 'tomorrow' = {appointment_date.date()}")
                    elif 'today' in message_lower:
                        appointment_date = now
                        print(f"Parsed 'today' = {appointment_date.date()}")

                # Handle exact date formats with better patterns
                if not appointment_date:
                    date_patterns = [
                        r'(\d{1,2})[\/\-](\d{1,2})(?:[\/\-](\d{2,4}))?',  # 15/07, 15-07, 15/07/2025
                        r'(january|february|march|april|may|june|july|august|september|october|november|december)\s+(\d{1,2})(?:,?\s*(\d{4}))?',
                        r'(\d{1,2})(?:st|nd|rd|th)?\s+(january|february|march|april|may|june|july|august|september|october|november|december)(?:,?\s*(\d{4}))?',
                    ]

                    month_names = ['january', 'february', 'march', 'april', 'may', 'june',
                                'july', 'august', 'september', 'october', 'november', 'december']

                    for pattern in date_patterns:
                        date_match = re.search(pattern, message_lower)
                        if date_match:
                            try:
                                groups = date_match.groups()
                            
                                if '/' in pattern or '-' in pattern:  # DD/MM or DD-MM format
                                    day, month = int(groups[0]), int(groups[1])
                                    year = int(groups[2]) if groups[2] else now.year
                                    if year < 100:  # Handle 2-digit years
                                        year += 2000
                                else:  # Month name formats
                                    if groups[0].lower() in month_names:  # "January 15"
                                        month = month_names.index(groups[0].lower()) + 1
                                        day = int(groups[1])
                                        year = int(groups[2]) if groups[2] else now.year
                                    else:  # "15 January"
                                        day = int(groups[0])
                                        month = month_names.index(groups[1].lower()) + 1
                                        year = int(groups[2]) if groups[2] else now.year
                            
                                # Create appointment date
                                appointment_date = now.replace(year=year, month=month, day=day, hour=0, minute=0, second=0, microsecond=0)
                            
                                # If the date is in the past, assume next year
                                if appointment_date < now:
                                    appointment_date = appointment_date.replace(year=now.year + 1)
                            
                                print(f"Parsed exact date: {appointment_date.date()}")
                                break
                            
                            except (ValueError, IndexError) as e:
                                print(f"Date parsing error for pattern {pattern}: {str(e)}")
                                continue

                if not appointment_date:
                    print("No date found in message")
                    return None

                # Enhanced time parsing
                time_patterns = [
                    (r'(\d{1,2}):(\d{2})\s*(am|pm)', 'hh:mm am/pm'),
                    (r'(\d{1,2})\s*(am|pm)', 'hh am/pm'),
                    (r'(\d{1,2}):(\d{2})', 'hh:mm 24-hour'),
                ]

                time_found = False
                for pattern, description in time_patterns:
                    time_match = re.search(pattern, message_lower)
                    if time_match:
                        groups = time_match.groups()
                    
                        if len(groups) >= 3 and groups[2]:  # Has AM/PM
                            hour = int(groups[0])
                            minute = int(groups[1]) if len(groups) > 1 and groups[1] else 0
                            am_pm = groups[2]
                        
                            # Convert to 24-hour time
                            if am_pm == 'pm' and hour != 12:
                                hour += 12
                            elif am_pm == 'am' and hour == 12:
                                hour = 0
                            
                        elif len(groups) >= 2 and groups[1] and groups[1] in ['am', 'pm']:  # Just hour with AM/PM
                            hour = int(groups[0])
                            minute = 0
                            am_pm = groups[1]
                        
                            if am_pm == 'pm' and hour != 12:
                                hour += 12
                            elif am_pm == 'am' and hour == 12:
                                hour = 0
                            
                        else:  # 24-hour format
                            hour = int(groups[0])
                            minute = int(groups[1]) if len(groups) > 1 and groups[1] else 0

                        # Validate time
                        if 0 <= hour <= 23 and 0 <= minute <= 59:
                            appointment_date = appointment_date.replace(hour=hour, minute=minute, second=0, microsecond=0)
                            time_found = True
                            print(f"Parsed time using {description}: {hour:02d}:{minute:02d}")
                            break
                        else:
                            print(f"Invalid time values: hour={hour}, minute={minute}")

                if not time_found:
                    print("No valid time found in message")
                    return None

                print(f"Final parsed datetime: {appointment_date}")
                return appointment_date

            except Exception as e:
                print(f"DateTime parsing error: {str(e)}")
                return None

