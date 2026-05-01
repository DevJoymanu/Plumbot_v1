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


class RescheduleMixin:
        def detect_reschedule_request_with_ai(self, message):
            """Use AI to intelligently detect rescheduling requests"""
            try:
                # Only check for reschedule if appointment is already confirmed
                if self.appointment.status != 'confirmed' or not self.appointment.scheduled_datetime:
                    return False
                
                current_appt = self.appointment.scheduled_datetime.strftime('%A, %B %d at %I:%M %p')
            
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
            
                # Get current appointment info
                current_appt = self.appointment.scheduled_datetime
                current_appt_str = current_appt.strftime('%A, %B %d at %I:%M %p')
            
                # Try to extract new datetime
                new_datetime = self.parse_datetime_with_ai(message)
            
                if new_datetime:
                    # Check availability
                    is_available, conflict = self.check_appointment_availability(new_datetime)
                
                    if is_available:
                        return self.process_successful_reschedule(current_appt, new_datetime)
                    else:
                        return self.handle_unavailable_reschedule_with_ai(new_datetime, message)
                else:
                    return self.request_reschedule_clarification_with_ai(current_appt_str, message)
                
            except Exception as e:
                print(f"❌ AI reschedule handling error: {str(e)}")
                return "I'd like to help you reschedule, but I'm having some technical difficulties. Could you call us at (555) PLUMBING to reschedule?"


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

                datetime_extraction_prompt = f"""You are a datetime extraction assistant for appointment scheduling.

        TASK: Extract a complete date and time from the customer's message and convert it to YYYY-MM-DDTHH:MM format.

        CURRENT CONTEXT:
        - Current datetime: {current_time.strftime('%Y-%m-%d %H:%M')} (Africa/Johannesburg, UTC+2)
        - Business hours: 08:00–18:00
        - Working days: Sunday through Friday (Saturday is CLOSED)
        - Today is: {today_date_str} ({current_time.strftime('%A')})

        NEXT OCCURRENCE OF EACH DAY:
        - Monday: {next_days['monday']}
        - Tuesday: {next_days['tuesday']}
        - Wednesday: {next_days['wednesday']}
        - Thursday: {next_days['thursday']}
        - Friday: {next_days['friday']}
        - Saturday: {next_days['saturday']} (CLOSED — do NOT use)
        - Sunday: {next_days['sunday']}
        - Tomorrow: {tomorrow_date_str}

        EXTRACTION RULES:
        1. Return a complete datetime ONLY if BOTH date AND time are clearly specified.
        2. "Saturday" → return UNAVAILABLE (we are closed Saturdays)
        3. "Sunday" → use Sunday date above, valid working day
        4. "tomorrow" → {tomorrow_date_str}
        5. "today" → {today_date_str}
        6. Time formats: "2pm"=14:00, "10am"=10:00, "2:30pm"=14:30, "14:00"=14:00
        7. Default minutes to 00 if not specified.
        8. Do NOT adjust timezone — return local Zimbabwe time.

        RESPONSE FORMAT (return ONLY one of these, no other text):
        - Complete datetime: YYYY-MM-DDTHH:MM
        - Saturday requested: SATURDAY_CLOSED
        - Only partial info (missing date OR time): PARTIAL_INFO
        - No datetime found: NOT_FOUND

        CUSTOMER MESSAGE: "{message}"
        EXTRACTED DATETIME:"""

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a precise datetime extraction assistant. Return ONLY the format specified — a datetime string like 2025-11-03T14:00, or one of: SATURDAY_CLOSED, PARTIAL_INFO, NOT_FOUND."
                        },
                        {"role": "user", "content": datetime_extraction_prompt}
                    ],
                    temperature=0.1,
                    max_tokens=30
                )

                ai_response = response.choices[0].message.content.strip()
                print(f"🤖 DeepSeek datetime extraction: '{message}' → {ai_response}")

                if ai_response == "SATURDAY_CLOSED":
                    print("⚠️ Customer requested Saturday — closed")
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
            """Use AI to generate response when requested time is unavailable"""
            try:
                # Get alternative suggestions
                alternatives = self.get_alternative_time_suggestions(requested_datetime)
            
                unavailable_response_prompt = f"""
                You a professional appointment assistant for a plumbing company.
            
                SITUATION: Customer requested to reschedule to a time that's not available.
            
                CONTEXT:
                - Customer requested: {requested_datetime.strftime('%A, %B %d at %I:%M %p')}
                - This time is unavailable (conflict with another appointment)
                - Alternative times available: {[alt['display'] for alt in alternatives] if alternatives else 'None immediately available'}
            
                TASK: Write a professional, helpful response that:
                1. Politely explains the requested time isn't available
                2. Offers the alternative times if available
                3. Asks customer to choose an alternative or suggest another time
                4. Maintains friendly, professional tone
                5. Keep it concise (2-3 sentences max)
            
                RESPONSE STYLE:
                - Professional but warm
                - No humor or jokes
                - Direct and clear
                - Use "That time isn't available" rather than technical explanations
            
                Generate the response:"""
            
                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": "You are a professional appointment assistant. Be helpful and concise."},
                        {"role": "user", "content": unavailable_response_prompt}
                    ],
                    temperature=0.7,
                    max_tokens=150
                )
            
                ai_response = response.choices[0].message.content.strip()
                print(f"🤖 AI generated unavailable response")
                return ai_response
            
            except Exception as e:
                print(f"❌ AI unavailable response error: {str(e)}")
                # Fallback response
                if alternatives:
                    alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                    return f"That time isn't available. Here are some alternatives:\n{alt_text}\n\nWhich works better for you?"
                else:
                    return "That time isn't available. Could you suggest another time? Our hours are 8 AM - 6 PM, Monday to Friday."


        def request_reschedule_clarification_with_ai(self, current_appt_str, message):
            """Use AI to generate clarification request when datetime parsing fails"""
            try:
                clarification_prompt = f"""
                You are a professional appointment assistant for a plumbing company.
            
                SITUATION: Customer wants to reschedule but didn't provide clear date/time information.
            
                CONTEXT:
                - Customer's current appointment: {current_appt_str}
                - Customer message: "{message}"
                - Need both date AND time to reschedule
            
                TASK: Write a professional response that:
                1. Acknowledges their reschedule request
                2. Mentions their current appointment time
                3. Asks for specific new date AND time
                4. Provides example format ("Monday at 2pm", "tomorrow at 10am")
                5. Keep it concise and helpful
            
                RESPONSE STYLE:
                - Professional and clear
                - No humor or excessive friendliness
                - Direct request for information
                - Include current appointment for reference
            
                Generate the response:"""
            
                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": "You are a professional appointment assistant. Be clear and helpful."},
                        {"role": "user", "content": clarification_prompt}
                    ],
                    temperature=0.7,
                    max_tokens=100
                )
            
                ai_response = response.choices[0].message.content.strip()
                print(f"🤖 AI generated clarification request")
                return ai_response
            
            except Exception as e:
                print(f"❌ AI clarification error: {str(e)}")
                # Fallback response
                return f"I understand you'd like to reschedule your appointment currently scheduled for {current_appt_str}. When would you prefer to reschedule to? Please provide both the day and time (e.g., 'Monday at 2pm', 'tomorrow at 10am')."


        def process_successful_reschedule(self, old_datetime, new_datetime):
            """Process a successful reschedule and generate confirmation"""
            try:
                # Update appointment
                self.appointment.scheduled_datetime = new_datetime
                if hasattr(self.appointment, 'reschedule_count'):
                    self.appointment.reschedule_count = (self.appointment.reschedule_count or 0) + 1
                if hasattr(self.appointment, 'original_datetime') and not self.appointment.original_datetime:
                    self.appointment.original_datetime = old_datetime
                self.appointment.save()
            
                # Update Google Calendar
                try:
                    self.update_google_calendar_appointment(old_datetime, new_datetime)
                except Exception as cal_error:
                    print(f"Calendar update error: {str(cal_error)}")
            
                # Notify team
                try:
                    self.notify_team_about_reschedule(old_datetime, new_datetime)
                except Exception as team_error:
                    print(f"Team notification error: {str(team_error)}")
            
                # Generate confirmation with AI
                confirmation_prompt = f"""
                You are a professional appointment assistant for a plumbing company.
            
                SITUATION: Successfully rescheduled customer's appointment.
            
                DETAILS:
                - Customer: {self.appointment.customer_name or 'Customer'}
                - Old appointment: {old_datetime.strftime('%A, %B %d at %I:%M %p')}
                - New appointment: {new_datetime.strftime('%A, %B %d at %I:%M %p')}
                - Service: {self.appointment.project_type or 'Plumbing service'}
                - Area: {self.appointment.customer_area or 'Your area'}
            
                TASK: Write a professional confirmation message that:
                1. Confirms the reschedule
                2. Shows the new appointment time clearly
                3. Mentions the team will contact them before arrival
                4. Offers help if they need to change again
                5. Professional, reassuring tone
            
                Keep it concise and clear.
            
                Generate the confirmation:"""
            
                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": "You are a professional appointment assistant. Be reassuring and clear."},
                        {"role": "user", "content": confirmation_prompt}
                    ],
                    temperature=0.7,
                    max_tokens=150
                )
            
                ai_confirmation = response.choices[0].message.content.strip()
                print(f"✅ Successful reschedule processed with AI confirmation")
                return ai_confirmation
            
            except Exception as e:
                print(f"❌ Error processing successful reschedule: {str(e)}")
                # Fallback confirmation
                return f"✅ Appointment rescheduled to {new_datetime.strftime('%A, %B %d at %I:%M %p')}. Our team will contact you before arrival."


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

