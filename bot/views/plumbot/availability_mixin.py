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


class AvailabilityMixin:
        def _parse_time_only_for_selected_date(self, message: str):
            """
            Parse a time-only reply like '2pm' or '14:00' against the appointment's
            already-selected date.
            """
            base_dt = self.appointment.scheduled_datetime
            if not base_dt:
                return None

            msg = (message or '').strip().lower()
            if not msg:
                return None

            selected_date = self._get_selected_local_date()
            if not selected_date:
                return None

            sa_tz = pytz.timezone('Africa/Johannesburg')

            bare_hour_match = re.fullmatch(r'(\d{1,2})', msg)
            if bare_hour_match:
                chosen_hour = int(bare_hour_match.group(1))
                offered_times = self._get_two_available_times_for_date(selected_date)
                matching_slots = []
                for slot in offered_times:
                    local_slot = slot.astimezone(sa_tz) if slot.tzinfo else sa_tz.localize(slot)
                    if local_slot.strftime('%I').lstrip('0') == str(chosen_hour):
                        matching_slots.append(local_slot)
                if len(matching_slots) == 1:
                    return matching_slots[0]

            time_patterns = [
                r'(\d{1,2}):(\d{2})\s*(am|pm)',
                r'(\d{1,2})\s*(am|pm)',
                r'(\d{1,2}):(\d{2})',
            ]

            for pattern in time_patterns:
                match = re.search(pattern, msg)
                if not match:
                    continue

                groups = match.groups()
                if len(groups) >= 3 and groups[2]:
                    hour = int(groups[0])
                    minute = int(groups[1]) if groups[1] and groups[1].isdigit() else 0
                    am_pm = groups[2]
                    if am_pm == 'pm' and hour != 12:
                        hour += 12
                    elif am_pm == 'am' and hour == 12:
                        hour = 0
                elif len(groups) >= 2 and groups[1] in ['am', 'pm']:
                    hour = int(groups[0])
                    minute = 0
                    am_pm = groups[1]
                    if am_pm == 'pm' and hour != 12:
                        hour += 12
                    elif am_pm == 'am' and hour == 12:
                        hour = 0
                else:
                    hour = int(groups[0])
                    minute = int(groups[1]) if len(groups) > 1 and groups[1] else 0

                if 0 <= hour <= 23 and 0 <= minute <= 59:
                    return sa_tz.localize(
                        datetime.combine(
                            selected_date,
                            datetime.min.time().replace(hour=hour, minute=minute),
                        )
                    )

            return None


        def _get_selected_local_date(self):
            """Return the appointment's selected day in Johannesburg local time."""
            dt = self.appointment.scheduled_datetime
            if not dt:
                return None
            sa_tz = pytz.timezone('Africa/Johannesburg')
            local_dt = dt.astimezone(sa_tz) if dt.tzinfo else sa_tz.localize(dt)
            return local_dt.date()


        def _get_next_two_available_days(self) -> list:
            """
            Return the next two calendar dates (as datetime.date objects) that:
            - Are not Saturday (our only closed day)
            - Are in the future (from tomorrow onwards)
            """
            import pytz
            from datetime import timedelta
            sa_tz = pytz.timezone('Africa/Johannesburg')
            today = timezone.now().astimezone(sa_tz).date()
            results = []
            check = today + timedelta(days=1)
            while len(results) < 2:
                if check.weekday() != 5:   # 5 = Saturday
                    results.append(check)
                check += timedelta(days=1)
            return results


        def _get_two_available_times_for_date(self, date_obj) -> list:
            """
            Return two available time slots (as datetime objects, timezone-aware)
            for a given date.  Checks against existing confirmed appointments.
            Prefers 9 AM and 2 PM; falls back to next available business-hours slots.
            """
            import pytz
            from datetime import datetime as dt, timedelta
            sa_tz = pytz.timezone('Africa/Johannesburg')
            preferred_hours = [9, 14, 10, 11, 13, 15, 16]
            results = []
            for h in preferred_hours:
                candidate = sa_tz.localize(dt.combine(date_obj, dt.min.time().replace(hour=h)))
                if candidate <= timezone.now():
                    continue
                is_avail, _ = self.check_appointment_availability(candidate)
                if is_avail:
                    results.append(candidate)
                if len(results) == 2:
                    break
            return results


        def check_appointment_availability(self, requested_datetime):
            """Check if requested time slot is available"""
            try:
                # Ensure timezone awareness
                if requested_datetime.tzinfo is None:
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    requested_datetime = sa_timezone.localize(requested_datetime)
            
                # Define appointment duration (default 2 hours)
                appointment_duration = timedelta(hours=2)
                requested_end = requested_datetime + appointment_duration
            
                print(f"Checking availability for: {requested_datetime} to {requested_end}")
            
                # 1. Check if it's not in the past (with 1-hour buffer)
                now = timezone.now()
                min_booking_time = now + timedelta(hours=1)
            
                if requested_datetime <= min_booking_time:
                    print(f"Requested time is too soon: {requested_datetime} vs minimum {min_booking_time}")
                    return False, "too_soon"
            
                # 2. Check business days (Monday-Friday)
                # Check business days (Sunday-Friday, Saturday closed)
                weekday = requested_datetime.weekday()  # 0=Monday, 6=Sunday
                if weekday == 5:  # Only Saturday (5) is closed
                    print(f"Requested time is on Saturday (closed): weekday {weekday}")
                    # ✅ Clear the invalid datetime so it doesn't loop on every message
                    self.appointment.scheduled_datetime = None
                    if self._appointment_has_field('retry_count'):
                        self.appointment.save(update_fields=['retry_count'])
                    return False, "saturday_closed"

                # 3. Check business hours (8 AM - 6 PM)
                hour = requested_datetime.hour
                if hour < 8 or hour >= 18:
                    print(f"Outside business hours: {hour}:00 (business hours: 8 AM - 6 PM)")
                    return False, "outside_business_hours"
            
                # 4. Check if appointment would end after business hours
                if requested_end.hour > 18 or (requested_end.hour == 18 and requested_end.minute > 0):
                    print(f"Appointment would end after business hours: {requested_end}")
                    return False, "ends_after_hours"
            
                # 5. Check for conflicts with other confirmed appointments
                conflicting_appointments = Appointment.objects.filter(
                    status='confirmed',
                    scheduled_datetime__isnull=False
                ).exclude(
                    id=self.appointment.id  # Exclude current appointment for reschedules
                )
            
                for existing_appt in conflicting_appointments:
                    # Ensure existing appointment is timezone-aware
                    if existing_appt.scheduled_datetime.tzinfo is None:
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        existing_start = sa_timezone.localize(existing_appt.scheduled_datetime)
                    else:
                        existing_start = existing_appt.scheduled_datetime
                    
                    existing_end = existing_start + appointment_duration
                
                    # Check for time overlap
                    if (requested_datetime < existing_end and requested_end > existing_start):
                        print(f"Conflict found with appointment {existing_appt.id}")
                        print(f"Existing: {existing_start} to {existing_end}")
                        print(f"Requested: {requested_datetime} to {requested_end}")
                        return False, existing_appt
            
                # 6. Check maximum advance booking (3 months)
                max_advance_time = now + timedelta(days=90)
                if requested_datetime > max_advance_time:
                    print(f"Too far in advance: {requested_datetime} vs maximum {max_advance_time}")
                    return False, "too_far_ahead"
            
                print(f"✅ Time slot is available: {requested_datetime}")
                return True, None
            
            except Exception as e:
                print(f"❌ Error checking availability: {str(e)}")
                return False, "error"


        def get_alternative_time_suggestions(self, requested_datetime):
            """Get alternative available time slots near the requested time"""
            try:
                suggestions = []
            
                # Get the requested date and time
                requested_date = requested_datetime.date()
            
                # Business time slots (8am, 10am, 12pm, 2pm, 4pm)
                business_time_slots = [8, 10, 12, 14, 16]
            
                print(f"Looking for alternatives near {requested_datetime}")
            
                # Try same day first, then next few business days
                for day_offset in range(0, 5):  # Check today + next 4 days
                    check_date = requested_date + timedelta(days=day_offset)
                
                    # This one is actually correct already — but double-check the one
                    # inside find_next_available_slots which has:
                    if check_date.weekday() == 5:   # ← Skip Saturday only
                        continue
                    
                    for hour in business_time_slots:
                        candidate_time = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        candidate_datetime = sa_timezone.localize(candidate_time)
                    
                        # Skip times in the past
                        if candidate_datetime <= timezone.now():
                            continue
                    
                        # Skip the exact requested time
                        if candidate_datetime == requested_datetime:
                            continue
                    
                        is_available, conflict = self.check_appointment_availability(candidate_datetime)
                        if is_available:
                            day_type = 'same_day' if day_offset == 0 else 'next_days'
                            suggestions.append({
                                'datetime': candidate_datetime,
                                'display': candidate_datetime.strftime('%A, %B %d at %I:%M %p'),
                                'day_type': day_type
                            })
                        
                            # Limit to 4 suggestions
                            if len(suggestions) >= 4:
                                break
                
                    if len(suggestions) >= 4:
                        break
            
                print(f"Found {len(suggestions)} alternative time suggestions")
                return suggestions
            
            except Exception as e:
                print(f"Error getting alternative suggestions: {str(e)}")
                return []


        def format_datetime_for_display(self, dt):
            """Format datetime ensuring it shows in  timezone"""
            try:
                import pytz
            
                # Ensure datetime is timezone-aware
                if dt.tzinfo is None:
                    # If naive, assume it's already in SA time
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    dt = sa_timezone.localize(dt)
                else:
                    # If aware, convert to SA timezone
                    sa_timezone = pytz.timezone('Africa/Johannesburg')
                    dt = dt.astimezone(sa_timezone)
            
                return dt
            
            except Exception as e:
                print(f"Error formatting datetime: {str(e)}")
                return dt


        def find_next_available_slots(self, preferred_datetime, num_suggestions=4):
            """Find the next available appointment slots after the preferred time"""
            try:
                suggestions = []
                current_check = preferred_datetime
                max_days_ahead = 14  # Look up to 2 weeks ahead
            
                # Time slots to check (every 2 hours during business hours)
                business_hours = [8, 10, 12, 14, 16]  # 8am, 10am, 12pm, 2pm, 4pm
            
                days_checked = 0
                while len(suggestions) < num_suggestions and days_checked < max_days_ahead:
                    check_date = current_check.date()
                
                    # Skip weekends
                    # Skip Saturday only (Sunday is open)
                    if check_date.weekday() != 5:
                        for hour in business_hours:
                            check_datetime = datetime.combine(check_date, datetime.min.time().replace(hour=hour))
                            sa_timezone = pytz.timezone('Africa/Johannesburg')
                            check_datetime = sa_timezone.localize(check_datetime)
                        
                            # Only check times after the preferred time
                            if check_datetime > preferred_datetime:
                                is_available, conflict = self.check_appointment_availability(check_datetime)
                            
                                if is_available:
                                    suggestions.append({
                                        'datetime': check_datetime,
                                        'display': check_datetime.strftime('%A, %B %d at %I:%M %p'),
                                        'day_type': 'weekday'
                                    })
                                
                                    if len(suggestions) >= num_suggestions:
                                        break
                
                    # Move to next day
                    current_check += timedelta(days=1)
                    days_checked += 1
            
                return suggestions
            
            except Exception as e:
                print(f"Error finding available slots: {str(e)}")
                return []


        def is_business_day(self, check_date):
            """Check if a given date is a business day (Sunday-Friday)"""
            weekday = check_date.weekday()
            return weekday != 5  # All days except Saturday (5)


        def is_business_hours(self, check_time):
            """Check if a given time is within business hours (8 AM - 6 PM)"""
            hour = check_time.hour
            return 8 <= hour < 18


        def get_business_day_name(self, date_obj):
            """Get user-friendly day name with business context"""
            weekday = date_obj.weekday()
            day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        
            if weekday < 5:
                return day_names[weekday]
            else:
                    return f"{day_names[weekday]} (Weekend - Closed)"


        def format_availability_response(self, alternatives, requested_time_str=None):
            """Format alternative time suggestions into a user-friendly message"""
            try:
                if not alternatives:
                    return "I'm having trouble finding available alternatives. Could you suggest a different day or time? Our hours are 8 AM - 6 PM, Monday to Friday."
            
                # Group by day type for better formatting
                same_day = [alt for alt in alternatives if alt['day_type'] == 'same_day']
                next_days = [alt for alt in alternatives if alt['day_type'] == 'next_days']
            
                message_parts = []
            
                if requested_time_str:
                    message_parts.append(f"That time ({requested_time_str}) isn't available.")
                else:
                    message_parts.append("That time isn't available.")
            
                message_parts.append("\nHere are some alternatives:")
            
                # Format same day options
                if same_day:
                    message_parts.append("\n📅 Same day options:")
                    for alt in same_day:
                        time_only = alt['datetime'].strftime('%I:%M %p')
                        message_parts.append(f"• {time_only}")
            
                # Format next days options  
                if next_days:
                    message_parts.append("\n📅 Other days:")
                    for alt in next_days:
                        message_parts.append(f"• {alt['display']}")
            
                message_parts.append("\nWhich time works best for you?")
            
                return "".join(message_parts)
            
            except Exception as e:
                print(f"Error formatting availability response: {str(e)}")
                return "That time isn't available. Please suggest another time."


        def get_availability_error_message(self, error_type, conflict_appointment=None):
            """Generate user-friendly error messages for availability issues"""
            try:
                if error_type == "past_time":
                    return "That time has already passed. Please choose a future time."
                #
                elif error_type == "saturday_closed":
                    return "We're closed on Saturdays. Please choose Sunday through Friday."

                elif error_type == "outside_business_hours":
                    return "We're only available 8 AM to 6 PM, Monday through Friday. Please choose a time within business hours."
            
                elif error_type == "ends_after_hours":
                    return "That appointment would run past our closing time (6 PM). Please choose an earlier time slot."
            
                elif error_type == "insufficient_notice":
                    return "We need at least 2 hours advance notice for appointments. Please choose a time further in the future."
            
                elif error_type == "too_far_ahead":
                    return "We can only book appointments up to 3 months in advance. Please choose a sooner date."
            
                elif error_type == "error":
                    return "There was a technical issue checking availability. Please try a different time or call us."
            
                elif isinstance(conflict_appointment, Appointment):
                    conflict_time = conflict_appointment.scheduled_datetime.strftime('%I:%M %p')
                    customer_name = conflict_appointment.customer_name or "another customer"
                    return f"That time conflicts with an appointment for {customer_name} at {conflict_time}."
            
                else:
                    return "That time slot isn't available. Please choose a different time."
                
            except Exception as e:
                print(f"Error generating availability message: {str(e)}")
                return "That time isn't available. Please choose a different time."

