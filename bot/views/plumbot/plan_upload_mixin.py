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


class PlanUploadMixin:
        def _plan_question_already_pending(self) -> bool:
            """
            Return True if the bot's most recent message already asked about
            plan vs site visit. Prevents asking the same question twice in a row.
            """
            try:
                history = self.appointment.conversation_history or []
                for msg in reversed(history):
                    if msg.get('role') == 'assistant':
                        content = msg.get('content', '').lower()
                        plan_phrases = [
                            'do you have a plan',
                            'have a plan',
                            'site visit',
                            'picture or pdf',
                            'plan already',
                            'plan or visit',
                            'photo/plan',
                            'photo or plan',
                        ]
                        return any(phrase in content for phrase in plan_phrases)
                return False
            except Exception:
                return False


        def handle_plan_later_response(self, message):
            """
            Use DeepSeek to detect if customer is saying they'll send their plan later.
            Returns True ONLY if customer clearly has a plan but will send it later.
            Never triggers on site visit requests.
            """
            try:
                # Only check if plan status is still undecided
                if self.appointment.has_plan is not None:
                    return False

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are an intent classifier for a plumbing appointment system in Zimbabwe. Customers may write in English, Shona, or mixed. Reply with ONLY 'YES' or 'NO'."
                        },
                        {
                            "role": "user",
                            "content": f"""We asked the customer: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"

        Is the customer saying they HAVE a plan and will send/share it later (not now)?

        This should be YES ONLY if:
        - They confirm they have a plan/blueprint/drawing
        - AND they say they will send it later, tonight, tomorrow, soon etc.

        This should be NO if:
        - They are asking for a site visit (even if they mention "tomorrow" as when they want the visit)
        - They say they don't have a plan
        - They mention "tomorrow" in the context of scheduling a visit, not sending a plan
        - They are asking about anything else

        Examples of YES:
        - "I'll send the plan later"
        - "I have blueprints, will send tonight"  
        - "Ndinayo plan, nditumire mangwana" (I have a plan, let me send it tomorrow)
        - "Let me send the drawings when I get home"

        Examples of NO:
        - "Site visit tomorrow" ← NO, they want a visit tomorrow, not sending a plan
        - "Come tomorrow for the visit"
        - "I don't have a plan"
        - "Please do a site visit"
        - "Uye uone mangwana" (Come and see tomorrow)
        - "Kwete, uye utarise" (No, come and look)

        Customer message: "{message}"

        Reply YES or NO only."""
                        }
                    ],
                    temperature=0.1,
                    max_tokens=5
                )

                result = response.choices[0].message.content.strip().upper()
                is_plan_later = result == "YES"

                print(f"🤖 DeepSeek plan-later detection: '{message}' → {result}")

                if is_plan_later:
                    self.appointment.has_plan = True
                    if self._appointment_has_field('retry_count'):
                        self.appointment.save(update_fields=['retry_count'])
                    print(f"✅ Updated: has_plan = True (customer will send plan later)")

                return is_plan_later

            except Exception as e:
                print(f"❌ DeepSeek plan-later detection error: {str(e)}")
                return False  # Safe default — don't assume


        def has_basic_info_for_plan_upload(self):
            """Check if we have enough basic info to start plan upload process"""
            return (self.appointment.project_type and 
                    self.appointment.customer_area and 
                    self.appointment.property_type)


        def initiate_plan_upload_flow(self):
            """Start the plan upload process"""
            try:
                self.appointment.plan_status = 'pending_upload'
                self.appointment.save()
            
                service_name = self.appointment.project_type.replace('_', ' ').title()
            
                upload_message = f"""Perfect! Since you have a plan for your {service_name}, I'll need you to send it to me so our plumber can review it.

    📋 PLAN UPLOAD INSTRUCTIONS:

    1. Take clear photos of your plan/blueprint
    2. Send them as images in this chat (one by one)
    3. Or send as a PDF document

    Make sure the plan shows:
    • Room dimensions
    • Fixture locations  
    • Plumbing connections
    • Any special requirements

    Once you send the plan, I'll forward it to our plumber immediately. Send your first image or document now."""

                return upload_message

            except Exception as e:
                print(f"❌ Error initiating plan upload: {str(e)}")
                return "I'd like to help you with your plan, but I'm having a technical issue. Could you try again in a moment?"


        def handle_plan_upload_flow(self, message):
            """Handle messages during plan upload process"""
            try:
                # Check if this is a plan completion message
                completion_indicators = ['done', 'finished', 'complete', 'that\'s all', 'no more', 'all sent']
                message_lower = message.lower()
            
                if any(indicator in message_lower for indicator in completion_indicators):
                    return self.complete_plan_upload()
            
                # Check for more images/documents
                if any(word in message_lower for word in ['more', 'another', 'next', 'additional']):
                    return "Great! Please send the next image or document."
            
                # Check for questions or concerns
                if '?' in message or any(word in message_lower for word in ['help', 'how', 'what', 'problem', 'issue']):
                    return self.handle_plan_upload_question(message)
            
                # Default response during upload
                return """Thanks! I can see you're sending the plan materials. 

    If you have more images or documents to send, please continue. 

    When you're finished sending everything, just type "done" or "finished" and I'll send it all to the plumber."""

            except Exception as e:
                print(f"❌ Error in plan upload flow: {str(e)}")
                return "I'm processing your plan. If you have more to send, please continue. Type 'done' when finished."


        def handle_plan_upload_question(self, message):
            """Handle questions during plan upload process"""
            try:
                question_lower = message.lower()
            
                if 'format' in question_lower or 'type' in question_lower:
                    return "You can send: JPG/PNG images, PDF documents, or even hand-drawn sketches. Just make sure they're clear and readable."
            
                elif 'size' in question_lower or 'large' in question_lower:
                    return "File size shouldn't be an issue through WhatsApp. If a file is too large, try taking separate photos of different sections."
            
                elif 'quality' in question_lower or 'clear' in question_lower:
                    return "Make sure the text and measurements are readable. Good lighting helps. If a photo is blurry, feel free to retake it."
            
                elif 'how many' in question_lower or 'pages' in question_lower:
                    return "Send as many images/pages as needed to show the complete plan. Most customers send 2-5 images."
            
                else:
                    return "I'm here to help with your plan upload. Send your images/documents and type 'done' when finished. Any specific questions about the upload process?"

            except Exception as e:
                print(f"❌ Error handling upload question: {str(e)}")
                return "Please continue sending your plan materials. Type 'done' when you've sent everything."


        def complete_plan_upload(self):
            """Complete the plan upload process and notify plumber"""
            try:
                # Update appointment status
                self.appointment.plan_status = 'plan_uploaded'
                self.appointment.save()

                plumber_number = getattr(
                    self.appointment,
                    'plumber_contact_number',
                    '+263774819901'
                )

                # Notify plumber
                self.notify_plumber_about_plan()

                service_name = self.appointment.project_type.replace('_', ' ').title()
                customer_name = self.appointment.customer_name

                # ✅ Customer-friendly wording
                if customer_name:
                    intro_message = (
                        f"Hi {customer_name}, I've forwarded your {service_name} "
                        "plan to our plumber for review."
                    )
                else:
                    intro_message = (
                        f"Thanks! I've forwarded your {service_name} "
                        "plan to our plumber for review."
                    )

                completion_message = f"""✅ PLAN SENT SUCCESSFULLY!

        {intro_message}

        📞 NEXT STEPS:
        • Our plumber will review your plan within 24 hours
        • They'll contact you directly on this number: {self.phone_number.replace('whatsapp:', '')}
        • They'll discuss the project details and provide a quote
        • Once approved, they'll book your appointment or message you to complete booking

        🔧 PLUMBER DIRECT CONTACT:
        If you need to reach them directly: {plumber_number.replace('+263', '0').replace('+', '')}

        You don't need to do anything now — just wait for their call. They're very responsive!

        Questions? Feel free to ask here anytime 😊
        """

                return completion_message

            except Exception as e:
                print(f"❌ Error completing plan upload: {str(e)}")
                return (
                    "Your plan has been uploaded successfully. "
                    "Our plumber will review it and contact you within 24 hours."
                )


        def handle_post_upload_messages(self, message):
            """
            Called when has_plan=True and plan_status='plan_uploaded'.

            The customer has just sent us their image/plan and the media ack asked them
            to describe what they want done.  This method treats their reply as the
            project description, stores it, and marks the plan as reviewed so the
            normal booking flow can continue (area → datetime → confirmation).

            Returns None to signal the caller to fall through to the booking flow.
            Returns a string only when we genuinely need to handle an edge case.
            """
            try:
                msg = (message or '').strip()

                # If description not yet stored, this message IS the description
                if not self.appointment.project_description and msg:
                    self.appointment.project_description = msg
                    self.appointment.plan_status = 'plan_reviewed'
                    self.appointment.save(update_fields=['project_description', 'plan_status'])
                    print(f"✅ Project description captured from post-upload reply: {msg[:60]}")
                    # Return None → caller falls through to normal booking question
                    return None

                # Description already stored — just exit the plan_uploaded gate
                if self.appointment.plan_status == 'plan_uploaded':
                    self.appointment.plan_status = 'plan_reviewed'
                    self.appointment.save(update_fields=['plan_status'])

                return None

            except Exception as e:
                print(f"❌ Error in handle_post_upload_messages: {str(e)}")
                return None


        def provide_plan_status_update(self):
            """Provide status update on plan review"""
            # Calculate time since upload
            upload_time = self.appointment.updated_at
            hours_since = (timezone.now() - upload_time).total_seconds() / 3600
        
            if hours_since < 24:
                remaining_hours = int(24 - hours_since)
                return f"""📋 PLAN STATUS UPDATE:

    Your plan was sent {int(hours_since)} hours ago. Our plumber typically responds within 24 hours.

    Expected contact: Within the next {remaining_hours} hours

    If it's urgent, you can call directly: 0774819901

    Otherwise, they'll definitely contact you today!"""
            else:
                return """I see it's been over 24 hours since your plan was sent. Let me check on this for you.

    Please call our plumber directly at 0774819901 - they may have tried to reach you already.

    I'll also send them a follow-up message now."""


        def handle_plan_change_request(self):
            """Handle requests to change or update the plan"""
            self.appointment.plan_status = 'pending_upload'
            self.appointment.save()
        
            return """No problem! I can help you send an updated plan.

    Please send your revised plan materials now (images or PDF). 

    I'll make sure the plumber gets the updated version and knows it replaces the previous one."""


        def handle_urgent_plan_request(self):
            """Handle urgent plan review requests"""
            try:
                # Send urgent notification to plumber
                urgent_message = f"""🚨 URGENT PLAN REVIEW REQUEST

    Customer: {self.appointment.customer_name or 'Customer'}
    Phone: {self.phone_number.replace('whatsapp:', '')}
    Project: {self.appointment.project_type}

    Customer is requesting urgent review of their uploaded plan.

    Please contact ASAP: {self.phone_number.replace('whatsapp:', '')}

    View details: http://127.0.0.1:8000/appointments/{self.appointment.id}/"""

                # Send to plumber
                twilio_client.messages.create(
                    body=urgent_message,
                    from_=TWILIO_WHATSAPP_NUMBER,
                    to='whatsapp:+0774819901'
                )
            
                return """🚨 I've marked your plan review as URGENT and notified our plumber immediately.

    They should contact you within the next few hours.

    For immediate assistance, you can also call: 0774819901

    I understand this is time-sensitive!"""

            except Exception as e:
                print(f"❌ Error handling urgent request: {str(e)}")
                return "I've noted this is urgent. Please call our plumber directly at 0774819901 for immediate assistance."


        def verify_plan_question_not_asked_recently(self):
            """Check if we asked about plan in last 5 messages"""
            try:
                if not self.appointment.conversation_history:
                    return False
            
                recent_messages = self.appointment.conversation_history[-5:]
                plan_keywords = ['have a plan', 'site visit', 'existing plan', 'Do you have']
            
                for msg in recent_messages:
                    if msg.get('role') == 'assistant':
                        content = msg.get('content', '').lower()
                        if any(keyword.lower() in content for keyword in plan_keywords):
                            return True  # We asked recently
            
                return False  # Safe to ask
            except Exception as e:
                print(f"Error checking conversation history: {str(e)}")
                return False

