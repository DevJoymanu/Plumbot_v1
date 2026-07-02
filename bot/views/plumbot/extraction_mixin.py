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

from ...services.lead_scoring import refresh_lead_score

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


class ExtractionMixin:
        def get_appointment_context(self):
            """Get current appointment data to provide context to AI"""
            try:
                context_parts = []
            
                if self.appointment.customer_name:
                    context_parts.append(f"Customer Name: {self.appointment.customer_name}")
                else:
                    context_parts.append("Customer Name: Not provided yet")
                
                if self.appointment.customer_area:
                    context_parts.append(f"Area: {self.appointment.customer_area}")
                else:
                    context_parts.append("Area: Not provided yet")
                
                if self.appointment.project_type:
                    context_parts.append(f"Service Type: {self.appointment.project_type}")
                else:
                    context_parts.append("Service Type: Not specified yet")
                
                if self.appointment.has_plan is True:
                    context_parts.append("Plan Status: Customer has existing plan")
                elif self.appointment.has_plan is False:
                    context_parts.append("Plan Status: Customer wants site visit")
                else:
                    context_parts.append("Plan Status: Not specified yet")
                
                if self.appointment.property_type:
                    context_parts.append(f"Property Type: {self.appointment.property_type}")
                else:
                    context_parts.append("Property Type: Not specified yet")
                
                if self.appointment.timeline:
                    context_parts.append(f"Timeline: {self.appointment.timeline}")
                else:
                    context_parts.append("Timeline: Not specified yet")
                
                context_parts.append(f"Current Status: {self.appointment.get_status_display()}")
            
                # ✅ FIX: Check if scheduled_datetime exists before calling astimezone
                if self.appointment.scheduled_datetime:
                    try:
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        sa_time = self.appointment.scheduled_datetime.astimezone(sa_timezone)
                        formatted_datetime = sa_time.strftime('%A, %B %d, %Y at %I:%M %p')
                        context_parts.append(f"Scheduled: {formatted_datetime}")
                        context_parts.append(f" CRITICAL: When mentioning appointment time, ALWAYS use: {formatted_datetime}")
                    except Exception as dt_error:
                        print(f"⚠️ Error formatting scheduled datetime: {dt_error}")
                        context_parts.append("Scheduled: Error reading datetime")
                else:
                    context_parts.append("Scheduled: No appointment time set yet")
                
                next_question = self.get_next_question_to_ask()
                context_parts.append(f"Next Question Needed: {next_question}")
            
                retry_count = self._get_question_retry_count(next_question)
                context_parts.append(f"Question Retry Count: {retry_count}")
            
                completeness = self.appointment.get_customer_info_completeness()
                context_parts.append(f"Info Completeness: {completeness:.0f}%")

                return "\n".join(context_parts)
            
            except Exception as e:
                print(f"Error getting appointment context: {str(e)}")
                return "Unable to load appointment context"


        def get_information_summary(self):
            """Get a summary of collected information for debugging"""
            try:
                summary = {
                    'service_type': self.appointment.project_type,
                    'has_plan': self.appointment.has_plan,
                    'area': self.appointment.customer_area,
                    'timeline': self.appointment.timeline,
                    'property_type': self.appointment.property_type,
                    'scheduled_datetime': self.appointment.scheduled_datetime.isoformat() if self.appointment.scheduled_datetime else None,
                    'customer_name': self.appointment.customer_name,
                    'status': self.appointment.status,
                    'completion_percentage': self.smart_booking_check()['completion_percentage']
                }
                return summary
            except Exception as e:
                print(f"Error getting info summary: {str(e)}")
                return {}


        def extract_appointment_details(self):
            """Extract customer details from appointment data"""
            try:
                details = {}
            
                # Use existing appointment data
                if self.appointment.customer_name:
                    details['name'] = self.appointment.customer_name
                if self.appointment.customer_area:
                    details['area'] = self.appointment.customer_area
                if self.appointment.project_type:
                    details['project_type'] = self.appointment.project_type
                if self.appointment.property_type:
                    details['property_type'] = self.appointment.property_type
                if self.appointment.timeline:
                    details['timeline'] = self.appointment.timeline
                if self.appointment.has_plan is not None:
                    details['has_plan'] = self.appointment.has_plan

                return details
            
            except Exception as e:
                print(f"Error extracting appointment details: {str(e)}")
                return {}


        def extract_all_available_info_with_ai(self, message):
            """Extract ALL possible appointment information from any message - FIXED TO PREVENT RE-ASKING"""
            try:
                # Get current appointment state for context
                current_context = self.get_appointment_context()
                next_question = self.get_next_question_to_ask()
            
                # Format current time properly
                current_time = timezone.now().strftime('%Y-%m-%d %H:%M')
            
                extraction_prompt = f"""
                You are a comprehensive data extraction assistant for a plumbing appointment system.
                Customers may write in English, Shona, or mixed language.
            
                CRITICAL: You MUST return ONLY a valid JSON object with no markdown formatting, code blocks, or extra text.
            
                TASK: Extract information from the customer's message and return ONLY what you can clearly identify.
            
                CURRENT APPOINTMENT STATE:
                {current_context}
            
                NEXT QUESTION WE NEED: {next_question}
            
                CUSTOMER MESSAGE: "{message}"
            
                EXTRACTION RULES:
                1. ONLY extract information that is CLEARLY and EXPLICITLY present in the message
                2. DO NOT GUESS or ASSUME - if not explicitly stated, return null
                3. PRESERVE existing information - do NOT set fields to null if they already have values
                4. Return ONLY a JSON object - no markdown, no explanations, no code blocks
                5. For plan_status: ONLY extract if we are ACTIVELY ASKING about the plan RIGHT NOW
            
                EXTRACTION TARGETS:
            
                SERVICE TYPE - Look for:
                - English keywords: bathroom, kitchen, plumbing, installation, renovation, repair, toilet, shower, sink
                - Shona/mixed keywords: chimbuzi (toilet), shawa (shower), bhavhu/bhavu (bathtub),
                  bheseni (basin/sink), kicheni (kitchen), mapombi (pipes), imba itsva (new house)
                - Return: "bathroom_renovation", "kitchen_renovation",
                  "bathroom_and_kitchen_renovation", or "new_plumbing_installation"
                - Return "bathroom_and_kitchen_renovation" when customer indicates BOTH bathroom AND
                  kitchen (e.g. "both", "both renovations", "kitchen and bathroom", "both rooms")
            
                PROJECT DESCRIPTION - Look for specific details of what the customer wants done,
                including renovation state clues like "already tiled", "new build", "existing bathroom",
                "from scratch", "walls done", "rough plumbing done".
                Capture verbatim where possible because these details affect pricing and the plumber's approach.
                - Return: the description as a string (max 300 chars), or null
            
            
                PLAN STATUS - ULTRA CRITICAL - STRICT EXTRACTION RULES:
            
                WHEN TO EXTRACT:
                - ONLY if next_question = "plan_or_visit" (we are actively asking about plan)
                - ONLY if customer is DIRECTLY answering the plan question in THIS message
                - NEVER extract from general conversation, greetings, or other topics
            
                CURRENT QUESTION CHECK: {next_question}
            
                IF next_question IS NOT "plan_or_visit":
                - ALWAYS return null for plan_status
                - Do NOT try to infer plan status from any message
                - This prevents re-asking questions already answered
            
                IF next_question IS "plan_or_visit":
                YES indicators (customer HAS plan):
                - Direct: "yes", "yeah", "yep", "i do", "i have", "got plan", "have plan"
                - Future: "will send", "i'll send", "send later", "let me send"
                - Shona/mixed: "hongu", "ehe", "ndine plan", "ndinayo plan", "tine plan",
                  "ndine blueprint", "ndinayo blueprint", "ndine mapepa"
            
                NO indicators (customer needs site visit):
                - Direct: "no", "nope", "don't have", "no plan", "need visit", "site visit"
                - Shona/mixed: "kwete", "handina plan", "hapana plan", "sina plan",
                  "mauye muone", "uyai muone", "tiuye muone", "shanyira"
            
                IF IN DOUBT: Return null (better to ask again than assume wrong answer)
            
                AREA/LOCATION - Look for:
                - Any location names, suburbs, areas mentioned
                - Return: the area name as stated
                - Examples of Zimbabwe suburbs: Hatfield, Avondale, Borrowdale, Mabvuku,
                  Budiriro, Kuwadzana, Ziko, Dzivarasekwa, Highfields, Glen Norah,
                  Waterfalls, Mbare, Greendale, Msasa, Tynwald, Eastlea, Highlands,
                  Marlborough, Mount Pleasant, Ruwa, Chitungwiza
                - IMPORTANT: When next_question is "area", treat ANY short word or
                  unfamiliar phrase as an area name, NOT as a customer name.

                TIMELINE - Look for:
                - When they want work done: ASAP, next week, next month, tomorrow, etc.
                - Return: timeline as stated
            
                PROPERTY TYPE - Look for:
                - English keywords: house, home, apartment, flat, business, office, commercial, shop, store
                - Shona/mixed keywords: imba (house/home), bhizimisi (business), shopu (shop)
                - Return: "house", "apartment", or "business"
            
                AVAILABILITY/DATETIME - Look for:
                - Complete date and time information
                - Handle: "Monday at 2pm", "tomorrow at 10am", "15th July at 14:00"
                - Return: YYYY-MM-DDTHH:MM format
            
                CUSTOMER NAME - Look for:
                - Patterns: "I'm John", "my name is Sarah", "call me Mike"
                - Return: full name in title case
                - IMPORTANT: When next_question is "area", do NOT extract customer_name —
                  any short word in that context is a suburb name, not a person's name
            
                RESPONSE FORMAT (CRITICAL):
                Return EXACTLY this JSON structure with no additional text:
                {{
                    "service_type": "extracted_value_or_null",
                    "project_description": "extracted_value_or_null",
                    "plan_status": "extracted_value_or_null", 
                    "area": "extracted_value_or_null",
                    "timeline": "extracted_value_or_null",
                    "property_type": "extracted_value_or_null",
                    "availability": "extracted_value_or_null",
                    "customer_name": "extracted_value_or_null"
                }}
            
                CURRENT DATE: {current_time}
            
                Extract from: "{message}"
                """
            
                _extraction_client = deepseek_client
                _extraction_model  = settings.DEEPSEEK_MODEL
                response = _extraction_client.chat.completions.create(
                    model=_extraction_model,
                    messages=[
                        {"role": "system", "content": "You are a data extraction assistant. Return ONLY valid JSON with no formatting or explanations. NEVER extract plan_status unless actively asking about it RIGHT NOW."},
                        {"role": "user", "content": extraction_prompt}
                    ],
                    temperature=0.1,
                    max_tokens=500,
                    response_format={"type": "json_object"},
                )

                ai_response = response.choices[0].message.content.strip()

                # If DeepSeek returned empty despite response_format, retry once
                if not ai_response:
                    import time as _time
                    _time.sleep(0.5)
                    _retry = _extraction_client.chat.completions.create(
                        model=_extraction_model,
                        messages=[
                            {"role": "system", "content": "You are a data extraction assistant. Return ONLY valid JSON with no formatting or explanations. NEVER extract plan_status unless actively asking about it RIGHT NOW."},
                            {"role": "user", "content": extraction_prompt}
                        ],
                        temperature=0.1,
                        max_tokens=500,
                        response_format={"type": "json_object"},
                    )
                    ai_response = _retry.choices[0].message.content.strip()
                    if ai_response:
                        print("🔄 Extraction retry succeeded")

                # Clean up the response to handle markdown formatting
                ai_response = ai_response.replace('```json', '').replace('```', '').strip()

                # Parse AI response as JSON
                try:
                    extracted_data = json.loads(ai_response)
                    print(f"🤖 AI extracted data: {extracted_data}")
                    # Shona fallback: plan_or_visit responses (only when actively asking)
                    if next_question == "plan_or_visit" and not extracted_data.get("plan_status"):
                        msg = (message or "").lower().strip()
                        has_plan_terms = [
                            "hongu", "ehe", "ndine plan", "ndinayo plan", "tine plan",
                            "ndine blueprint", "ndinayo blueprint", "ndine mapepa",
                            "ndine drawing", "ndinayo drawing", "ndine maplani",
                        ]
                        needs_visit_terms = [
                            "kwete", "handina plan", "hapana plan", "sina plan",
                            "mauye muone", "uyai muone", "tiuye muone", "shanyira",
                            "site visit", "come see", "come and see",
                        ]
                        if any(term in msg for term in has_plan_terms):
                            extracted_data["plan_status"] = "has_plan"
                            print("✅ Shona fallback: detected HAS_PLAN")
                        elif any(term in msg for term in needs_visit_terms):
                            extracted_data["plan_status"] = "needs_visit"
                            print("✅ Shona fallback: detected NEEDS_VISIT")
                
                    # ADDITIONAL SAFETY CHECK: Never extract plan_status if we already have it
                    if self.appointment.has_plan is not None and extracted_data.get('plan_status'):
                        print(f"⚠️ BLOCKED: Attempted to re-extract plan_status when already set to {self.appointment.has_plan}")
                        extracted_data['plan_status'] = None  # Force to null
                
                    # Debug log for plan status specifically
                    if extracted_data.get('plan_status'):
                        print(f"✅ PLAN STATUS DETECTED: {extracted_data['plan_status']}")
                
                    return extracted_data
                except json.JSONDecodeError as e:
                    print(f"❌ AI returned invalid JSON: {ai_response}")
                    print(f"❌ JSON Parse Error: {str(e)}")
                    return {}
                
            except Exception as e:
                print(f"❌ AI extraction error: {str(e)}")
                return {}


        def get_next_question_to_ask(self):
            """
            5-question booking flow:
            1. service_type          → which service?
            2. project_description   → what exactly needs doing?
            3. availability_date     → which day?
            4. availability_time     → which time slot?
            5. area                  → which suburb?

            After all 5 are collected the appointment is booked immediately.
            The only follow-up question is the customer's name, asked once
            after the booking confirmation is sent.
            """
            # A captured project description answers the service question too — a
            # lead who said "2x shower cubicles and accessories" must never be
            # bounced back to "How may we assist you on plumbing services" just
            # because the service-type classifier couldn't label it (prod
            # 2026-07-02: a 'yes' after the budget tie-down got the opener).
            if (not self.appointment.project_type
                    and not self.appointment.project_description):
                return "service_type"

            if not self.appointment.project_description:
                return "project_description"

            if not self.appointment.customer_area:
                return "area"

            if not self.appointment.scheduled_datetime:
                return "availability_date"

            if not self._time_confirmed():
                return "availability_time"


            if (
                not self.appointment.customer_name
                and self.appointment.status == "confirmed"
                and not self._customer_name_declined()
            ):
                return "name"

            return "complete"


        def update_appointment_with_extracted_data(self, extracted_data, incoming_message=None):
            """
            Update appointment with AI-extracted data.
            New flow: service → project_description → datetime → area.
            """
            from datetime import datetime
            import pytz

            try:
                updated_fields = []
                next_question  = self.get_next_question_to_ask()
    
                print(f"🔄 Updating appointment — current question: {next_question}")
                print(f"📦 Extracted data: {extracted_data}")
    
                # ── Service type ──────────────────────────────────────────────────────
                _VALID_SERVICE_TYPES = {
                    'bathroom_renovation', 'bathroom_installation',
                    'kitchen_renovation', 'kitchen_installation',
                    'bathroom_and_kitchen_renovation', 'new_plumbing_installation',
                    'drain_unblocking', 'pipe_repair', 'geyser_repair', 'toilet_repair',
                    'other',
                }
                if (extracted_data.get('service_type') and
                        extracted_data.get('service_type') != 'null' and
                        not self.appointment.project_type):
                    _raw_svc = extracted_data['service_type']
                    # Normalise space-separated variant if AI returns it
                    _norm_svc = _raw_svc.replace(' ', '_')
                    if _norm_svc in _VALID_SERVICE_TYPES:
                        self.appointment.project_type = _norm_svc
                    else:
                        self.appointment.project_type = _raw_svc
                    updated_fields.append('service_type')
                    print(f"✅ Updated service_type: {self.appointment.project_type}")
    
                # ── Project description ───────────────────────────────────────────────
                _SERVICE_TYPE_LABELS = {
                    'bathroom renovation', 'bathroom',
                    'bathroom installation', 'install a bathroom', 'install bathroom',
                    'kitchen renovation', 'kitchen',
                    'kitchen installation', 'install a kitchen', 'install kitchen',
                    'both renovations', 'bathroom and kitchen renovation',
                    'new plumbing installation', 'plumbing installation',
                    'bathroom_renovation', 'bathroom_installation',
                    'kitchen_renovation', 'kitchen_installation',
                    'bathroom_and_kitchen_renovation', 'new_plumbing_installation',
                    'drain unblocking', 'blocked drain', 'drain_unblocking',
                    'pipe repair', 'pipe_repair', 'leaking pipe', 'burst pipe',
                    'geyser repair', 'geyser_repair', 'fix geyser',
                    'toilet repair', 'toilet_repair', 'fix toilet',
                }
                _extracted_desc = (extracted_data.get('project_description') or '').strip()
                # A service-type-only phrase ("Bathroom and kitchen installations")
                # is NOT a description on the first pass — ask the scripted
                # description question first; store it only when the lead repeats
                # service types AFTER we've specifically asked (retry >= 1).
                _desc_retry = self._get_question_retry_count('project_description')
                if (
                    _extracted_desc and
                    _extracted_desc != 'null' and
                    not self.appointment.project_description and
                    not self._is_product_availability_question(incoming_message) and
                    _extracted_desc.lower() not in _SERVICE_TYPE_LABELS and
                    not (self._is_service_type_only(_extracted_desc) and _desc_retry == 0)
                ):
                    self.appointment.project_description = _extracted_desc
                    updated_fields.append('project_description')
                    print(f"✅ Updated project_description: {self.appointment.project_description[:60]}")
                elif (
                    next_question == 'project_description' and
                    not self.appointment.project_description and
                    self._looks_like_project_description_reply(incoming_message) and
                    not self._is_product_availability_question(incoming_message) and
                    (incoming_message or '').strip().lower() not in _SERVICE_TYPE_LABELS and
                    not (self._is_service_type_only(incoming_message) and _desc_retry == 0)
                ):
                    self.appointment.project_description = incoming_message.strip()
                    updated_fields.append('project_description')
                    print(f"✅ Fallback project_description from raw message: "
                        f"{self.appointment.project_description[:60]}")
                elif (
                    next_question == 'project_description' and
                    not self.appointment.project_description and
                    self.appointment.project_type and
                    # Only store the service type AS the description once we've ALREADY
                    # asked for detail and they gave the same short answer again. On
                    # the FIRST one-word / service-type answer, don't store — let the
                    # flow ask the scripted project-description question first.
                    self._get_question_retry_count('project_description') >= 1
                ):
                    # Customer repeated the service type (or gave nothing new) —
                    # don't push further; use the service type as the description
                    _msg_norm = (incoming_message or '').strip().lower().replace(' ', '_')
                    _svc_norm = (self.appointment.project_type or '').lower()
                    if (
                        (incoming_message or '').strip().lower() in _SERVICE_TYPE_LABELS or
                        _msg_norm == _svc_norm or
                        _svc_norm.replace('_', ' ') in (incoming_message or '').lower()
                    ):
                        _desc = self.appointment.project_type.replace('_', ' ')
                        self.appointment.project_description = _desc
                        updated_fields.append('project_description')
                        print(f"✅ Description stored from repeated service type: {_desc}")

                # ── Area — capture passively whenever volunteered ─────────────────────
                if (extracted_data.get('area') and
                        extracted_data.get('area') != 'null' and
                        not self.appointment.customer_area):
                    _raw_area = extracted_data['area']
                    _excl     = self._is_excluded_city(_raw_area)
                    if _excl:
                        # Flag as excluded — do NOT save the area
                        token = f'[EXCLUDED_AREA:{_excl}]'
                        notes = self.appointment.internal_notes or ''
                        if token not in notes:
                            self.appointment.internal_notes = f'{notes}\n{token}'.strip()
                            self.appointment.save(update_fields=['internal_notes'])
                        updated_fields.append('excluded_area')
                        print(f"🚫 Excluded area: {_raw_area} → {_excl}")
                    else:
                        self.appointment.customer_area = _raw_area
                        updated_fields.append('area')
                        print(f"✅ Updated area: {self.appointment.customer_area}")
    
                # ── Availability / DateTime ───────────────────────────────────────────
                if (extracted_data.get('availability') and
                        extracted_data.get('availability') != 'null'):
                    try:
                        parsed_dt = datetime.strptime(extracted_data['availability'], '%Y-%m-%dT%H:%M')
                        sa_timezone = pytz.timezone('Africa/Johannesburg')
                        localized_dt = sa_timezone.localize(parsed_dt)
    
                        old_dt = self.appointment.scheduled_datetime
                        self.appointment.scheduled_datetime = localized_dt
                        updated_fields.append('availability')
                        print(f"📅 Updated datetime: {old_dt} -> {localized_dt}")
    
                        # If time is non-midnight, mark it confirmed
                        if localized_dt.hour != 0 or localized_dt.minute != 0:
                            self._mark_time_confirmed()
    
                    except ValueError as e:
                        print(f"❌ Failed to parse AI datetime: {extracted_data['availability']} — {e}")
    
                elif (
                    next_question == 'availability_date' and
                    not self.appointment.scheduled_datetime and
                    not extracted_data.get('availability')
                ):
                    # Try to parse a day name selection using the existing helper
                    parsed = self.process_alternative_time_selection(incoming_message)
                    if parsed:
                        # Store date only (midnight) — time confirmed separately
                        self.appointment.scheduled_datetime = parsed.replace(hour=0, minute=0, second=0)
                        updated_fields.append('availability')
                        self.appointment.save(update_fields=['scheduled_datetime'])
                        print(f"✅ Day selection captured: {self._get_selected_local_date()}")


                elif (
                    next_question == 'availability_time' and
                    self.appointment.scheduled_datetime and
                    not extracted_data.get('availability')
                ):
                    parsed_time_only = self._parse_time_only_for_selected_date(incoming_message)
                    if parsed_time_only:
                        old_dt = self.appointment.scheduled_datetime
                        self.appointment.scheduled_datetime = parsed_time_only
                        self._mark_time_confirmed()
                        updated_fields.append('availability')
                        print(f"âœ… Time selection captured: {old_dt} -> {self.appointment.scheduled_datetime}")
                    else:
                        # Lead was asked for a time but gave none — they've committed
                        # a date with no time. Hand it to the plumber once so a human
                        # can call to pin the time down.
                        self._maybe_alert_plumber_date_no_time()
            
                # ── Customer name ─────────────────────────────────────────────────────
                if (extracted_data.get('customer_name') and
                        extracted_data.get('customer_name') != 'null' and
                        not self.appointment.customer_name):
                    if self.is_valid_name(extracted_data['customer_name']):
                        self.appointment.customer_name = extracted_data['customer_name']
                        self._clear_customer_name_declined()
                        updated_fields.append('customer_name')
                        print(f"✅ Updated customer_name: {self.appointment.customer_name}")
    
                if updated_fields:
                    update_field_map = {
                        'service_type': 'project_type',
                        'project_description': 'project_description',
                        'area': 'customer_area',
                        'availability': 'scheduled_datetime',
                        'customer_name': 'customer_name',
                    }
                    db_update_fields = [
                        update_field_map[field]
                        for field in updated_fields
                        if field in update_field_map and self._appointment_has_field(update_field_map[field])
                    ]
                    if db_update_fields:
                        self.appointment.save(update_fields=db_update_fields)
                    refresh_lead_score(self.appointment)

                    # Reset retry count for every question that was just answered
                    # so the NEXT unanswered question starts fresh at 0
                    question_to_field = {
                        'service_type': 'service_type',
                        'project_description': 'project_description',
                        'area': 'area',
                        'availability_date': 'availability',
                        'availability_time': 'availability',
                        'customer_name': 'customer_name',
                    }
                    for question_key, field_key in question_to_field.items():
                        if field_key in updated_fields:
                            self._set_question_retry_count(question_key, 0)
                            print(f"🔄 Reset retry count for question: {question_key}")
                    print(f"💾 Saved appointment with updated fields: {updated_fields}")
                else:
                    print("ℹ️ No fields were updated")
    
                return updated_fields
    
            except Exception as e:
                print(f"❌ Error updating appointment: {str(e)}")
                import traceback
                traceback.print_exc()
                return []


        def extract_appointment_data_with_ai(self, message):
            """Enhanced AI extraction with proper property_type handling"""
            try:
                next_question = self.get_next_question_to_ask()
                retry_count = getattr(self.appointment, 'retry_count', 0)
            
                extraction_prompt = f"""
                You are a data extraction assistant for a plumbing appointment system.
            
                TASK: Extract specific appointment information from the customer's message.
            
                CONTEXT:
                - Current date: {timezone.now().strftime('%Y-%m-%d')}
                - Current question being asked: {next_question}
                - Customer message: "{message}"
                - Phone number: {self.phone_number}
                - Retry attempt: {retry_count}
            
                EXTRACTION RULES:
                1. Only extract data relevant to the current question being asked
                2. Return ONLY the extracted value, no explanations
                3. If no clear answer is found, return "NOT_FOUND"
                4. Be flexible with language variations and typos
            
                QUESTION-SPECIFIC EXTRACTION:
            
                If current question is "service_type":
                - Look for: bathroom, kitchen, plumbing, installation, renovation, repair
                - Return one of: "bathroom renovation", "kitchen renovation",
                  "bathroom and kitchen renovation", or "new plumbing installation"
                - Return "bathroom and kitchen renovation" when both rooms are mentioned
                  (e.g. "both", "both renovations", "kitchen and bathroom")
            
                If current question is "plan_or_visit":
                - Look for: existing plan, site visit, yes/no responses
                - Return one of: "has_plan", "needs_visit"
            
                If current question is "area":
                - Extract location/area information
                - Return the area name (e.g., "Hatfield", "Avondale", "Ziko", "Budiriro")
                - ANY short word or phrase is an area name in this context — do NOT treat
                  it as a customer name. Zimbabwe has many unique suburb names.
            
                If current question is "timeline":
                - Extract when they want work done
                - Return the timeline as stated
            
                If current question is "property_type":
                - Look for: house, apartment, business, home, flat, office, shop
                - Be flexible with synonyms
                - Return one of: "house", "apartment", "business"
            
                If current question is "availability":

                - Parse complete date and time to format YYYY-MM-DDTHH:MM
                - Handle relative dates like "today", "tomorrow", weekdays
                - Return complete datetime or "PARTIAL_INFO" or "NOT_FOUND"
            
                If current question is "name":
                - Extract person's name from patterns like "I'm", "my name is", "call me"
                - Return full name in title case
            
                CUSTOMER MESSAGE: "{message}"
                CURRENT QUESTION: {next_question}
            
                EXTRACTED VALUE:"""
            
                # Call AI to extract the data
                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {"role": "system", "content": "You are a precise data extraction assistant."},
                        {"role": "user", "content": extraction_prompt}
                    ],
                    temperature=0.1,
                    max_tokens=100
                )
            
                extracted_value = response.choices[0].message.content.strip()
            
                if extracted_value and extracted_value not in ["NOT_FOUND", "PARTIAL_INFO"]:
                    result = self.process_extracted_data(next_question, extracted_value, message)
                    if result == "BOOK_APPOINTMENT":
                        return "BOOK_APPOINTMENT"
                    print(f"✅ AI extracted {next_question}: '{extracted_value}'")
                else:
                    print(f"🤖 AI could not extract {next_question} from: '{message}'")
                
                return extracted_value
            
            except Exception as e:
                print(f"❌ AI extraction error: {str(e)}")
                return self.fallback_manual_extraction(message)


        def process_extracted_data(self, question_type, extracted_value, original_message):
            """FIXED: Process the AI-extracted data and update the appointment"""
            try:
                print(f"🔧 Processing extracted data: {question_type} = '{extracted_value}'")
            
                # Only update if we don't already have this information
                if question_type == "service_type" and not self.appointment.project_type:
                    _valid = {
                        'bathroom renovation': 'bathroom_renovation',
                        'kitchen renovation': 'kitchen_renovation',
                        'bathroom and kitchen renovation': 'bathroom_and_kitchen_renovation',
                        'new plumbing installation': 'new_plumbing_installation',
                    }
                    if extracted_value in _valid:
                        self.appointment.project_type = _valid[extracted_value]
                    
                elif question_type == "plan_or_visit" and self.appointment.has_plan is None:
                    if extracted_value == "has_plan":
                        self.appointment.has_plan = True
                    elif extracted_value == "needs_visit":
                        self.appointment.has_plan = False
                    
                elif question_type == "area" and not self.appointment.customer_area:
                    _excl = self._is_excluded_city(extracted_value or '')
                    if _excl:
                        token = f'[EXCLUDED_AREA:{_excl}]'
                        notes = self.appointment.internal_notes or ''
                        if token not in notes:
                            self.appointment.internal_notes = f'{notes}\n{token}'.strip()
                    else:
                        self.appointment.customer_area = extracted_value
                
                elif question_type == "timeline" and not self.appointment.timeline:
                    self.appointment.timeline = extracted_value
                
                # FIXED: Add property_type handling that was missing
                elif question_type == "property_type" and not self.appointment.property_type:
                    if extracted_value in ['house', 'apartment', 'business']:
                        self.appointment.property_type = extracted_value
                    
                elif question_type == "name" and not self.appointment.customer_name:
                    if self.is_valid_name(extracted_value):
                        self.appointment.customer_name = extracted_value

                elif question_type == "availability" and not self.appointment.scheduled_datetime:
                    if extracted_value not in ["PARTIAL_INFO", "NOT_FOUND"]:
                        try:
                            # Parse AI datetime format: YYYY-MM-DDTHH:MM
                            parsed_dt = datetime.strptime(extracted_value, '%Y-%m-%dT%H:%M')
                            sa_timezone = pytz.timezone('Africa/Johannesburg')
                            localized_dt = sa_timezone.localize(parsed_dt)
                        
                            print(f"🤖 AI extracted datetime: {localized_dt}")
                        
                            # Store the parsed datetime for booking
                            self.appointment.scheduled_datetime = localized_dt
                            self.appointment.save()
                        
                            print(f"💾 Stored datetime for booking: {localized_dt}")
                            return "BOOK_APPOINTMENT"
                        
                        except ValueError as e:
                            print(f"❌ Failed to parse AI datetime '{extracted_value}': {str(e)}")
            
                # Save the updated appointment
                self.appointment.save()
                print(f"💾 Appointment updated successfully")
            
            except Exception as e:
                print(f"❌ Error processing extracted data: {str(e)}")


        def fallback_manual_extraction(self, message):
            """ENHANCED: Fallback extraction - ONLY extract what's being asked"""
            try:
                message_lower = message.lower()
                original_message = message.strip()
                next_question = self.get_next_question_to_ask()
                retry_count = getattr(self.appointment, 'retry_count', 0)
            
                print(f"🔍 Fallback extraction - Current question: {next_question}")
            
                # Be more generous on retries
                be_generous = retry_count > 0
            
                # CRITICAL: ONLY extract plan status when it's the actual question being asked
                if next_question == "plan_or_visit" and self.appointment.has_plan is None:
                    print(f"❓ Looking for plan status in message: '{message}'")
                
                    # Explicit YES patterns
                    yes_patterns = [
                        'yes', 'yeah', 'yep', 'yup', 'sure', 'have plan', 'got plan', 
                        'have a plan', 'got a plan', 'already have', 'existing plan',
                        'i do', 'i have', 'yes i do', 'yes i have', 'i got'
                    ]
                
                    # Explicit NO patterns
                    no_patterns = [
                        'no', 'nope', 'nah', "don't have", "dont have", 
                        'no plan', 'need visit', 'site visit', 'visit first',
                        "don't", "i don't", 'visit please', 'no i', 'i need'
                    ]
                
                    # Check for YES
                    for pattern in yes_patterns:
                        if pattern in message_lower:
                            self.appointment.has_plan = True
                            self.appointment.save()
                            print(f"✅ Manual extraction: has_plan = True (matched: '{pattern}')")
                            return "has_plan"
                
                    # Check for NO
                    for pattern in no_patterns:
                        if pattern in message_lower:
                            self.appointment.has_plan = False
                            self.appointment.save()
                            print(f"✅ Manual extraction: has_plan = False (matched: '{pattern}')")
                            return "needs_visit"
                
                    print(f"⚠️ No clear plan status found in message")
            
                # Property type detection
                if next_question == "property_type" and not self.appointment.property_type:
                    property_keywords = {
                        'house': ['house', 'home', 'residential'],
                        'apartment': ['apartment', 'flat', 'unit', 'complex'],
                        'business': ['business', 'commercial', 'office', 'shop', 'store', 'company']
                    }
                
                    if be_generous:
                        property_keywords['house'].extend(['place', 'property', 'residence'])
                        property_keywords['apartment'].extend(['condo', 'townhouse'])
                        property_keywords['business'].extend(['work', 'workplace', 'commercial'])
                
                    for prop_type, keywords in property_keywords.items():
                        if any(keyword in message_lower for keyword in keywords):
                            self.appointment.property_type = prop_type
                            self.appointment.save()
                            print(f"✅ Manual extraction: property_type = {prop_type}")
                            return prop_type
            
                return "NOT_FOUND"
            
            except Exception as e:
                print(f"❌ Fallback extraction error: {str(e)}")
                return "NOT_FOUND"


        def update_appointment_from_conversation(self, message):
            """Enhanced version using AI-powered extraction with retry logic"""
            try:
                print(f"🔍 Processing message: '{message}'")
            
                # Get current question and retry count
                next_question = self.get_next_question_to_ask()
                retry_count = getattr(self.appointment, 'retry_count', 0)
            
                # Use AI to extract appointment data
                extracted_result = self.extract_appointment_data_with_ai(message)
            
                # Check if extraction was successful
                if extracted_result and extracted_result not in ["NOT_FOUND", "ERROR"]:
                    # Reset retry count on successful extraction
                    self.appointment.retry_count = 0
                    if self._appointment_has_field('retry_count'):
                        self.appointment.save(update_fields=['retry_count'])
                    print(f"✅ Successfully extracted {next_question}: {extracted_result}")
                    return extracted_result
                else:
                    # Increment retry count for failed extraction
                    self.appointment.retry_count = retry_count + 1
                    self.appointment.save()
                    print(f"⚠️ Failed to extract {next_question}. Retry count: {self.appointment.retry_count}")
                
                    # Don't give up - let AI ask again with different phrasing
                    return "RETRY_NEEDED"
            
                # Check if we should book appointment
                if extracted_result == "BOOK_APPOINTMENT":
                    return "BOOK_APPOINTMENT"
                
            except Exception as e:
                print(f"❌ Error updating appointment from conversation: {str(e)}")
                return "ERROR"

