from __future__ import annotations

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


class ResponseMixin:
        def _build_retry_context_line(self, updated_fields, next_question) -> str:
            updated_fields = updated_fields or []
            if 'area' in updated_fields and self.appointment.customer_area:
                return (
                    f"Thanks for providing your area. We've actually done a number of renovations in "
                    f"{self.appointment.customer_area} recently."
                )
            if 'project_description' in updated_fields and self.appointment.project_description:
                return "Thanks for the extra detail. That gives us a much clearer picture of the job."
            if 'service_type' in updated_fields and self.appointment.project_type:
                service_name = self.appointment.project_type.replace('_', ' ').title()
                return f"Thanks for clarifying the service. That helps us point you in the right direction for the {service_name}."
            if 'availability' in updated_fields:
                if next_question == 'availability_time':
                    return "Thanks, that day is noted. We just need to lock in the best time for you."
                if next_question == 'area':
                    return "Thanks, that time works on our side. We just need your area to finish this off."
            return ""


        def _declines_sharing_name(self, message: str) -> bool:
            msg = (message or '').strip().lower()
            if not msg:
                return False
            decline_phrases = {
                'no', 'nope', 'nah', 'prefer not', 'rather not', 'no thanks',
                'not comfortable', 'dont want to', "don't want to",
                'dont want', "don't want", 'not now'
            }
            return any(phrase in msg for phrase in decline_phrases)


        def _describe_project_context(self) -> str:
            """Build a short, human-readable visit purpose based on project details."""
            project = (self.appointment.project_type or '').lower().replace('_', ' ')
            desc    = (self.appointment.project_description or '').lower()
    
            # Keyword-based specifics
            if 'drain' in desc or 'pipe' in desc:
                return 'have a quick look at the drains/pipes'
            if 'toilet' in desc or 'chimbuzi' in desc:
                return 'have a quick look at the toilet setup'
            if 'shower' in desc or 'bath' in desc or 'tub' in desc:
                return 'have a quick look at the bathroom space'
            if 'kitchen' in project or 'kitchen' in desc:
                return 'have a quick look at the kitchen plumbing'
            if 'geyser' in desc:
                return 'have a quick look at the geyser setup'
            if 'installation' in project:
                return 'have a quick look at the site for the installation'
    
            # Generic fallback
            return 'have a quick look at the space'


        def _get_contextual_description_question(self) -> str:
            """Return a service-specific, Hormozi-style question to capture project detail."""
            svc = (self.appointment.project_type or '').lower()

            if svc == 'bathroom_installation':
                return (
                    "Are we fitting the bathroom from scratch in a new space, "
                    "or converting an existing room — and which fixtures do you want: "
                    "toilet, shower, bath, or the full set?"
                )
            if svc == 'kitchen_installation':
                return (
                    "Is this kitchen being plumbed fresh, or is there existing pipework "
                    "to work around — and are we talking sink, dishwasher connection, or both?"
                )
            if 'bathroom' in svc and 'kitchen' in svc:
                return (
                    "Which room is the priority — bathroom, kitchen, or both at once? "
                    "And is it a full redo or specific fixtures you want sorted?"
                )
            if 'bathroom' in svc:
                return (
                    "Is this a full bathroom redo — tiling, fittings, the works — "
                    "or are you targeting specific things like the shower, tub, or toilet?"
                )
            if 'kitchen' in svc:
                return (
                    "Is this a full kitchen refit or specific work — "
                    "new sink, countertop plumbing, or drainage?"
                )
            if 'drain' in svc:
                return (
                    "Which drain is blocked — kitchen, bathroom, or outside? "
                    "And is it draining slowly or completely backed up?"
                )
            if 'pipe' in svc:
                return (
                    "Where's the pipe — in a wall, under a sink, or outside? "
                    "And is it dripping or has it fully burst?"
                )
            if 'geyser' in svc:
                return (
                    "Is the geyser not heating at all, leaking, or just making noise — "
                    "and how long has it been like that?"
                )
            if 'toilet' in svc:
                return (
                    "What's the toilet doing — leaking at the base, not flushing, "
                    "or running continuously?"
                )
            if 'installation' in svc:
                return (
                    "Is this for a new build or an extension — "
                    "and which areas need plumbing: bathroom, kitchen, or the full house?"
                )
            return (
                "What specifically needs doing — the more detail, "
                "the sharper the quote we can give you."
            )

        def _format_day(self, date_obj) -> str:
            """Return e.g. 'Monday the 7th' or 'Tuesday the 8th'."""
            day_names = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
            day_name  = day_names[date_obj.weekday()]
            day_num   = date_obj.day
            suffix    = 'th' if 11 <= day_num <= 13 else {1: 'st', 2: 'nd', 3: 'rd'}.get(day_num % 10, 'th')
            return f"{day_name} the {day_num}{suffix}"


        def _get_pricing_followup_prompt(self, language: str = "english") -> str:
            """Return the next booking question in a natural sales tone."""
            next_question = self.get_next_question_to_ask()
            is_shona = language == "shona"

            if next_question == "service_type":
                return "Uri kuda service ipi chaizvo?" if is_shona else "Which service are you looking at exactly?"
            if next_question == "project_description":
                if is_shona:
                    return "Parizvino, ungandiudza zvishoma kuti chii chaizvo chamunoda kuti chiitwe?"
                return self._get_contextual_description_question()
            if next_question == "availability_date":
                days = self._get_next_two_available_days()
                if len(days) >= 2:
                    if is_shona:
                        return (
                            f"Kana muchida, tinogona kutanga nefree on-site assessment. "
                            f"{self._format_day(days[0])} kana {self._format_day(days[1])}, nderipi zuva rinokukodzerai?"
                        )
                    return (
                        f"If you'd like, we can do a free on-site assessment first. "
                        f"Would {self._format_day(days[0])} or {self._format_day(days[1])} work better for you?"
                    )
                return (
                    "Kana muchida, tinogona kutanga nefree on-site assessment. Nderipi zuva rinokukodzerai?"
                    if is_shona else
                    "If you'd like, we can do a free on-site assessment first. Which day would suit you best?"
                )
            if next_question == "availability_time":
                return "Nguva ipi ingakukodzerai ye free on-site assessment?" if is_shona else "What time would suit you best for the free on-site assessment?"
            if next_question == "area":
                return "Muri munzvimbo ipi kuti tironge kuuya zvakanaka?" if is_shona else "What area are you in so we can plan the visit properly?"
            if next_question == "name":
                return "Tingaisa zita ripi pabhooking?" if is_shona else "What name should we put on the booking?"
            return (
                "Kana muchida, ndinogona kukubatsirai kubhuka free on-site assessment kubva pano."
                if is_shona else
                "If you'd like, I can help you book the free on-site assessment from here."
            )


        def _build_pricing_response(
            self,
            *,
            breakdown_lines,
            total_line: str,
            cheapest_line: str,
            visit_committed: bool = False,
            language: str = "english",
        ) -> str:
            if language == "shona":
                return (
                    f"{total_line}\n\n"
                    f"{cheapest_line}\n\n"
                    f"{self._get_pricing_followup_prompt('shona')}"
                )
            return (
                f"{total_line}\n\n"
                f"{cheapest_line}\n\n"
                f"{self._get_pricing_followup_prompt('english')}"
            )


        def _is_delay_or_exit_signal(self, message: str) -> bool:
            """
            Return True ONLY when the customer is signalling they want to pause/end
            AND one of the following is true:
              1. The appointment is already confirmed (booked)
              2. The customer has explicitly said they will reach out later

            For all other cases — including mid-conversation acks like "oh ok", "sharp",
            "shap", "cool", "noted" — return False so the bot continues naturally.

            Uses DeepSeek to classify intent accurately, with a fast pre-filter to avoid
            burning tokens on obvious non-exit messages.
            """
            msg = (message or '').strip()
            if not msg:
                return False

            msg_lower = msg.lower()

            if len(msg_lower.split()) > 6:
                return False

            if '?' in msg:
                return False

            engagement_signals = (
                'how much', 'price', 'cost', 'quote', 'photo', 'pic', 'picture',
                'bathroom', 'shower', 'toilet', 'tub', 'vanity', 'geyser', 'kitchen',
                'marii', 'mutengo', 'chimbuzi', 'shawa', 'bhavhu', 'kicheni',
                'when', 'where', 'what', 'which', 'who', 'can you', 'do you',
            )
            if any(sig in msg_lower for sig in engagement_signals):
                return False

            obvious_acks = {
                'ok', 'okay', 'k', 'kk', 'oky', 'oh ok', 'oh okay', 'ooh ok',
                'ooh okay', 'sharp', 'shap', 'sho', 'cool', 'nice', 'noted',
                'got it', 'alright', 'great', 'good', 'fine', 'sure', 'yes',
                'yep', 'yeah', 'yup', 'no', 'nope', 'nah', 'ok thanks',
                'ok thank you', 'thanks', 'thank you', 'thank u', 'thx', 'thnx',
                'understood', 'i see', 'ah ok', 'ah okay', 'oh ok thanks',
                'oh okay thanks', 'ok cool', 'ok bye', 'okay bye', 'bye',
                'no worries', '👍', '🙏', '✅', '😊', 'bo', 'bho',
                'hongu', 'zvakanaka', 'maita basa', 'ndatenda',
            }
            explicit_delay_phrases = (
                "i'll talk", "i will talk", "talk later", "will contact",
                "contact later", "i'll be in touch", "get back to you",
                "busy now", "busy at the moment", "not right now",
                "will let you know", "will come back", "come back later",
                "in a bit", "later today", "i'll get back", "let me think",
                "need to think", "thinking about it", "i will reach out",
                "will reach out", "i'll reach out", "ndichatumira",
                "mangwana", "ndichauya",
            )

            is_obvious_ack = msg_lower in obvious_acks
            is_explicit_delay = any(phrase in msg_lower for phrase in explicit_delay_phrases)

            if not is_obvious_ack and not is_explicit_delay:
                return False

            appointment_confirmed = self.appointment.status == 'confirmed'
            customer_said_later = self._customer_said_they_will_reach_out()

            if appointment_confirmed or customer_said_later:
                if is_explicit_delay or is_obvious_ack:
                    print(
                        f"✅ Exit signal accepted: confirmed={appointment_confirmed}, "
                        f"said_later={customer_said_later}, msg='{msg}'"
                    )
                    return True

            if is_obvious_ack and not appointment_confirmed and not customer_said_later:
                return self._deepseek_classify_exit_intent(msg)

            return False


        def _customer_said_they_will_reach_out(self) -> bool:
            """
            Scan recent conversation history for messages where the customer
            explicitly said they will contact us later / in due time.
            Checks the last 10 customer messages.
            """
            history = self.appointment.conversation_history or []
            reach_out_phrases = (
                "i'll reach out", "will reach out", "i'll contact",
                "will contact you", "i'll get back", "get back to you",
                "i'll be in touch", "will be in touch", "contact later",
                "i'll call", "will call you", "reach out later",
                "i'll message", "will message", "come back to this",
                "revisit later", "when i'm ready", "when ready",
                "ndichatumira", "ndichauya", "ndichakubata",
                "mangwana ndichauya", "ill reach out",
            )
            customer_messages = [
                m.get('content', '').lower()
                for m in history[-20:]
                if m.get('role') == 'user'
            ][-10:]

            for content in customer_messages:
                if any(phrase in content for phrase in reach_out_phrases):
                    return True
            return False


        def _deepseek_classify_exit_intent(self, message: str) -> bool:
            """
            Use DeepSeek to determine whether a short acknowledgement message
            means the customer wants to END/PAUSE the conversation, or whether
            it is a mid-conversation acknowledgement that expects a bot reply.

            Returns True only if DeepSeek is HIGH confidence the customer is done.
            Defaults to False (keep conversation alive) on any error or LOW confidence.
            """
            try:
                next_question = self.get_next_question_to_ask()
                has_project = bool(self.appointment.project_type)
                has_area = bool(self.appointment.customer_area)
                has_datetime = bool(self.appointment.scheduled_datetime)
                status = self.appointment.status

                context_summary = (
                    f"Appointment status: {status}\n"
                    f"Service type collected: {'yes' if has_project else 'no'}\n"
                    f"Area collected: {'yes' if has_area else 'no'}\n"
                    f"Appointment datetime set: {'yes' if has_datetime else 'no'}\n"
                    f"Next question bot needs to ask: {next_question}"
                )

                history = self.appointment.conversation_history or []
                recent = []
                for m in history[-6:]:
                    role = "Customer" if m.get('role') == 'user' else "Bot"
                    content = (m.get('content') or '')[:200].strip()
                    if content and not content.startswith('['):
                        recent.append(f"{role}: {content}")
                recent_str = "\n".join(recent) if recent else "No recent messages"

                prompt = f"""You are an intent classifier for a WhatsApp chatbot at a Zimbabwean plumbing company.

    CONVERSATION STATE:
    {context_summary}

    RECENT CONVERSATION:
    {recent_str}

    CUSTOMER'S LATEST MESSAGE: "{message}"

    QUESTION: Is the customer's message a signal that they want to END or PAUSE the conversation right now?

    Answer YES only if the customer clearly wants to stop — e.g. they said they'll think about it, they're busy, they'll get back later, or they are done asking questions and expect no reply.

    Answer NO if the message is a mid-conversation acknowledgement that naturally expects the bot to continue — e.g. they just said "ok" after receiving information and are waiting for the bot to ask the next question, or the conversation is clearly still in progress with unanswered questions remaining.

    IMPORTANT: When there are still questions to ask (next_question is not 'complete') and the appointment is not confirmed, default to NO — keep the conversation alive. A bare "ok" or "sharp" from a customer who hasn't booked yet almost always means "I heard you, continue" not "I'm done".

    Reply with ONLY a JSON object:
    {{"intent": "exit" or "continue", "confidence": "HIGH" or "LOW"}}"""

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "Return ONLY valid JSON. No markdown, no explanation.",
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=30,
                )

                raw = response.choices[0].message.content.strip()
                raw = raw.replace('```json', '').replace('```', '').strip()
                result = json.loads(raw)

                intent = result.get('intent', 'continue')
                confidence = result.get('confidence', 'LOW')

                print(
                    f"🤖 DeepSeek exit intent: '{message}' → "
                    f"intent={intent}, confidence={confidence}"
                )

                return intent == 'exit' and confidence == 'HIGH'

            except Exception as exc:
                print(f"⚠️ DeepSeek exit classification failed: {exc} — defaulting to continue")
                return False


        def _get_delay_acknowledgment(self) -> str:
            """
            Return a warm acknowledgment for genuine exit signals.
            Varies based on whether the appointment is booked or they said they'll reach out.
            """
            if self.appointment.status == 'confirmed' and self.appointment.scheduled_datetime:
                import pytz
                sa_tz = pytz.timezone('Africa/Johannesburg')
                dt = self.appointment.scheduled_datetime.astimezone(sa_tz)
                formatted = dt.strftime('%A, %B %d at %I:%M %p')
                return (
                    f"Perfect — see you on {formatted}! "
                    "We will call you 30 minutes before arrival. "
                    "Feel free to message anytime if you have questions. 😊"
                )

            if self._customer_said_they_will_reach_out():
                return (
                    "No problem at all! Whenever you're ready, just drop us a message and "
                    "we'll pick up right where we left off. 😊"
                )

            return (
                "No problem at all! Whenever you're ready, just drop us a message and "
                "we'll pick up right where we left off. 😊"
            )


        def _explicitly_requests_price(self, message: str) -> bool:
            """Return True only when the customer clearly asks about pricing."""
            msg = (message or '').strip().lower()
            if not msg:
                return False

            price_markers = (
                'price', 'pricing', 'cost', 'quote', 'quotation', 'how much',
                'how much is', 'how much are', 'charges', 'charge', 'rate', 'rates',
                'mutengo', 'marii', 'mari', 'zvinodhura', 'inodhura', 'bhadhara',
            )
            return any(marker in msg for marker in price_markers)


        def _looks_like_project_description_reply(self, message: str) -> bool:
            """
            Return True when the message looks like a meaningful description of work
            the customer wants done.
            """
            msg = (message or '').strip()
            msg_lower = msg.lower()
            if not msg:
                return False

            generic_non_answers = {
                'hi', 'hello', 'hey', 'ok', 'okay', 'alright', 'cool', 'sharp',
                'thanks', 'thank you', 'noted', 'yes', 'no', 'bathroom',
                'bathroom renovation', 'kitchen renovation', 'new plumbing installation',
            }
            if msg_lower in generic_non_answers:
                return False

            vague_info_requests = (
                'more information', 'more info', 'tell me more', 'can i get more information',
                'may i get more information', 'need more information',
            )
            if any(phrase in msg_lower for phrase in vague_info_requests):
                return False

            detail_markers = (
                'want', 'need', 'change', 'replace', 'install', 'fix', 'repair',
                'move', 'remove', 'redo', 'renovat', 'upgrade', 'fit',
                'chamber', 'shower', 'toilet', 'geyser', 'basin', 'sink',
                'bath', 'bathtub', 'tub', 'pipe', 'drain', 'tile',
            )
            return any(marker in msg_lower for marker in detail_markers) or len(msg.split()) >= 3


        def _is_product_availability_question(self, message: str) -> bool:
            """
            Return True when the customer is asking whether we HAVE or SELL a product,
            or asking for its price — rather than describing work they want done.

            Examples that return True:
              "And vanitys if you have"
              "do you have tubs"
              "if you have shower cubicles"
              "vanitys?"
              "toilets also?"
              "and geysers"

            Examples that return False (genuine project descriptions):
              "I want to replace my toilet and shower"
              "bathroom renovation with new vanity"
              "need to tile and fit new fixtures"
            """
            msg = (message or '').strip().lower()
            if not msg:
                return False

            availability_patterns = (
                'if you have',
                'do you have',
                'do you sell',
                'you have',
                'you sell',
                'do you do',
                'also?',
                'as well?',
                'too?',
                'and also',
            )
            if any(p in msg for p in availability_patterns):
                return True

            product_words = (
                'vanity', 'vanitys', 'vanities',
                'tub', 'tubs', 'bathtub', 'bathtubs',
                'shower', 'showers', 'cubicle', 'cubicles',
                'toilet', 'toilets', 'chamber', 'chambers',
                'geyser', 'geysers',
                'basin', 'basins', 'sink', 'sinks',
            )
            size_question_patterns = ('how big', 'what size', 'what sizes', 'dimensions', 'how large', 'how wide', 'how long')
            if any(p in msg for p in size_question_patterns) and any(w in msg for w in product_words):
                return True

            clean = msg.removeprefix('and ').strip().rstrip('?').strip()
            word_count = len(msg.split())

            if word_count <= 5 and any(clean == p or clean.startswith(p) for p in product_words):
                return True

            return False


        def generate_response(self, incoming_message, precomputed_service_inquiry=None, precomputed_classification=None):
            try:
                # ── EMAIL CAPTURE (post-booking) ──────────────────────────────────────
                # Must be checked first — the customer's email address should not be
                # classified by DeepSeek or any other handler.
                if self._email_pending():
                    return self._handle_email_capture(incoming_message)

                # ── DELAY SIGNAL ACTIVE ───────────────────────────────────────────────
                # Customer previously said they'll reach out later.
                # Acks ("ok", "sharp", 👍) → save silently, no reply.
                # Substantive message → clear flag, fall through to normal processing.
                if self._delay_signal_active():
                    # When the appointment is already marked delayed, obvious acks
                    # ("ok", "thanks", "👍", etc.) should always be suppressed without
                    # a DeepSeek call. is_delayed=True is all the context we need.
                    _obvious_acks = {
                        'ok', 'okay', 'k', 'kk', 'oky', 'oh ok', 'oh okay', 'ooh ok',
                        'ooh okay', 'sharp', 'shap', 'sho', 'cool', 'nice', 'noted',
                        'got it', 'alright', 'great', 'good', 'fine', 'sure', 'yes',
                        'yep', 'yeah', 'yup', 'no', 'nope', 'nah', 'ok thanks',
                        'ok thank you', 'thanks', 'thank you', 'thank u', 'thx', 'thnx',
                        'understood', 'i see', 'ah ok', 'ah okay', 'oh ok thanks',
                        'oh okay thanks', 'ok cool', 'ok bye', 'okay bye', 'bye',
                        'no worries', '👍', '🙏', '✅', '😊', 'bo', 'bho',
                        'hongu', 'zvakanaka', 'maita basa', 'ndatenda',
                    }
                    if (incoming_message or '').strip().lower() in _obvious_acks:
                        self.appointment.add_conversation_message("user", incoming_message)
                        print(f"🔇 Delay active — ack suppressed without DeepSeek: '{incoming_message[:60]}'")
                        return None
                    if self._is_delay_or_exit_signal(incoming_message):
                        self.appointment.add_conversation_message("user", incoming_message)
                        print(f"🔇 Delay signal active — ack suppressed: '{incoming_message[:60]}'")
                        return None
                    # Check if this is a follow-up date correction ("No I said a month, not the 20th").
                    # If so, re-enter the delay flow at step 2 instead of restarting the booking flow.
                    from bot.out_of_scope_handler import (
                        _message_has_timeframe, _handle_delay_timeframe_answer,
                    )
                    _msg_lower_dc = incoming_message.lower()
                    _correction_signals = (
                        'not the', 'not on', 'not that', 'i said', 'said i', 'i meant', 'actually',
                    )
                    _is_date_correction = (
                        _message_has_timeframe(incoming_message)
                        and any(s in _msg_lower_dc for s in _correction_signals)
                    )
                    if _is_date_correction:
                        reply = _handle_delay_timeframe_answer(incoming_message, {}, self.appointment)
                        self.appointment.add_conversation_message("user", incoming_message)
                        self.appointment.add_conversation_message("assistant", reply)
                        print(f"📅 Delay date correction — re-entering delay flow: '{incoming_message[:60]}'")
                        return reply
                    from bot.whatsapp_webhook import _clear_delay_signal_if_present
                    _clear_delay_signal_if_present(self.appointment)
                    print(f"▶️ Delay signal cleared — customer re-engaged: '{incoming_message[:60]}'")
                    # Fall through to normal processing

                # ── FIRST-TIME DELAY / EXIT SIGNAL ───────────────────────────────────
                # Send one warm acknowledgment, then pause follow-ups.
                if self._is_delay_or_exit_signal(incoming_message):
                    print(f"⏸️ Delay/exit signal — acknowledging and pausing follow-ups")
                    reply = self._get_delay_acknowledgment()
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    self._mark_delay_signal()
                    return reply

                current_question = self.get_next_question_to_ask()

                # ── OUT-OF-SCOPE / DELAY / COMPLAINT HANDLER ─────────────────────────
                # When precomputed_classification is supplied by the webhook, the OOS
                # handler uses it and skips its own DeepSeek call. Pending-state
                # resolution (delay flow steps 2-4) still runs normally.
                from bot.out_of_scope_handler import handle_out_of_scope
                from bot.unified_classifier import uc_as_oos_classification
                has_prior_convo = len(self.appointment.conversation_history or []) > 2
                _oos_precomputed = (
                    uc_as_oos_classification(precomputed_classification)
                    if precomputed_classification else None
                )
                oos_reply = (
                    handle_out_of_scope(
                        incoming_message, self.appointment,
                        precomputed=_oos_precomputed,
                    )
                    if has_prior_convo else None
                )
                if oos_reply is not None:
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", oos_reply)
                    return oos_reply

                # ── PRODUCT SIZE / SPEC QUESTION ─────────────────────────────────────
                _spec_triggers = ('how big', 'what size', 'what sizes', 'dimensions', 'how large', 'how wide', 'how long')
                _tub_words = ('tub', 'tubs', 'bathtub', 'bathtubs', 'free standing', 'freestanding', 'standalone')
                _msg_lower = incoming_message.lower()
                if any(t in _msg_lower for t in _spec_triggers) and any(w in _msg_lower for w in _tub_words):
                    reply = self.handle_service_inquiry('standalone_tub', incoming_message)
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply

                # ── CATALOGUE / PRODUCT LIST REQUEST ─────────────────────────────────
                _catalogue_triggers = ('catalogue', 'catalog', 'price list', 'pricelist', 'product list', 'portfolio')
                if any(w in incoming_message.lower() for w in _catalogue_triggers):
                    from bot.whatsapp_webhook import send_catalogue_images, send_previous_work_photos
                    clean_phone = self.phone_number.replace('whatsapp:', '')
                    images_queued = send_catalogue_images(clean_phone, self.appointment)
                    if not images_queued:
                        images_queued = send_previous_work_photos(clean_phone, self.appointment)
                    followup = self._get_pricing_followup_prompt('english')
                    _price_list = (
                        "• Toilet: Supply from US$50 | Install from US$20\n"
                        "• Tub (built-in): Supply from US$80 | Install from US$80\n"
                        "• Free-standing tub: Supply from US$400 | Mixer from US$150 | Install from US$120\n"
                        "• Shower cubicle: Supply from US$130 | Install from US$40\n"
                        "• Vanity unit: Supply from US$150 | Install from US$30\n"
                        "• Geyser: Supply from US$80 | Install from US$80\n"
                        "• Side chamber: Supply from US$130 | Install from US$30"
                    )
                    if images_queued:
                        self.appointment.add_conversation_message("user", incoming_message)
                        return None
                    else:
                        reply = (
                            "Here's our product catalogue — rough supply + install prices "
                            "(final cost confirmed after a free site visit):\n\n"
                            f"{_price_list}\n\n"
                            "Bundling items can get you a discount. "
                            "The plumber gives a fixed quote on the spot after seeing the space.\n\n"
                            f"{followup}"
                        )
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply

                # ── SERVICE / PRODUCT PRICING INQUIRIES (pre-booking only) ───────────
                any_pricing_sent = (
                    getattr(self.appointment, 'pricing_overview_sent', False) or
                    bool(getattr(self.appointment, 'sent_pricing_intents', None))
                )
                mid_conversation = (
                    any_pricing_sent or
                    (
                        self.appointment.project_type is not None and
                        (
                            self.appointment.has_plan is not None or
                            self.appointment.customer_area is not None
                        )
                    )
                )
                if not mid_conversation and current_question != 'project_description':
                    inquiry = precomputed_service_inquiry or self.detect_service_inquiry(incoming_message)
                    PRODUCT_INTENTS = {
                        'tub_sales', 'standalone_tub', 'geyser', 'shower_cubicle',
                        'vanity', 'bathtub_installation', 'toilet', 'chamber',
                        'facebook_package', 'location_ask', 'location_visit',
                        'previous_quotation', 'pictures', 'combined_pricing',
                        'drain_unblocking', 'pipe_repair', 'geyser_repair', 'toilet_repair',
                    }
                    NON_PRICING_AUTO_REPLY_INTENTS = {
                        'location_ask', 'location_visit', 'previous_quotation', 'pictures',
                        'combined_pricing', 'standalone_tub', 'tub_sales', 'bathtub_installation',
                    }
                    PRICING_AUTO_REPLY_INTENTS = {
                        'geyser', 'shower_cubicle', 'vanity', 'toilet', 'chamber',
                        'drain_unblocking', 'pipe_repair', 'geyser_repair', 'toilet_repair',
                        'facebook_package',
                    }
                    if inquiry.get('intent') != 'none' and (
                        inquiry.get('confidence') == 'HIGH' or
                        inquiry.get('intent') in PRODUCT_INTENTS
                    ):
                        intent = inquiry['intent']
                        price_requested = self._explicitly_requests_price(incoming_message)
                        sent = list(getattr(self.appointment, 'sent_pricing_intents', None) or [])
                        if (intent not in NON_PRICING_AUTO_REPLY_INTENTS and
                                intent not in PRICING_AUTO_REPLY_INTENTS and
                                not price_requested):
                            print(f"Skipping priced service inquiry: {intent} - no explicit price request")
                        elif intent in sent:
                            print(f"⏭️ Skipping already-sent service inquiry: {intent}")
                        else:
                            print(f"💡 Handling service inquiry: {intent}")
                            reply = self.handle_service_inquiry(intent, incoming_message)
                            sent.append(intent)
                            self.appointment.sent_pricing_intents = sent
                            self.appointment.save(update_fields=['sent_pricing_intents'])
                            self.appointment.add_conversation_message("user", incoming_message)
                            self.appointment.add_conversation_message("assistant", reply)
                            return reply

                # ── PLAN UPLOAD FLOW ──────────────────────────────────────────────────
                if (self.appointment.has_plan is True and
                        self.appointment.plan_status == 'pending_upload'):
                    return self.handle_plan_upload_flow(incoming_message)

                if (self.appointment.has_plan is True and
                        self.appointment.plan_status == 'plan_uploaded'):
                    result = self.handle_post_upload_messages(incoming_message)
                    if result is not None:
                        return result
                    # None means description was just captured — fall through to
                    # the normal booking flow so it asks the next question

                # ── CONFIRMED + COMPLETE — respond contextually, never go silent ─────
                if (self.appointment.status == 'confirmed' and
                        self.get_next_question_to_ask() == 'complete'):
                    # Capture name if the customer is still responding to the name ask
                    if not self.appointment.customer_name:
                        extracted = self.extract_all_available_info_with_ai(incoming_message) or {}
                        name = extracted.get('customer_name')
                        if name:
                            self.appointment.customer_name = name
                            self.appointment.save(update_fields=['customer_name'])
                            reply = self._confirm_or_request_email()
                            self.appointment.add_conversation_message("user", incoming_message)
                            self.appointment.add_conversation_message("assistant", reply)
                            return reply
                    reply = self._post_booking_contextual_reply(incoming_message)
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply

                # ── ALTERNATIVE TIME SELECTION ────────────────────────────────────────
                if (self.appointment.status == 'pending' and
                        self.appointment.project_type and
                        self.appointment.customer_area and
                        self.appointment.timeline and
                        self.appointment.property_type and
                        not self.appointment.customer_name):
                    selected_time = self.process_alternative_time_selection(incoming_message)
                    if selected_time:
                        print(f"🎯 Customer selecting alternative time: {selected_time}")
                        booking_result = self.book_appointment_with_selected_time(selected_time)
                        if booking_result['success']:
                            reply = (
                                "One last thing — what name should we put on the booking? "
                                "If you'd rather not share it, just say no."
                            )
                        else:
                            alternatives = booking_result.get('alternatives', [])
                            if alternatives:
                                alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                                reply = (
                                    f"That time isn't available either. Here are some other options:\n"
                                    f"{alt_text}\n\nWhich works better for you?"
                                )
                            else:
                                reply = (
                                    "I'm having trouble finding available times. Could you suggest a "
                                    "completely different day? Our hours are 8 AM - 6 PM, Monday to Friday."
                                )
                        self.appointment.add_conversation_message("user", incoming_message)
                        self.appointment.add_conversation_message("assistant", reply)
                        return reply

                # ── STEP 2: EXTRACT ALL AVAILABLE INFO ───────────────────────────────
                # Use precomputed data from the unified classifier when available,
                # so we don't pay for a second DeepSeek call for the same message.
                if precomputed_classification:
                    from bot.unified_classifier import uc_extracted, uc_service_type
                    _pre = uc_extracted(precomputed_classification)
                    # Seed extracted_data with the unified result.
                    # extract_all_available_info_with_ai still runs but only for
                    # fields the unified call doesn't cover (plan_status, timeline, etc.)
                    extracted_data = {
                        "service_type":         uc_service_type(precomputed_classification),
                        "project_description":  _pre.get("project_description"),
                        "area":                 _pre.get("area"),
                        "availability":         _pre.get("availability"),
                        "customer_name":        _pre.get("customer_name"),
                        # Fields not in unified call — let existing extractor fill these
                        "plan_status":          None,
                        "timeline":             None,
                        "property_type":        None,
                    }
                    # For fields that need extra precision (plan_status, timeline, property_type),
                    # only call the full extractor when those specific questions are next.
                    _next_q = self.get_next_question_to_ask()
                    if _next_q in ("plan_or_visit", "timeline", "property_type"):
                        _deep = self.extract_all_available_info_with_ai(incoming_message)
                        extracted_data.update({
                            k: v for k, v in _deep.items()
                            if v and not extracted_data.get(k)
                        })
                else:
                    extracted_data = self.extract_all_available_info_with_ai(incoming_message)

                # ── PLAN LATER RESPONSE ───────────────────────────────────────────────
                # Use the unified classifier flag when available — skips the API call.
                from bot.unified_classifier import uc_is_plan_later as _uc_plan
                if precomputed_classification is not None:
                    _plan_later = _uc_plan(precomputed_classification)
                    if _plan_later and not getattr(self.appointment, 'has_plan', None):
                        self.appointment.has_plan = True
                        self.appointment.save(update_fields=['has_plan'])
                else:
                    _plan_later = self.handle_plan_later_response(incoming_message)
                if _plan_later:
                    next_question = self.get_next_question_to_ask()
                    if next_question != "complete":
                        reply = self.generate_contextual_response(
                            incoming_message,
                            next_question,
                            ['plan_status'],
                        )
                        reply = "Perfect! You can send your plan whenever you're ready. " + reply
                        return reply

                # ── STEP 3: UPDATE APPOINTMENT ────────────────────────────────────────
                updated_fields = self.update_appointment_with_extracted_data(
                    extracted_data,
                    incoming_message=incoming_message,
                )

                # ── EXCLUDED AREA ─────────────────────────────────────────────────────
                if 'excluded_area' in updated_fields:
                    import re as _re
                    _m    = _re.search(r'\[EXCLUDED_AREA:([^\]]+)\]',
                                       self.appointment.internal_notes or '')
                    _city = _m.group(1) if _m else 'that area'
                    reply = (
                        f"Thank you for reaching out! 😊 Unfortunately we currently only "
                        f"service the *Harare* area and don't cover *{_city}* at the moment.\n\n"
                        "If you ever have any plumbing work in Harare, we'd love to help! 🔧"
                    )
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply

                # If name was just captured on a confirmed appointment → ask for email
                # (or send confirmation directly if email already on file)
                if (
                    'customer_name' in updated_fields and
                    self.appointment.status == 'confirmed' and
                    self.appointment.scheduled_datetime
                ):
                    reply = self._confirm_or_request_email()
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply

                # ── STEP 4: RESCHEDULE CHECK (confirmed appointments only) ────────────
                if (self.appointment.status == 'confirmed' and
                        self.appointment.scheduled_datetime and
                        self.detect_reschedule_request_with_ai(incoming_message)):
                    print("🤖 AI detected reschedule request, handling...")
                    reschedule_response = self.handle_reschedule_request_with_ai(incoming_message)
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reschedule_response)
                    return reschedule_response

                # ── GARAGE / OUTBUILDING SERVICE-SCOPE QUESTION ───────────────────────
                _msg_lc = incoming_message.lower()
                _scope_q = any(q in _msg_lc for q in ('do you do', 'do u do', 'do you also', 'do u also', 'can you do', 'can u do'))
                if 'garage' in _msg_lc and (_scope_q or '?' in incoming_message):
                    reply = (
                        "Yes! We handle all plumbing work in garages and outbuildings — "
                        "sinks, water points, drainage, and pipework. 🔧\n\n"
                        "Is this for a garage at the same property, or is it a separate job?"
                    )
                    self.appointment.add_conversation_message("user", incoming_message)
                    self.appointment.add_conversation_message("assistant", reply)
                    return reply

                # ── STEPS 5 & 6: BOOK IF READY, OTHERWISE ASK NEXT QUESTION ─────────
                next_question  = self.get_next_question_to_ask()
                booking_status = self.smart_booking_check()

                if booking_status['ready_to_book'] and self.appointment.status != 'confirmed':
                    booking_result = self.book_appointment(incoming_message)
                    if booking_result['success']:
                        reply = (
                            "One last thing — what name should we put on the booking? "
                            "If you'd rather not share it, just say no."
                        )
                    else:
                        error        = booking_result.get('error', '')
                        alternatives = booking_result.get('alternatives', [])
                        if 'saturday' in error.lower() or not alternatives:
                            alt_text = (
                                "\n".join([f"• {alt['display']}" for alt in alternatives])
                                if alternatives else ""
                            )
                            reply = (
                                "We unfortunately don't operate on Saturdays. 😊\n\n"
                                "Our working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                            )
                            if alt_text:
                                reply += (
                                    f"Here are some available slots:\n{alt_text}\n\n"
                                    "Or feel free to suggest a different date and time!"
                                )
                            else:
                                reply += "Could you suggest a different day and time?"
                        else:
                            alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives])
                            reply = (
                                f"That slot just got taken — here are the next available times:\n"
                                f"{alt_text}\n\nWhich works better for you?"
                            )
                else:
                    if self._is_standalone_question(incoming_message):
                        PRODUCT_INTENTS = {
                            'tub_sales', 'standalone_tub', 'geyser', 'shower_cubicle',
                            'vanity', 'bathtub_installation', 'toilet', 'chamber',
                            'facebook_package', 'location_ask', 'location_visit',
                            'previous_quotation', 'pictures', 'combined_pricing',
                        }
                        _inquiry      = self.detect_service_inquiry(incoming_message)
                        _intent       = _inquiry.get('intent', 'none')
                        direct_answer = None
                        _from_service_inquiry = False
                        _already_sent = _intent in (getattr(self.appointment, 'sent_pricing_intents', None) or [])
                        if _intent in PRODUCT_INTENTS and _inquiry.get('confidence') == 'HIGH' and not _already_sent:
                            direct_answer = self.handle_service_inquiry(_intent, incoming_message)
                            _from_service_inquiry = bool(direct_answer)
                        if not direct_answer:
                            direct_answer = self._answer_standalone_question(incoming_message)
                            _from_service_inquiry = False
                        if direct_answer:
                            # handle_service_inquiry already ends with a followup question
                            # (via _build_pricing_response → _get_pricing_followup_prompt).
                            # Appending the nudge would stack a second question on top of it.
                            if _from_service_inquiry:
                                reply = direct_answer
                            else:
                                nudge = self._get_soft_booking_nudge()
                                reply = f"{direct_answer}\n\n{nudge}" if nudge else direct_answer
                        else:
                            reply = self.generate_contextual_response(
                                incoming_message, next_question, updated_fields
                            )
                    else:
                        reply = self.generate_contextual_response(
                            incoming_message, next_question, updated_fields
                        )

                # Guard: never return None or empty — send a safe fallback instead
                if not reply or not str(reply).strip():
                    reply = (
                        "Sorry, I didn't quite catch that. Could you tell me more about "
                        "what plumbing work you need? 😊"
                    )

                self.appointment.add_conversation_message("user", incoming_message)
                self.appointment.add_conversation_message("assistant", reply)
                return reply

            except Exception as e:
                print(f"❌ API Error: {str(e)}")
                return "Sorry, dropped that on our end — could you send that again?"


        def generate_contextual_response(self, incoming_message, next_question, updated_fields):
            """
            Generate the next bot message.

            retry_count == 0  → exact hardcoded first-pass question, no DeepSeek call.
            retry_count >= 1  → DeepSeek rephrases to match the customer's tone.
                                If the customer provided info this turn, open with a
                                thank-you + one contextual line before the question.
            """
            try:
                import pytz as _pytz

                retry_count = self._get_question_retry_count(next_question)
                sa_tz = _pytz.timezone('Africa/Johannesburg')

                saturday_indicators = ['saturday', 'sat']
                if any(s in incoming_message.lower() for s in saturday_indicators):
                    alternatives = self.get_alternative_time_suggestions(
                        timezone.now() + timedelta(days=1)
                    )
                    alt_text = (
                        "\n".join([f"• {alt['display']}" for alt in alternatives])
                        if alternatives else ""
                    )
                    reply = (
                        "We unfortunately don't operate on Saturdays. 😊\n\n"
                        "Our working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                    )
                    if alt_text:
                        reply += (
                            f"Here are some available slots:\n{alt_text}\n\n"
                            "Or feel free to suggest a different date and time!"
                        )
                    else:
                        reply += "Could you please choose a different day that works for you?"
                    return reply

                all_day_phrases = [
                    'available all day', 'whole day', 'all day', 'anytime',
                    'any time', 'free all day', 'i am free', 'im free',
                ]
                if (
                    next_question in ('availability_time', 'area', 'complete') and
                    self.appointment.scheduled_datetime and
                    any(p in incoming_message.lower() for p in all_day_phrases)
                ):
                    return self._handle_all_day_response()

                if next_question == "name":
                    if self.appointment.customer_name and 'customer_name' in (updated_fields or []):
                        return self._confirm_or_request_email()
                    if self._declines_sharing_name(incoming_message):
                        self._mark_customer_name_declined()
                        return (
                            "No problem at all. Your appointment is still confirmed — "
                            "we'll use this WhatsApp number for updates."
                        )
                    return (
                        "One last thing — what name should we put on the booking? "
                        "If you'd rather not share it, just say no."
                    )

                if retry_count == 0:
                    first_pass = self._get_first_pass_question(next_question)
                    if first_pass:
                        self._set_question_retry_count(next_question, 1)
                        return first_pass
                else:
                    # retry_count >= 1

                    # Human handoff after 4 failed extraction attempts
                    if retry_count >= 4:
                        return self._build_human_handoff_reply()

                    # Semantic rescue on first retry with no extracted data
                    if not updated_fields and retry_count == 1:
                        rescue_reply = self._try_semantic_rescue(incoming_message, next_question)
                        if rescue_reply:
                            self._set_question_retry_count(next_question, retry_count + 1)
                            return rescue_reply

                    # Classify availability-date replies before repeating options
                    if next_question == "availability_date":
                        handled = self._handle_availability_date_response(
                            incoming_message, retry_count
                        )
                        if handled is not None:
                            self._set_question_retry_count(next_question, retry_count + 1)
                            return handled

                new_retry = retry_count + 1
                self._set_question_retry_count(next_question, new_retry)

                return self._generate_retry_response(
                    incoming_message=incoming_message,
                    next_question=next_question,
                    updated_fields=updated_fields or [],
                    retry_count=new_retry,
                )

            except Exception as e:
                print(f"❌ Error generating contextual response: {str(e)}")
                return "Sorry, dropped that on our end — could you send that again?"


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
    - "not available", "can't do either", "neither works", "those don't work" → rejected_both
    - Picks one of the offered days → accepted_offered
    - Vague "ok" with no day mentioned → unclear

    Return ONLY valid JSON (no markdown):
    {{"intent": "accepted_offered|suggested_new_day|rejected_both|unclear", "day_mentioned": "DayName or null", "confidence": "HIGH|LOW"}}"""

            try:
                from bot.services.clients import deepseek_call
                raw = deepseek_call(
                    messages=[
                        {"role": "system", "content": "Return ONLY valid JSON. No markdown."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=60,
                    json_response=True,
                )
                raw = raw.replace("```json", "").replace("```", "").strip()
                result = json.loads(raw)
                print(f"🤖 Availability intent: {result}")
                return result
            except Exception as e:
                print(f"⚠️ Availability classification failed: {e}")
                return {"intent": "unclear", "day_mentioned": None, "confidence": "LOW"}


        def _handle_availability_date_response(self, message: str, retry_count: int):
            """
            Called when next_question == 'availability_date' AND the bot has already
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
                # Clear stored datetime so we don't re-offer the same days next turn
                if self.appointment.scheduled_datetime:
                    self.appointment.scheduled_datetime = None
                    self.appointment.save(update_fields=["scheduled_datetime"])
                self._set_question_retry_count("availability_date", 0)
                return "Oh okay 👍 when are you available? We're open Sunday–Friday, 8 AM–6 PM."

            if intent == "suggested_new_day" and day_mentioned and confidence == "HIGH":
                # Confirm the new day without repeating the original options
                return f"Do you mean this coming {day_mentioned}?"

            if (intent == "unclear" or confidence == "LOW") and retry_count >= 2:
                # After two failed attempts, ask open-ended rather than repeating
                return "When would work best for you? We're open Sunday–Friday, 8 AM–6 PM."

            # accepted_offered or first-retry unclear → fall through to normal logic
            return None


        def _get_first_pass_question(self, next_question: str) -> str:
            """
            Return the exact hardcoded first-pass question for a given question key.
            Returns None if the question key is unrecognised.
            These are sent verbatim on retry_count == 0 with no DeepSeek call.
            """
            if next_question == "service_type":
                return (
                    "Hello,\nHow may we assist you on plumbing services"
                )

            if next_question == "project_description":
                return f"Got it! {self._get_contextual_description_question()}"

            if next_question == "availability_date":
                days = self._get_next_two_available_days()
                day_a = self._format_day(days[0]) if len(days) > 0 else "tomorrow"
                day_b = self._format_day(days[1]) if len(days) > 1 else "the day after"
                visit_desc = self._describe_project_context()
                return (
                    f"Great, what works better for you — {day_a} or {day_b} — "
                    f"for us to come through and {visit_desc}?"
                )

            if next_question == "availability_time":
                dt = self.appointment.scheduled_datetime
                if dt:
                    selected_date = self._get_selected_local_date()
                    day_label = self._format_day(selected_date) if selected_date else "that day"
                    times = self._get_two_available_times_for_date(selected_date) if selected_date else []
                    time_a = times[0].strftime('%I%p').lstrip('0') if len(times) > 0 else "9AM"
                    time_b = times[1].strftime('%I%p').lstrip('0') if len(times) > 1 else "2PM"
                    return (
                        f"Perfect, for {day_label} — "
                        f"what works better: {time_a} or {time_b}?"
                    )
                return "What time works best for you — morning or afternoon?"

            if next_question == "area":
                return "All good, what area are you in?"

            return None


        def _generate_retry_response(
            self,
            incoming_message: str,
            next_question: str,
            updated_fields: list,
            retry_count: int,
        ) -> str:
            """
            Generate a retry response that:
            1. Opens with a thank-you + contextual line if the customer provided info.
            2. Rephrases the next question to match the customer's tone and wording style.
            3. Escalates naturally with each retry (simpler → choices → light urgency).

            Always uses DeepSeek. Falls back to a hardcoded rephrase on error.
            """
            info_provided = self._describe_info_provided(updated_fields)
            contextual_line = self._get_contextual_line(updated_fields, next_question)
            question_instruction = self._get_question_instruction(next_question, retry_count)

            msg_lower = incoming_message.lower()
            shona_markers = [
                'hongu', 'kwete', 'ndinoda', 'ndoda', 'chimbuzi', 'shawa',
                'bhavhu', 'kicheni', 'mauya', 'mangwana', 'mauro', 'zvakanaka',
            ]
            shona_count = sum(1 for m in shona_markers if m in msg_lower)
            language_note = (
                "The customer is writing in Shona — respond in Shona."
                if shona_count >= 2
                else "The customer is writing in mixed Shona/English — match their mix."
                if shona_count == 1 and len(msg_lower.split()) > 2
                else "The customer is writing in English — respond in English."
            )

            if info_provided and contextual_line:
                opening_instruction = (
                    f"Open with a brief thank-you for the information they provided "
                    f"({info_provided}), then add this specific contextual line: "
                    f"\"{contextual_line}\". Then ask the question below. "
                    f"Keep the whole message under 4 sentences."
                )
            elif info_provided:
                opening_instruction = (
                    f"Open with a brief, natural thank-you for the information they "
                    f"provided ({info_provided}). Then ask the question below. "
                    f"Keep it under 3 sentences."
                )
            else:
                opening_instruction = (
                    "Go straight to the question — no preamble. "
                    "The customer hasn't provided new information this turn."
                )

            if retry_count == 1:
                escalation = "Simplify the question slightly. Same intent, fresher phrasing."
            elif retry_count == 2:
                escalation = (
                    "Offer two explicit choices instead of an open question. "
                    "Make it very easy to answer."
                )
            elif retry_count >= 3:
                escalation = (
                    "Keep it to 1-2 sentences max. Add light urgency: "
                    "\"We're booking up this week.\" or similar real constraint."
                )
            else:
                escalation = "Natural rephrasing."

            prompt = f"""You are writing a WhatsApp message for Homebase Plumbers in Zimbabwe.

    CUSTOMER'S LAST MESSAGE: "{incoming_message}"

    WHAT TO DO:
    {opening_instruction}

    QUESTION TO ASK:
    {question_instruction}

    TONE RULES:
    - Mirror the customer's vocabulary and sentence length exactly
    - If they wrote 3 words, your question should be short too
    - If they wrote in full sentences, match that
    - Zimbabwean English ("sorted", "keen", "sharp")
    - {language_note}
    - No markdown, no bold, no bullet points in the question itself
    - One question only — never stack two questions
    - At most one emoji for retry 1-2, zero emoji for retry 3+
    - Never say "just checking in", "following up", "hope you're well"
    - Never use the customer's name (we may not know it)
    - Sound like a real person texting, not a bot

    RETRY COUNT: {retry_count} (higher = simpler and more direct)
    {escalation}

    Write ONLY the message text. No labels, no quotes around it."""

            try:
                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You write short WhatsApp messages for a plumbing company. "
                                "Match the customer's tone exactly. "
                                "Sound human. Never ask for the customer's name."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.5,
                    max_tokens=200,
                )
                reply = response.choices[0].message.content.strip()
                reply = reply.replace('**', '').replace('__', '')
                print(
                    f"🤖 Retry response | q={next_question} retry={retry_count} "
                    f"updated={updated_fields}"
                )
                if not reply:
                    return self._hardcoded_retry_fallback(next_question, retry_count)
                return reply

            except Exception as e:
                print(f"❌ DeepSeek retry response error: {e}")
                return self._hardcoded_retry_fallback(next_question, retry_count)


        def _describe_info_provided(self, updated_fields: list) -> str:
            """
            Return a human-readable summary of what the customer just provided,
            for use in the thank-you opening.
            """
            if not updated_fields:
                return ""

            field_labels = {
                'service_type': 'the type of service they need',
                'project_description': 'details about their project',
                'area': 'their area',
                'availability': 'their preferred time',
                'customer_name': 'their name',
                'property_type': 'their property type',
                'timeline': 'their timeline',
            }
            labels = [field_labels.get(f, f.replace('_', ' ')) for f in updated_fields]
            if len(labels) == 1:
                return labels[0]
            return ', '.join(labels[:-1]) + ' and ' + labels[-1]


        def _get_contextual_line(self, updated_fields: list, next_question: str) -> str:
            """
            Return a specific, relevant contextual line to add after the thank-you,
            before the next question. These lines make the bot feel human and informed
            rather than robotic.
            """
            if not updated_fields:
                return ""

            area = self.appointment.customer_area or ""
            service = (self.appointment.project_type or "").replace("_", " ").lower()
            desc = (self.appointment.project_description or "").lower()

            if 'area' in updated_fields and area:
                return (
                    f"We've actually done a number of renovations in {area} "
                    f"over the past month alone."
                )

            if 'service_type' in updated_fields:
                if 'bathroom' in service:
                    return "Bathroom renovations are actually our most popular service right now."
                if 'kitchen' in service:
                    return "Kitchen plumbing is one of our specialities — great choice."
                if 'installation' in service:
                    return "New installations are something we handle from scratch — no problem at all."
                return "That's actually one of the services we do most frequently."

            if 'project_description' in updated_fields:
                if any(w in desc for w in ('tiled', 'already tiled', 'existing')):
                    return (
                        "Since it's already tiled, the work focuses on fixtures and fittings "
                        "which keeps costs down."
                    )
                if any(w in desc for w in ('new', 'from scratch', 'building')):
                    return "Starting fresh gives us more flexibility with the layout — good to know."
                return "That gives us a much clearer picture of the job."

            if 'availability' in updated_fields and next_question == 'availability_time':
                return "That day works well on our side."

            if 'availability' in updated_fields and next_question == 'area':
                return "That time is noted — almost there."

            return ""


        def _get_question_instruction(self, next_question: str, retry_count: int) -> str:
            """
            Return the instruction for DeepSeek describing what question to ask next.
            Provides context-specific phrasing guidance per question.
            """
            if next_question == "service_type":
                if retry_count >= 3:
                    return (
                        "Ask a simpler angle: which room needs work — bathroom, kitchen, "
                        "or is this a new installation? Don't mention service names — "
                        "just ask about the room or whether it's new work."
                    )
                return (
                    "Ask which of our three services they need: "
                    "Bathroom Renovation, New Plumbing Installation, or Kitchen Renovation. "
                    "Don't list them as bullet points — weave them into a natural question."
                )

            if next_question == "project_description":
                specific_q = self._get_contextual_description_question()
                return (
                    f"Ask the following specific question about their project — do not rephrase it, "
                    f"just weave it in naturally: \"{specific_q}\""
                )

            if next_question == "availability_date":
                days = self._get_next_two_available_days()
                day_a = self._format_day(days[0]) if len(days) > 0 else "tomorrow"
                day_b = self._format_day(days[1]) if len(days) > 1 else "the day after"
                visit_desc = self._describe_project_context()
                return (
                    f"Ask whether {day_a} or {day_b} works better for a free on-site visit "
                    f"to {visit_desc}. Frame it as offering two specific options."
                )

            if next_question == "availability_time":
                selected_date = self._get_selected_local_date()
                day_label = self._format_day(selected_date) if selected_date else "that day"
                times = self._get_two_available_times_for_date(selected_date) if selected_date else []
                time_a = times[0].strftime('%I%p').lstrip('0') if len(times) > 0 else "9AM"
                time_b = times[1].strftime('%I%p').lstrip('0') if len(times) > 1 else "2PM"
                return (
                    f"Ask whether {time_a} or {time_b} works better on {day_label}. "
                    "Two options only — make it easy to reply."
                )

            if next_question == "area":
                return (
                    "Ask which suburb or area they are in. Keep it short — "
                    "just need the location to plan the visit."
                )

            if next_question == "name":
                return (
                    "Ask what name to put on the booking. "
                    "Mention they can decline if they prefer not to share."
                )

            return "Ask the most natural next question to move the booking forward."


        def _hardcoded_retry_fallback(self, next_question: str, retry_count: int) -> str:
            """
            Fallback retry questions used when DeepSeek is unavailable.
            Progressively simpler with each retry.
            """
            fallbacks = {
                'service_type': [
                    "Which service were you after — bathroom, kitchen, or a new installation?",
                    "Bathroom, kitchen, or new installation — which one?",
                    "Just to confirm — which service do you need?",
                ],
                'project_description': [
                    self._get_contextual_description_question(),
                    "What exactly needs doing — the more detail the better for the quote.",
                    "What's the main thing you want sorted?",
                ],
                'availability_date': [
                    "Which day works better for the site visit?",
                    "Would tomorrow or the day after suit you better?",
                    "What day works for you?",
                ],
                'availability_time': [
                    "Morning or afternoon — which works better for you?",
                    "What time suits you best?",
                    "Morning or afternoon?",
                ],
                'area': [
                    "Which area are you based in?",
                    "What suburb are you in?",
                    "Which area?",
                ],
            }
            options = fallbacks.get(next_question, ["What's the best next step for you?"])
            idx = min(retry_count - 1, len(options) - 1)
            return options[idx]


        def _try_semantic_rescue(self, message: str, next_question: str) -> str | None:
            """
            Run semantic rescue when extraction returned no useful fields.
            If rescue identifies a service_type it also persists it to the appointment.
            Returns a contextual reply string, or None to fall through to normal retry.
            """
            try:
                from bot.semantic_rescue import rescue as _rescue

                history = self.appointment.conversation_history or []
                lines = []
                for turn in history[-4:]:
                    role = "Customer" if turn.get("role") == "user" else "Bot"
                    content = (turn.get("content") or "").strip()[:80]
                    if content and not content.startswith("["):
                        lines.append(f"{role}: {content}")
                ctx = "\n".join(lines)

                result = _rescue(message, next_question=next_question, conversation_context=ctx)

                input_type = result.get("input_type", "unclear")
                svc        = result.get("service_type")
                reply      = result.get("suggested_reply")

                if input_type == "unclear" or not reply:
                    return None

                if svc and not self.appointment.project_type:
                    self.appointment.project_type = svc
                    self.appointment.save(update_fields=["project_type"])
                    logger.info(
                        "semantic_rescue: saved service_type=%s from input_type=%s",
                        svc, input_type,
                    )

                logger.info("semantic_rescue: returning rescue reply for input_type=%s", input_type)
                return reply

            except Exception as exc:
                logger.warning("_try_semantic_rescue error: %s", exc)
                return None


        def _build_human_handoff_reply(self) -> str:
            """Reply after 4 failed extraction attempts — offer direct human contact."""
            from bot.out_of_scope_handler import PLUMBER_NUMBER_FALLBACK
            number = (
                getattr(self.appointment, "plumber_contact_number", None) or PLUMBER_NUMBER_FALLBACK
            ).replace("+", "").replace("whatsapp:", "")
            return (
                f"Let me get the right person to help you directly — "
                f"you can reach Tinashe on +{number} and he'll sort it from there.\n\n"
                "Or just tell me in a few words what you need and I'll take it from there."
            )


        def validate_plan_status_with_ai(self, extracted_status: str, original_message: str) -> tuple:
            """
            Use AI to validate and normalize plan status responses
            Handles spelling mistakes, context, and ambiguous answers
        
            Args:
                extracted_status: The raw AI extraction ('yes', 'no', 'has_plan', etc.)
                original_message: The customer's original message
            
            Returns:
                tuple: (is_valid: bool, normalized_value: bool or None, confidence: str)
            """
            try:
                validation_prompt = f"""You are a plan status validation assistant for an appointment booking system.

        CONTEXT:
        We asked the customer: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"

        CUSTOMER'S RESPONSE: "{original_message}"
    system_prompt
        AI EXTRACTED VALUE: "{extracted_status}"

        TASK:
        Analyze the customer's response and determine:
        1. Did they answer the plan question?
        2. Do they HAVE a plan or do they NEED a site visit?
        3. How confident are you in this interpretation?

        ANALYSIS RULES:
        - Look at the MEANING, not just keywords
        - Handle spelling mistakes (e.g., "vist" = "visit", "pln" = "plan")
        - Handle context clues (e.g., "I'll send it" implies they have a plan)
        - Handle ambiguity (e.g., "maybe" or "not sure")
        - Ignore unrelated content (e.g., greetings, other questions)

        EXAMPLES:

        Customer: "A site visit would be ideal"
        Analysis: NEEDS_VISIT (customer wants site visit, doesn't have plan)
        Confidence: HIGH

        Customer: "yes i have one"
        Analysis: HAS_PLAN (customer confirms they have a plan)
        Confidence: HIGH

        Customer: "I'll send the blueprints later"
        Analysis: HAS_PLAN (implies they have plans to send)
        Confidence: HIGH

        Customer: "No, come see it first"
        Analysis: NEEDS_VISIT (customer wants visit first, no plan)
        Confidence: HIGH

        Customer: "I think so, let me check"
        Analysis: UNCLEAR (customer is uncertain)
        Confidence: LOW

        Customer: "How much will it cost?"
        Analysis: OFF_TOPIC (not answering the plan question)
        Confidence: N/A

        Customer: "yea I got da plan"
        Analysis: HAS_PLAN (spelling mistakes but clear intent)
        Confidence: HIGH

        Customer: "site vist would be better"
        Analysis: NEEDS_VISIT (spelling mistake but clear: site visit)
        Confidence: HIGH

        Customer: "No plan, need someone to come lok at it"
        Analysis: NEEDS_VISIT (no plan + wants someone to look = site visit)
        Confidence: HIGH

        RESPONSE FORMAT (CRITICAL - FOLLOW EXACTLY):
        Return ONLY a JSON object with this exact structure:
        {{
            "answer_provided": true/false,
            "interpretation": "HAS_PLAN" or "NEEDS_VISIT" or "UNCLEAR" or "OFF_TOPIC",
            "confidence": "HIGH" or "MEDIUM" or "LOW",
            "reasoning": "Brief explanation of your analysis"
        }}

        Do NOT include any other text, markdown, or explanations outside the JSON.

        CUSTOMER MESSAGE: "{original_message}"

        YOUR ANALYSIS:"""

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system", 
                            "content": "You are a precise validation assistant. Return ONLY valid JSON with no additional text or formatting."
                        },
                        {
                            "role": "user", 
                            "content": validation_prompt
                        }
                    ],
                    temperature=0.2,  # Low temperature for consistency
                    max_tokens=150
                )
            
                ai_response = response.choices[0].message.content.strip()
            
                # Clean up response (remove markdown if present)
                ai_response = ai_response.replace('```json', '').replace('```', '').strip()
            
                # Parse JSON response
                try:
                    validation_result = json.loads(ai_response)
                except json.JSONDecodeError as e:
                    print(f"❌ AI returned invalid JSON: {ai_response}")
                    print(f"JSON Error: {str(e)}")
                    return (False, None, "ERROR")
            
                # Extract results
                answer_provided = validation_result.get('answer_provided', False)
                interpretation = validation_result.get('interpretation', 'UNCLEAR')
                confidence = validation_result.get('confidence', 'LOW')
                reasoning = validation_result.get('reasoning', '')
            
                print(f"🤖 AI Validation Result:")
                print(f"   Answer provided: {answer_provided}")
                print(f"   Interpretation: {interpretation}")
                print(f"   Confidence: {confidence}")
                print(f"   Reasoning: {reasoning}")
            
                # Only accept HIGH or MEDIUM confidence answers
                if not answer_provided or confidence == 'LOW':
                    print(f"⚠️ Low confidence or no answer - will ask again")
                    return (False, None, confidence)
            
                # Convert interpretation to boolean
                if interpretation == 'HAS_PLAN':
                    normalized_value = True
                    is_valid = True
                elif interpretation == 'NEEDS_VISIT':
                    normalized_value = False
                    is_valid = True
                elif interpretation == 'UNCLEAR':
                    normalized_value = None
                    is_valid = False
                elif interpretation == 'OFF_TOPIC':
                    normalized_value = None
                    is_valid = False
                else:
                    print(f"❌ Unexpected interpretation: {interpretation}")
                    return (False, None, "ERROR")
            
                print(f"✅ Validated: has_plan = {normalized_value} (confidence: {confidence})")
                return (is_valid, normalized_value, confidence)
            
            except Exception as e:
                print(f"❌ AI validation error: {str(e)}")
                import traceback
                traceback.print_exc()
                return (False, None, "ERROR")


        def generate_clarifying_question_for_plan_status(self, retry_count: int) -> str:
            """
            Generate varied clarifying questions when plan status is unclear
            Uses different phrasing on retries to help customer understand
            """
            try:
                clarification_prompt = f"""You are a professional appointment assistant.

        SITUATION:
        You asked: "Do you have a plan(a picture of space or pdf) already, or would you like us to do a site visit?"
        The customer's response was unclear or off-topic.
        This is retry attempt #{retry_count + 1}

        TASK:
        Generate a NEW way to ask about whether they have an existing plan.

        PHRASING OPTIONS (use different ones for different retries):

        Retry 1 (Direct):
        "Just to clarify - do you already have plans/blueprints for your bathroom, or would you like us to visit first and create a plan?"

        Retry 2 (Explanation):
        "I need to know if you have existing plans (blueprints/drawings) that we should review, OR if you need us to come assess your space first. Which one?"

        Retry 3 (Simple Yes/No):
        "Quick question: Do you have plans/blueprints ready? 
        • Reply YES if you have plans to send us
        • Reply NO if you need us to visit and assess first"

        Retry 4 (Examples):
        "Let me explain the options:

        Option A: You already have architectural plans/blueprints → We review them first
        Option B: You don't have plans yet → We do a site visit to assess and create a plan

        Which option fits your situation - A or B?"

        REQUIREMENTS:
        - Keep it professional but friendly
        - Be clear and concise (2-3 sentences max)
        - Use language appropriate for retry #{retry_count + 1}
        - No markdown formatting
        - If retry > 3, use very simple YES/NO format

        Current retry: {retry_count}

        Generate the clarifying question:"""

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are a helpful appointment assistant. Generate clear, varied questions."
                        },
                        {
                            "role": "user",
                            "content": clarification_prompt
                        }
                    ],
                    temperature=0.8,  # Higher temp for variety
                    max_tokens=150
                )
            
                clarifying_question = response.choices[0].message.content.strip()
                print(f"🤖 Generated clarifying question (retry {retry_count}): {clarifying_question[:100]}...")
            
                return clarifying_question
            
            except Exception as e:
                print(f"❌ Error generating clarifying question: {str(e)}")
                # Fallback questions by retry count
                fallbacks = [
                    "Just to confirm - do you have plans already, or would you like us to do a site visit?",
                    "I need to know: do you have existing blueprints/plans, or should we visit your property first?",
                    "Simple question: Do you have plans? Reply YES or NO.",
                    "Option A: I have plans to send. Option B: I need a site visit. Which one - A or B?"
                ]
                return fallbacks[min(retry_count, len(fallbacks) - 1)]


        def detect_service_inquiry(self, message):
            """Use DeepSeek to detect if customer is asking about products/services/pricing."""
            try:
                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "You are an intent classifier for a Zimbabwean plumbing company. Customers may write in English, Shona, or mixed. Return ONLY valid JSON, no markdown."
                        },
                        {
                            "role": "user",
                            "content": f"""Classify the customer's message into ONE of these intents.

        Customer message: "{message}"

        "If the customer mentions multiple products (e.g. tub AND chamber, toilet AND
        shower), classify as 'bathtub_installation' if a tub is mentioned, otherwise
        pick the most prominent product.  Never return 'none' just because multiple
        products are mentioned — pick the most specific/expensive one."

        EXTRA CLASSIFICATION RULES:
        - Only choose an intent that is explicitly mentioned in the message.
        - Never return bathtub_installation unless the message explicitly mentions
          a tub, bathtub, bath, or freestanding tub.
        - If multiple non-tub products are mentioned, pick the clearest mentioned
          product instead of defaulting to bathtub_installation.

        INTENTS:
        - tub_sales: asking about tub price, cost, or availability — ANY message with "tub"
          and a price/cost signal. Examples: "how much tub", "tub price", "how much is a tub",
          "do you sell tubs", "tub cost", "how much for a tub", "tub supply and install"
        - standalone_tub: asking specifically about standalone or freestanding tub — price, size,
          dimensions, length, or any information. Examples: "freestanding tub price", "standalone
          tub how much", "free standing tub", "how big are your freestanding tubs", "how long is
          the tub", "what sizes do freestanding tubs come in"
        - geyser: asking about geyser installation or pricing
        - shower_cubicle: asking about shower cubicles, pricing, installation
        - vanity: asking about vanity units, custom vanity
        - bathtub_installation: asking about installing a bathtub, wall finishing around tub
        - toilet: asking about toilet supply or installation
        - chamber: asking about side chamber, chamber supply or installation
        - facebook_package: referencing a Facebook ad or package deal
        - location_ask: customer is ONLY asking where we are located or for our address
        - location_visit: customer wants to physically come IN PERSON to our office or showroom
        - previous_quotation: saying we sent them a quotation before
        - pictures: asking to see product pictures (not previous work photos)
        - drain_unblocking: blocked drain, clogged drain, drain not flowing,
          unblock drain, drainage problem, sewer blocked, sewage backup, blocked pipe
        - pipe_repair: leaking pipe, burst pipe, pipe leak, broken pipe, water leak,
          pipe burst, fix pipe, pipe replacement, leaking tap, dripping tap
        - geyser_repair: geyser not working, geyser broken, fix geyser, geyser leaking,
          no hot water, geyser problem, water heater broken, geyser tripping
        - toilet_repair: toilet not flushing, toilet leaking, broken toilet, fix toilet,
          cistern not filling, toilet running, toilet broken, toilet problem
        - combined_pricing: asking for total/combined cost, a full quotation, or general pricing,
          e.g. "how much for all", "how much zvese zvakadai", "zvese izvi zvinodhura marii",
          "total for everything", "all together how much", "what's the total",
          "I want a quotation", "send me a quote", "I need a quote", "ndida quotation",
          "how much overall", "how much is everything", "marii zvese"
        - none: none of the above

        CRITICAL RULES:
        1. location_ask vs location_visit:
        - location_ask = ONLY asking for address/whereabouts. Examples:
            * "Where are you located"
            * "Whre ar u located"
            * "Where are you based"
            * "What's your address"
            * "Muri kupi" (Shona: where are you)
            * "Muri kupi imimi"
        - location_visit = customer explicitly wants to come in person. Examples:
            * "Can I come to your office"
            * "Ko when can I come ku office"
            * "I want to visit your showroom"
            * "Can I come and see the tubs"
            * "When can I come in"

        ⚠️ IMPORTANT EXCEPTIONS — these are NOT location_visit:
        * 'Site visit' alone = customer is answering a plan question (needs site visit to their property)
        * 'Site visit would be perfect' = same
        * 'I need a site visit' = same
        These should return intent: 'none'"
    
        - If message is ONLY an area name like "Hatfield", "Avondale", "Glen View" → intent must be "none"

        2. Confidence rules:
        - HIGH = message clearly matches the intent. Short messages naming a specific
          product are HIGH — product names are unambiguous regardless of length.
          bathtub_installation is only valid when the message explicitly mentions
          a tub, bathtub, bath, or freestanding tub.
          Examples that are HIGH confidence:
            * "how much tub", "tub price", "tub cost"
            * "geyser install", "geyser price", "how much geyser"
            * "toilet price", "how much toilet", "toilet cost"
            * "shower cubicle price", "how much shower"
            * "chamber price", "side chamber cost"
            * "vanity price", "how much vanity"
            * "bathtub install", "bath installation"
            * "facebook package", "the package"
            * "where are you", "your address", "where are you located"
            * "can I come", "can I visit your office"
            * "send pictures", "show me photos", "got pics"
            * "how much zvese", "zvese zvakadai", "how much for all", "total for everything"
            * "I want quotation", "send me a quote", "I need a quote", "ndida quotation"
            * "how much" (standalone, no product mentioned)
        - LOW = message is genuinely ambiguous and could match multiple intents
          or no specific product/service
      
        Return ONLY this JSON:
        {{
            "intent": "one of the intents above",
            "confidence": "HIGH or LOW"
        }}"""
                        }
                    ],
                    temperature=0.1,
                    max_tokens=50,
                    response_format={"type": "json_object"},
                )

                ai_response = response.choices[0].message.content.strip()
                ai_response = ai_response.replace('```json', '').replace('```', '').strip()
                result = json.loads(ai_response)

                message_lower = (message or '').lower()
                tub_terms = ('tub', 'bathtub', 'bath', 'freestanding tub', 'free-standing tub')
                if result.get('intent') == 'bathtub_installation' and not any(term in message_lower for term in tub_terms):
                    if 'shower' in message_lower:
                        result = {"intent": "shower_cubicle", "confidence": result.get('confidence', 'HIGH')}
                    elif 'chamber' in message_lower:
                        result = {"intent": "chamber", "confidence": result.get('confidence', 'HIGH')}
                    elif 'toilet' in message_lower:
                        result = {"intent": "toilet", "confidence": result.get('confidence', 'HIGH')}
                    else:
                        result = {"intent": "none", "confidence": "LOW"}

                print(f"🤖 Service inquiry detection: '{message}' → {result}")
                return result

            except Exception as e:
                print(f"❌ Service inquiry detection error: {str(e)}")
                return {"intent": "none", "confidence": "LOW"}


        @staticmethod
        def _is_asking_for_price(message: str) -> bool:
            """Return True only when the customer explicitly asks about cost/price."""
            msg = (message or "").lower()
            price_keywords = (
                'price', 'cost', 'how much', 'charge', 'fee', 'rate',
                'mutengo', 'mbozha', 'dollar', ' usd', '$', 'cheap', 'afford',
                'estimate', 'quote', 'pricing', 'what does', 'what do you charge',
                'expensive', 'budget',
            )
            return any(kw in msg for kw in price_keywords)

        @staticmethod
        def _is_asking_for_size(message: str) -> bool:
            """Return True when the customer is asking about tub dimensions or sizes."""
            msg = (message or "").lower()
            size_keywords = (
                'how big', 'what size', 'what sizes', 'dimensions', 'how large',
                'how wide', 'how long', 'size', 'big', 'length', 'width', 'cm',
                'mm', 'metre', 'meter', 'fit', 'fits', 'will it fit',
            )
            return any(kw in msg for kw in size_keywords)


        def handle_service_inquiry(self, intent, message):
                """Generate response for product/service/pricing inquiries in English or Shona."""
                try:
                    # Detect language
                    lang_response = deepseek_client.chat.completions.create(
                        model=settings.DEEPSEEK_MODEL,
                        messages=[
                            {
                                "role": "system",
                                "content": "Detect the language of this message. Reply with ONLY 'shona', 'english', or 'mixed'."
                            },
                            {
                                "role": "user",
                                "content": message
                            }
                        ],
                        temperature=0.1,
                        max_tokens=5
                    )
                    language = lang_response.choices[0].message.content.strip().lower()
                    print(f"🌍 Detected language: {language}")

                    plumber_number = self.appointment.plumber_contact_number or '+263774819901'

                    # Has the customer already committed to a site visit or given their location?
                    already_visiting = self.appointment.has_plan is False
                    has_area = bool(self.appointment.customer_area)
                    visit_committed = already_visiting or has_area

                    #
                    structured_pricing = {
                        "tub_sales": {
                            "breakdown_lines": [
                                "Freestanding tub: Supply US$400 | Mixer US$150 | Install US$120 → from US$670 all-in",
                                "Standard built-in tub: Supply from US$80 | Install from US$80 → from US$160 all-in",
                                "Side chamber (add-on): Supply from US$130 | Install from US$30 → from US$160",
                            ],
                            "total_line": "Freestanding tubs from US$400 — mixer US$150, install US$120. Standard tubs from US$80, install from US$80.",
                            "cheapest_line": "Side chamber adds US$130 supply + US$30 install.",
                            "sn_breakdown_lines": [
                                "Freestanding tub: Supply US$400 | Mixer US$150 | Install US$120 → kubva US$670 all-in",
                                "Standard tub: Supply kubva US$80 | Install kubva US$80 → kubva US$160 all-in",
                                "Side chamber (add-on): Supply kubva US$130 | Install kubva US$30 → kubva US$160",
                            ],
                            "sn_total_line": "Full freestanding setup kubva US$670. Standard tub kubva US$160 all-in.",
                            "sn_cheapest_line": "Starting point i standard tub paUS$80 supply + US$80 install.",
                        },
                        "standalone_tub": {
                            "breakdown_lines": [
                                "Freestanding tub: Supply US$400 | Mixer US$150 | Install US$120 → from US$670 all-in",
                                "Standard built-in tub: Supply from US$80 | Install from US$80 → from US$160 all-in",
                                "Side chamber (add-on): Supply from US$130 | Install from US$30 → from US$160",
                            ],
                            "total_line": "Full freestanding setup from US$670 all-in.",
                            "cheapest_line": "If budget is tight, the standard built-in tub starts from US$160 all-in.",
                            "sn_breakdown_lines": [
                                "Freestanding tub: Supply US$400 | Mixer US$150 | Install US$120 → kubva US$670 all-in",
                                "Standard tub: Supply kubva US$80 | Install kubva US$80 → kubva US$160 all-in",
                                "Side chamber (add-on): Supply kubva US$130 | Install kubva US$30 → kubva US$160",
                            ],
                            "sn_total_line": "Full freestanding setup kubva US$670 all-in.",
                            "sn_cheapest_line": "Budget option i standard built-in tub kubva US$160 all-in.",
                        },
                        "geyser": {
                            "breakdown_lines": [
                                "Geyser: Supply from US$80, Install from US$80",
                            ],
                            "total_line": "Geysers start from US$160 all-in — supply and install.",
                            "cheapest_line": "Already have the geyser? Install-only from US$80.",
                            "sn_breakdown_lines": [
                                "Geyser: Supply kubva US$80, Install kubva US$80",
                            ],
                            "sn_total_line": "Geysers dzinotangira paUS$160 all-in — supply ne install.",
                            "sn_cheapest_line": "Muchitova ne geyser? Install chete kubva US$80.",
                        },
                        "shower_cubicle": {
                            "breakdown_lines": [
                                "Shower cubicle: Supply from US$130, Install from US$40",
                            ],
                            "total_line": "Shower cubicles start from US$170 all-in — supply and install.",
                            "cheapest_line": "Already have the cubicle? Install-only from US$40.",
                            "sn_breakdown_lines": [
                                "Shower cubicle: Supply kubva US$130, Install kubva US$40",
                            ],
                            "sn_total_line": "Shower cubicles dzinotangira paUS$170 all-in — supply ne install.",
                            "sn_cheapest_line": "Muchitova ne cubicle? Install chete kubva US$40.",
                        },
                        "vanity": {
                            "breakdown_lines": [
                                "Vanity unit: Supply from US$150, Install from US$30",
                            ],
                            "total_line": "Vanities start from US$180 all-in — supply and install.",
                            "cheapest_line": "Already have the unit? Install-only from US$30.",
                            "sn_breakdown_lines": [
                                "Vanity unit: Supply kubva US$150, Install kubva US$30",
                            ],
                            "sn_total_line": "Vanities dzinotangira paUS$180 all-in — supply ne install.",
                            "sn_cheapest_line": "Muchitova ne vanity? Install chete kubva US$30.",
                        },
                        "bathtub_installation": {
                            "breakdown_lines": [
                                "Freestanding tub: Supply US$400 | Mixer US$150 | Install US$120 → from US$670 all-in",
                                "Standard built-in tub: Supply from US$80 | Install from US$80 → from US$160 all-in",
                                "Side chamber (add-on): Supply from US$130 | Install from US$30 → from US$160",
                            ],
                            "total_line": "Full freestanding setup from US$670. Standard tub from US$160 all-in.",
                            "cheapest_line": "Standard built-in tub is the entry point at US$160 all-in.",
                            "sn_breakdown_lines": [
                                "Freestanding tub: Supply US$400 | Mixer US$150 | Install US$120 → kubva US$670 all-in",
                                "Standard tub: Supply kubva US$80 | Install kubva US$80 → kubva US$160 all-in",
                                "Side chamber (add-on): Supply kubva US$130 | Install kubva US$30 → kubva US$160",
                            ],
                            "sn_total_line": "Full freestanding setup kubva US$670. Standard tub kubva US$160 all-in.",
                            "sn_cheapest_line": "Standard built-in tub i entry point paUS$160 all-in.",
                        },
                        "toilet": {
                            "breakdown_lines": [
                                "Toilet seat: Supply from US$50, Install from US$20",
                            ],
                            "total_line": "Toilet replacement starts from US$70 all-in — supply and install.",
                            "cheapest_line": "Already have the toilet? Install-only from US$20.",
                            "sn_breakdown_lines": [
                                "Toilet seat: Supply kubva US$50, Install kubva US$20",
                            ],
                            "sn_total_line": "Zvingangoita US$70 yezvinhu zvese pa standard toilet replacement.",
                            "sn_cheapest_line": "Cheapest option installation chete kana muchitova ne toilet — labour inotangira paUS$20.",
                        },
                        "chamber": {
                            "breakdown_lines": [
                                "Side chamber: Supply from US$130, Install from US$30",
                            ],
                            "total_line": "Side chambers start from US$160 all-in — supply and install.",
                            "cheapest_line": "Already have the chamber? Install-only from US$30.",
                            "sn_breakdown_lines": [
                                "Side chamber: Supply kubva US$130, Install kubva US$30",
                            ],
                            "sn_total_line": "Zvingangoita US$160 yezvinhu zvese pa standard chamber setup.",
                            "sn_cheapest_line": "Cheapest option installation chete kana muchitova ne chamber — labour inotangira paUS$30.",
                        },
                        "facebook_package": {
                            "breakdown_lines": [
                                "Shower cubicle: Supply from US$130, Install from US$40",
                                "Vanity unit: Supply from US$150, Install from US$30",
                                "Toilet seat: Supply from US$50, Install from US$20",
                                "Side chamber: Supply from US$130, Install from US$30",
                                "Tub: Supply from US$80, Install from US$80",
                                "Freestanding tub: supply from US$400, mixer from US$150, install US$120",
                            ],
                            "total_line": "The Facebook package is US$600 — freestanding tub and side chamber.",
                            "cheapest_line": "We'll give you the exact price once we've seen the space.",
                            "sn_breakdown_lines": [
                                "Shower cubicle: Supply kubva US$130, Install kubva US$40",
                                "Vanity unit: Supply kubva US$150, Install kubva US$30",
                                "Toilet seat: Supply kubva US$50, Install kubva US$20",
                                "Side chamber: Supply kubva US$130, Install kubva US$30",
                                "Tub: Supply kubva US$80, Install kubva US$80",
                                "Free-standing tub mixer: Supply kubva US$150, Install kubva US$120",
                            ],
                            "sn_total_line": "Facebook package inosvika US$600 — freestanding tub ne side chamber.",
                            "sn_cheapest_line": "Cheapest option i basic package inotangira paUS$600 zvinhu zvekuwedzera zvisati zvaiswa.",
                        },
                        "drain_unblocking": {
                            "breakdown_lines": [
                                "Simple blockage (sink, basin, shower): Labour from US$20",
                                "Severe blockage (main drain, sewer line): Labour from US$50",
                                "High-pressure jetting (stubborn blockages): from US$80",
                            ],
                            "total_line": "Most drain unblocking jobs start from US$20 for labour — the exact cost depends on how severe and where the blockage is.",
                            "cheapest_line": "A basic sink or basin unblocking starts from US$20 labour.",
                            "sn_breakdown_lines": [
                                "Simple blockage (sink, basin, shower): Labour kubva US$20",
                                "Severe blockage (main drain, sewer line): Labour kubva US$50",
                                "High-pressure jetting: kubva US$80",
                            ],
                            "sn_total_line": "Zvingangoita US$20 kubva pa labour — zvichienderana nekubinya uye nzvimbo yekubikira.",
                            "sn_cheapest_line": "Basic sink kana basin unblocking inotangira paUS$20 labour.",
                        },
                        "pipe_repair": {
                            "breakdown_lines": [
                                "Minor leak repair (joint, fitting): Labour from US$20",
                                "Burst pipe repair: Labour from US$40",
                                "Pipe section replacement: Labour from US$50",
                                "Leaking tap washer/cartridge replacement: from US$15",
                            ],
                            "total_line": "Pipe repairs start from US$15–$20 for minor leaks — cost depends on the pipe size, location, and how accessible it is.",
                            "cheapest_line": "A leaking tap repair starts from US$15 labour.",
                            "sn_breakdown_lines": [
                                "Minor leak repair (joint, fitting): Labour kubva US$20",
                                "Burst pipe repair: Labour kubva US$40",
                                "Pipe section replacement: Labour kubva US$50",
                                "Leaking tap: kubva US$15",
                            ],
                            "sn_total_line": "Pipe repairs dzinotangira paUS$15–$20 pa minor leaks — zvichienderana ne pipe size, nzvimbo uye kuti inofashikira here.",
                            "sn_cheapest_line": "Leaking tap repair inotangira paUS$15 labour.",
                        },
                        "geyser_repair": {
                            "breakdown_lines": [
                                "Thermostat replacement: from US$30 labour + parts",
                                "Element replacement: from US$40 labour + parts",
                                "Pressure valve replacement: from US$25 labour + parts",
                                "Full geyser replacement: from US$350 (supply + install)",
                            ],
                            "total_line": "Geyser repairs start from US$25–$40 for labour + parts depending on what needs fixing. If the geyser needs replacing, full supply and install starts from US$350.",
                            "cheapest_line": "Minor repairs like a valve or thermostat start from US$25–$30.",
                            "sn_breakdown_lines": [
                                "Thermostat replacement: kubva US$30 labour + zvikamu",
                                "Element replacement: kubva US$40 labour + zvikamu",
                                "Pressure valve replacement: kubva US$25 labour + zvikamu",
                                "Full geyser replacement: kubva US$350 (supply + install)",
                            ],
                            "sn_total_line": "Geyser repairs dzinotangira paUS$25–$40 pa labour + zvikamu zvichienderana nezvinoda kugadzirwa.",
                            "sn_cheapest_line": "Minor repairs dzinotangira paUS$25–$30.",
                        },
                        "toilet_repair": {
                            "breakdown_lines": [
                                "Cistern repair (filling valve, flush valve): from US$20 labour + parts",
                                "Toilet seat replacement: Supply from US$20, fit from US$10",
                                "Leaking toilet base: Labour from US$25",
                                "Full toilet replacement: Supply from US$60, install from US$40",
                            ],
                            "total_line": "Toilet repairs start from US$20 for labour + parts. A full replacement (supply and fit) starts from US$100.",
                            "cheapest_line": "A cistern repair starts from US$20 labour + parts.",
                            "sn_breakdown_lines": [
                                "Cistern repair: kubva US$20 labour + zvikamu",
                                "Toilet seat replacement: Supply kubva US$20, fit kubva US$10",
                                "Leaking toilet base: Labour kubva US$25",
                                "Full toilet replacement: Supply kubva US$60, install kubva US$40",
                            ],
                            "sn_total_line": "Toilet repairs dzinotangira paUS$20 pa labour + zvikamu.",
                            "sn_cheapest_line": "Cistern repair inotangira paUS$20 labour + zvikamu.",
                        },
                    }
                    # combined_pricing always delegates to generate_pricing_overview
                    # for the full contextual Facebook-anchored response
                    if intent == 'combined_pricing':
                        return self.generate_pricing_overview(message)

                    # Facebook package — always return the anchor price and move to sale
                    if intent == 'facebook_package':
                        if language == 'shona':
                            return (
                                "Facebook package yedu inosvika US$600.\n\n"
                                f"{self._get_pricing_followup_prompt('shona')}"
                            )
                        return (
                            "Our Facebook package is US$600.\n\n"
                            f"{self._get_pricing_followup_prompt('english')}"
                        )

                    # Tub inquiries: answer size questions, then prices, then type
                    _TUB_INTENTS = {'tub_sales', 'standalone_tub', 'bathtub_installation'}
                    if intent in _TUB_INTENTS:
                        if self._is_asking_for_size(message):
                            if language == 'shona':
                                return (
                                    "Standard built-in tubs dzinouya mu1500×700mm, 1600×700mm ne1700×750mm.\n\n"
                                    "Freestanding tubs dzinobva ku1500×750mm kusvika 1800×800mm — "
                                    "inonyanya kusanangurwa i1700mm.\n\n"
                                    "Unoshanda neipi saizi?"
                                )
                            return (
                                "Standard built-in tubs come in 1500×700mm, 1600×700mm and 1700×750mm.\n\n"
                                "Freestanding tubs run from 1500×750mm up to 1800×800mm — "
                                "the most popular is 1700mm.\n\n"
                                "Which size works for your space?"
                            )
                        elif not self._is_asking_for_price(message):
                            if language == 'shona':
                                return (
                                    "Tubs dzinouya mumhando mbiri — "
                                    "standard built-in (inoiswa mumadziro) ne freestanding (inomira yega).\n\n"
                                    "Unofunga mhando ipi?"
                                )
                            return (
                                "Bathtubs come in two main types — "
                                "standard built-in (set into the wall surround) and freestanding (standalone).\n\n"
                                "Which type are you thinking?"
                            )
                        else:
                            if language == 'shona':
                                return (
                                    "Freestanding tubs dzinotangira paUS$400 — mixer US$150, install US$120.\n\n"
                                    "Standard tubs kubva US$80, install kubva US$80.\n\n"
                                    "Munoda chii chaizvo?"
                                )
                            return (
                                "Freestanding tubs start from US$400 — mixer US$150, install US$120.\n\n"
                                "Standard tubs from US$80, install from US$80.\n\n"
                                "What did you have in mind?"
                            )

                    if intent in structured_pricing:
                        pricing_payload = structured_pricing[intent]
                        if language == 'shona':
                            return self._build_pricing_response(
                                breakdown_lines=pricing_payload.get("sn_breakdown_lines", pricing_payload["breakdown_lines"]),
                                total_line=pricing_payload.get("sn_total_line", pricing_payload["total_line"]),
                                cheapest_line=pricing_payload.get("sn_cheapest_line", pricing_payload["cheapest_line"]),
                                visit_committed=visit_committed,
                                language="shona",
                            )
                        return self._build_pricing_response(
                            breakdown_lines=pricing_payload["breakdown_lines"],
                            total_line=pricing_payload["total_line"],
                            cheapest_line=pricing_payload["cheapest_line"],
                            visit_committed=visit_committed,
                        )

                    # ── Pricing responses ──
                    # Two variants per intent where relevant:
                    #   "en" / "sn"         → standard (visit not yet committed)
                    #   "en_v" / "sn_v"     → visit committed (drop the site-visit pitch)

                    pricing_info = {

                        "tub_sales": {
                            "en": (
                                "Tubs start from US$400 supply-only, or US$500–$800 supply + install — "
                                "depends on the style and size. \n\n"
                                "Do you know what size space you're working with, or would it be easier "
                                "to have us come measure and give you a fixed price on the spot? "
                                "(Site assessment is free)"
                            ),
                            "en_v": (
                                "Tubs start from US$400 supply-only, or US$500–$800 supply + install — "
                                "depends on the style and size. \n\n"
                                "Our plumber will go through the options with you when they come out."
                            ),
                            "sn": (
                                "Tubs dzinotangira kuUS$400 supply chete, kana US$500–$800 supply neinstallation — "
                                "zvichienda nemhando neukuru. \n\n"
                                "Unoziva ukuru hwenzvimbo yako here, kana tiuye tiite free assessment "
                                "tikupe mutengo wakakwana pasite?"
                            ),
                            "sn_v": (
                                "Tubs dzinotangira kuUS$400 supply chete, kana US$500–$800 supply neinstallation. \n\n"
                                "Plumber wedu achakuratidza zvinosarudzwa paauya."
                            ),
                        },

                        "standalone_tub": {
                            "en": (
                                "Standalone / freestanding tubs run US$400–$800 supply, plus US$120–$200 "
                                "to fit and finish. \n\n"
                                "Full breakdown:\n"
                                "• Free-standing tub supply: from US$400\n"
                                "• Free-standing mixer: from US$150\n"
                                "• tub installation: US$120\n"
                                "• Side chamber: US$130 (installation US$30)\n\n"
                                "Most customers are all-in at US$750–$1,200 depending on the tub they pick.\n\n"
                                "Do you already know which tub style you want, or would you like us to come "
                                "out and show you options on-site? (Free visit, no obligation)"
                            ),
                            "en_v": (
                                "Standalone / freestanding tubs run US$400–$800 supply, plus US$120–$200 "
                                "to fit and finish. \n\n"
                                "Full breakdown:\n"
                                "• Free-standing tub supply: from US$400\n"
                                "• Free-standing mixer: from US$150\n"
                                "• Mixer + tub installation: US$120\n"
                                "• Side chamber: US$130 (installation US$30)\n\n"
                                "Most customers are all-in at US$750–$1,200 depending on the tub they pick.\n\n"
                                "Our plumber will go through the options with you on-site."
                            ),
                            "sn": (
                                "Free-standing tubs dzinotangira kuUS$400 supply, neUS$120–$200 "
                                "yeinstallation. \n\n"
                                "• Free-standing tub: kubva US$400\n"
                                "• Free-standing mixer: kubva US$150\n"
                                "• Kuisa mixer netub: US$120\n"
                                "• Side chamber: US$130 (installation US$30)\n\n"
                                "Vazhinji vanobhadhara US$750–$1,200 zvichienda netub yavasarudza.\n\n"
                                "Unoziva mhando yetub yaungada here, kana tiuye tikuratidze zvinosarudzwa pasite?"
                            ),
                            "sn_v": (
                                "Free-standing tubs dzinotangira kuUS$400 supply, neUS$120–$200 yeinstallation. \n\n"
                                "• Free-standing tub: kubva US$400\n"
                                "• Free-standing mixer: kubva US$150\n"
                                "• Kuisa mixer netub: US$120\n"
                                "• Side chamber: US$130 (installation US$30)\n\n"
                                "Vazhinji vanobhadhara US$750–$1,200. Plumber wedu achakuratidza paauya."
                            ),
                        },

                        "geyser": {
                            "en": (
                                "Geyser installation starts from US$80 — most jobs land between US$80–$180 "
                                "depending on the geyser size and access. 🔥\n\n"
                                "What size geyser are you putting in? (100L, 150L, 200L?) — "
                                "that'll let me give you a tighter number right now."
                            ),
                            "sn": (
                                "Kuisa geyser kunotangira kuUS$80 — mazhinji mapoka anosvika US$80–$180 "
                                "zvichienda nekukura kwegeyser. 🔥\n\n"
                                "Geyser yaunoda yakura zvakadini? (100L, 150L, 200L?) — "
                                "ndingakupe mutengo wakajika zviri nani."
                            ),
                        },

                        "shower_cubicle": {
                            "en": (
                                "Shower cubicles (900×900mm) start from US$130 supply + US$40 install — "
                                "so roughly US$170 all-in for a standard fit. 🚿\n\n"
                                "Bigger cubicles or custom sizes run a bit more. "
                                "Do you know the rough dimensions, or should we come out and measure? "
                                "(Free site visit)"
                            ),
                            "en_v": (
                                "Shower cubicles (900×900mm) start from US$130 supply + US$40 install — "
                                "roughly US$170 all-in for a standard fit. 🚿\n\n"
                                "Bigger or custom sizes run a bit more. Our plumber will measure up "
                                "and confirm the exact price when they come out."
                            ),
                            "sn": (
                                "Shower cubicles (900×900mm) dzinotangira kuUS$130 supply neUS$40 installation — "
                                "pamwe US$170 yese. 🚿\n\n"
                                "Huru dzakakura dzinoti nzira dzinopfuura. "
                                "Unoziva saizi here, kana tiuye tiite free visit tiite measurement?"
                            ),
                            "sn_v": (
                                "Shower cubicles dzinotangira kuUS$170 yese ye900×900mm. 🚿\n\n"
                                "Plumber wedu achaveza uye akupe mutengo wakajika paauya."
                            ),
                        },

                        "vanity": {
                            "en": (
                                "Custom vanity units start from US$150 + US$30 labour — "
                                "most jobs come out at US$180–$350 depending on size and finish. 🪞\n\n"
                                "What size are you thinking? (Width in cm helps, even roughly)"
                            ),
                            "sn": (
                                "Ma vanity unit anotangira kuUS$150 neUS$30 yevashandi — "
                                "mazhinji mapoka anosvika US$180–$350 zvichienda nekukura nekugadzirwa. 🪞\n\n"
                                "Unofunga ukuru hwakaita sei? (Upamhi mucm unobatsira, kunyangwe wakangofanana)"
                            ),
                        },

                        "bathtub_installation": {
                            "en": (
                                "Bathtub installation runs US$80–$200 depending on the type: \n\n"
                                "• Ordinary tub (with wall finishing): from US$80\n"
                                "• Free-standing tub supply: from US$400\n"
                                "• Free-standing mixer: from US$150\n"
                                "• Mixer installation: US$120\n"
                                "• Side chamber: US$130 (install US$30)\n\n"
                                "What type of tub are you going with — standard built-in or freestanding?"
                            ),
                            "sn": (
                                "Kuisa bathtub kunosvika US$80–$200 zvichienda nemhando: \n\n"
                                "• Tub yakajairwa (ine wall finishing): kubva US$80\n"
                                "• Free-standing tub: kubva US$400\n"
                                "• Free-standing mixer: kubva US$150\n"
                                "• Kuisa mixer: US$120\n"
                                "• Side chamber: US$130 (install US$30)\n\n"
                                "Unoda mhando ipi — yakavakirwa mumadziro kana inomira yega?"
                            ),
                        },

                        "toilet": {
                            "en": (
                                "Toilet supply + install runs US$70–$120 for a standard close-coupled unit: 🚽\n\n"
                                "• Close-coupled toilet supply: from US$50\n"
                                "• Installation: from US$20\n"
                                "• Side chamber: US$130 (install US$30)\n\n"
                                "Are you replacing an existing toilet or fitting a new one in a fresh space?"
                            ),
                            "sn": (
                                "Toilet supply neinstallation inosvika US$70–$120 yetoilet yakajairwa: 🚽\n\n"
                                "• Close-coupled toilet: kubva US$50\n"
                                "• Kuisa: kubva US$20\n"
                                "• Side chamber: US$130 (install US$30)\n\n"
                                "Uri kutsiva toilet yaimbopo kana kuisa itsva munzvimbo itsva?"
                            ),
                        },

                        "chamber": {
                            "en": (
                                "Side chamber supply + install is US$160 all-in (US$130 supply, US$30 fit). 🚽\n\n"
                                "If you also need a toilet: close-coupled units start from US$50 supply + US$20 install.\n\n"
                                "Are you just doing the chamber, or the full toilet setup?"
                            ),
                            "sn": (
                                "Side chamber supply neinstallation ndiUS$160 yese (US$130 supply, US$30 kuisa). 🚽\n\n"
                                "Kana uchidawo toilet: close-coupled toilet inotangira kuUS$50 supply neUS$20 installation.\n\n"
                                "Uri kuita chamber chete kana setup yese yetoilet?"
                            ),
                        },

                        "facebook_package": {
                            "en": (
                                "The bathroom package from our Facebook ad starts from US$600. 📢\n\n"
                                "That covers the core fit-out — exact price depends on the size of your bathroom "
                                "and fixtures you choose.\n\n"
                                "Want us to come do a free on-site assessment so we can lock in your exact number?"
                            ),
                            "en_v": (
                                "The bathroom package from our Facebook ad starts from US$600. 📢\n\n"
                                "Exact price depends on your bathroom size and fixtures. "
                                "Our plumber will lock in your exact price when they come out."
                            ),
                            "sn": (
                                "Package yebathroom yatakaiswa pa Facebook inotangira kuUS$600. 📢\n\n"
                                "Iyo inofukidza basa guru — mutengo wakakwana unoenderana nekukura kwebathroom "
                                "nemhando yezvinhu zvaunosarudza.\n\n"
                                "Unoda here kuti tiuye tiite free assessment tikupe mutengo wakajika?"
                            ),
                            "sn_v": (
                                "Package yebathroom yatakaiswa pa Facebook inotangira kuUS$600. 📢\n\n"
                                "Plumber wedu achakupa mutengo wakajika paauya."
                            ),
                        },

                        "location_ask": {
                            "en": "We are based in Hatfield, Harare, and yourself 📍\n\n",
                            "sn": "Tiri muHatfield, Harare. 📍\n\n",
                        },

                        "location_visit": {
                            "en": (
                                "We work by appointment rather than walk-ins. 📍 We're in Hatfield, Harare.\n\n"
                                "Would you like us to come to you instead? We can do a free on-site assessment "
                                "at your place — saves you the trip and gets you a fixed price on the spot."
                            ),
                            "sn": (
                                "Tinoshandisa ne appointment, hatisi kushanda ne walk-ins. 📍 Tiri muHatfield, Harare.\n\n"
                                "Unoda here kuti tiuye kwauri? Tinogona kuita free assessment paimba yako — "
                                "kukuponesa rwendo uye tikupe mutengo wakakwana pasite."
                            ),
                        },

                        "previous_quotation": {
                            "en": (
                                f"For your previous quotation, please reach out to our plumber directly "
                                f"and they'll pull it up for you right away. 📄\n\n"
                                f"Contact: {plumber_number}"
                            ),
                            "sn": (
                                f"Kuti uwane quotation yako yekare, taura neplumber yedu directly "
                                f"uye vachakubatsira nekukurumidza. 📄\n\n"
                                f"Bata: {plumber_number}"
                            ),
                        },

                        #
                        "combined_pricing": {
                            "en": (
                                "Hi! Just a quick note — these prices are rough prices for supply and install "
                                "(materials included). After the plumber sees the site, the final cost may go up or down. "
                                "Bundling services can give you a discount. Here's a breakdown:\n\n"
                                "• Geyser: Supply from US$80, Install from US$80\n"
                                "• Shower cubicle: Supply from US$130, Install from US$40\n"
                                "• Vanity unit: Supply from US$150, Install from US$30\n"
                                "• Tub: Supply from US$80, Install from US$80\n"
                                "• Free-standing tub mixer: Supply from US$150, Install from US$120\n"
                                "• Side chamber: Supply from US$130, Install from US$30\n"
                                "• Toilet seat: Supply from US$50, Install from US$20\n\n"
                                "Final price depends on your setup — once our plumber sees the space "
                                "they'll give you a fixed number on the spot.\n\n"
                                f"{self._get_pricing_followup_prompt('english')}"
                            ),
                            "sn": (
                                "Mhoro! mitengo iyi ndeye rough yesupply neinstall (zvinhu zvakabatanidzwa). "
                                "Plumber aonawo nzvimbo, mutengo wekupedzisira unogona kukwira kana kuderera. "
                                "Kubatanidza masevhisi kunogona kukupai discount. Apa breakdown:\n\n"
                                "• Geyser: Supply kubva US$80, Install kubva US$80\n"
                                "• Shower cubicle: Supply kubva US$130, Install kubva US$40\n"
                                "• Vanity unit: Supply kubva US$150, Install kubva US$30\n"
                                "• Tub: Supply kubva US$80, Install kubva US$80\n"
                                "• Free-standing tub mixer: Supply kubva US$150, Install kubva US$120\n"
                                "• Side chamber: Supply kubva US$130, Install kubva US$30\n"
                                "• Toilet seat: Supply kubva US$50, Install kubva US$20\n\n"
                                "Mutengo wakakwana unoenderana nesetup yako — plumber wedu aona nzvimbo yako ozokuudza mutengo wakajika.\n\n"
                                f"{self._get_pricing_followup_prompt('shona')}"
                            ),
                        },

                    }

                    responses = pricing_info.get(intent, {})

                    # Select language key — prefer visit-committed variant when applicable
                    if language == 'shona':
                        if visit_committed:
                            reply = responses.get('sn_v') or responses.get('sn', '')
                        else:
                            reply = responses.get('sn', '')
                    else:
                        if visit_committed:
                            reply = responses.get('en_v') or responses.get('en', '')
                        else:
                            reply = responses.get('en', '')

                    # Fallback to DeepSeek if no response found
                    if not reply:
                        reply = self.generate_contextual_response(message, self.get_next_question_to_ask(), [])

                    return reply

                except Exception as e:
                    print(f"❌ Error handling service inquiry: {str(e)}")
                    return self.generate_contextual_response(message, self.get_next_question_to_ask(), [])


        def _generate_pricing_overview_legacy(self, message):
            """Send approximate prices when customer asks about cost"""
            # Try to detect specific service first
            inquiry = self.detect_service_inquiry(message)
        
            if inquiry.get('intent') != 'none' and inquiry.get('confidence') == 'HIGH':
                return self.handle_service_inquiry(inquiry['intent'], message)

            try:
                lang_response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "Detect the language of this message. Reply with ONLY 'shona', 'english', or 'mixed'."
                        },
                        {
                            "role": "user",
                            "content": message
                        }
                    ],
                    temperature=0.1,
                    max_tokens=5
                )
                language = lang_response.choices[0].message.content.strip().lower()
            except Exception:
                language = "english"

            if language == "shona":
                return (
                    "Tub netoilet zviri paFacebook picture zvinenge zviri around US$500, "
                    "uye labour ingangoita US$150 👍\n\n"
                    "Final price inoenderana nesetup, saka tinozoconfirm kana tauya tangoona space.\n\n"
                    f"{self._get_pricing_followup_prompt('shona')}"
                )

            return (
                "The tub & toilet on the Facebook picture are around US$500, "
                "and labour is about US$150 👍\n\n"
                "Final price depends on the setup, so we confirm after a quick site check.\n\n"
                f"{self._get_pricing_followup_prompt('english')}"
            )


        def generate_pricing_overview(self, message):
            """
            Send pricing overview for vague questions like 'how much', 'I want a quotation',
            'how much zvese zvakadai', or any reference to the Facebook offer.
            Anchors on the Facebook bathroom package, then shows individual item prices,
            then pushes to booking.
            """
            # Try to detect a specific service first — if HIGH confidence, hand off
            inquiry = self.detect_service_inquiry(message)
            if inquiry.get('intent') not in ('none', 'combined_pricing') and inquiry.get('confidence') == 'HIGH':
                return self.handle_service_inquiry(inquiry['intent'], message)

            # If the message is vague ("how much", "price?") check what was just
            # discussed in the last few turns and price that item instead.
            _ITEM_CONTEXT = {
                'vanity':        'vanity',
                'geyser':        'geyser',
                'shower':        'shower_cubicle',
                'cubicle':       'shower_cubicle',
                'tub':           'tub_sales',
                'bathtub':       'tub_sales',
                'toilet':        'toilet',
                'chamber':       'chamber',
                'drain':         'drain_unblocking',
                'pipe':          'pipe_repair',
                'facebook':      'facebook_package',
                'package':       'facebook_package',
            }
            recent = self.appointment.conversation_history or []
            recent_text = ' '.join(
                m.get('content', '') for m in recent[-6:]
                if m.get('role') == 'user'
            ).lower()
            for keyword, intent in _ITEM_CONTEXT.items():
                if keyword in recent_text:
                    return self.handle_service_inquiry(intent, message)

            # Detect language
            try:
                lang_response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": "Detect the language of this message. Reply with ONLY 'shona', 'english', or 'mixed'."
                        },
                        {"role": "user", "content": message}
                    ],
                    temperature=0.1,
                    max_tokens=5
                )
                language = lang_response.choices[0].message.content.strip().lower()
            except Exception:
                language = "english"

            # Has the customer committed to a site visit or given their area?
            visit_committed = (
                self.appointment.has_plan is False or
                bool(self.appointment.customer_area)
            )

            # Build context line based on what we know about the customer's project
            project_context = ""
            if self.appointment.project_description:
                desc_lower = self.appointment.project_description.lower()
                if any(w in desc_lower for w in ('tiled', 'already tiled', 'tile', 'existing')):
                    project_context = (
                        "Since your bathroom is already tiled, the renovation cost "
                        "focuses on fixtures and fittings rather than tiling. "
                    )
                elif any(w in desc_lower for w in ('new', 'from scratch', 'building')):
                    project_context = (
                        "For a new bathroom build, pricing covers both rough plumbing and fixtures. "
                    )

            followup = self._get_pricing_followup_prompt(language)

            #
            if language == 'shona':
                reply = (
                    f"{project_context}"
                    "Facebook package yedu inosvika US$600. Ine freestanding tub ne side chamber.\n\n"
                    "Kana muri kuda tub chete — freestanding tubs dzinotangira paUS$400, "
                    "standard tubs kubva US$80.\n\n"
                    "Munoda chii chaizvo?"
                )
            else:
                reply = (
                    f"{project_context}"
                    "Our Facebook package is US$600. That's a freestanding tub and side chamber.\n\n"
                    "If you're looking at just a tub — freestanding tubs start from US$400, "
                    "standard tubs from US$80.\n\n"
                    "What did you have in mind?"
                )
            return reply


        def _generate_contextual_response_legacy(self, incoming_message, next_question, updated_fields):
            """
            Generate the next bot message.
            retry_count == 0  → exact hardcoded wording.
            retry_count > 0   → AI rephrases with escalation psychology.
            """
            try:
                import pytz as _pytz
    
                retry_count = self._get_question_retry_count(next_question)
                sa_tz       = _pytz.timezone('Africa/Johannesburg')
    
                # ── Saturday guard ────────────────────────────────────────────────────
                saturday_indicators = ['saturday', 'sat']
                if any(s in incoming_message.lower() for s in saturday_indicators):
                    alternatives = self.get_alternative_time_suggestions(
                        timezone.now() + timedelta(days=1)
                    )
                    alt_text = "\n".join([f"• {alt['display']}" for alt in alternatives]) if alternatives else ""
                    reply = "We unfortunately don't operate on Saturdays. 😊\n\nOur working hours are Sunday to Friday, 8:00 AM – 6:00 PM.\n\n"
                    if alt_text:
                        reply += f"Here are some available slots:\n{alt_text}\n\nOr feel free to suggest a different date and time!"
                    else:
                        reply += "Could you please choose a different day that works for you?"
                    return reply
    
                # ── "Available all day" guard ─────────────────────────────────────────
                all_day_phrases = [
                    'available all day', 'whole day', 'all day', 'anytime',
                    'any time', 'free all day', 'i am free', 'im free',
                ]
                if (next_question in ('availability_time', 'area', 'complete') and
                        self.appointment.scheduled_datetime and
                        any(p in incoming_message.lower() for p in all_day_phrases)):
                    return self._handle_all_day_response()
                #
                if next_question == "name":
                    # Name was just saved in this turn — ask for email (or confirm)
                    if self.appointment.customer_name and 'customer_name' in (updated_fields or []):
                        return self._confirm_or_request_email()
                    # Name not yet provided — ask for it
                    return (
                        "One last thing — what name should we put on the booking? "
                        "If you'd rather not share it, just say no."
                    )

                # ── First-pass: exact hardcoded questions (retry_count == 0) ─────────

                if retry_count == 0:

                    if next_question == "service_type":
                        return (
                            "Hello,\nHow may we assist you on plumbing services"
                        )

                    if next_question == "project_description":
                        return f"Got it! {self._get_contextual_description_question()}"

                    if next_question == "availability_date":
                        days       = self._get_next_two_available_days()
                        day_a      = self._format_day(days[0]) if len(days) > 0 else "tomorrow"
                        day_b      = self._format_day(days[1]) if len(days) > 1 else "the day after"
                        visit_desc = self._describe_project_context()
                        return (
                            f"Great, what works better for you — {day_a} or {day_b} — "
                            f"for us to come through and {visit_desc}?"
                        )

                    if next_question == "availability_time":
                        dt = self.appointment.scheduled_datetime
                        if dt:
                            selected_date = self._get_selected_local_date()
                            day_label = self._format_day(selected_date) if selected_date else "that day"
                            times     = self._get_two_available_times_for_date(selected_date) if selected_date else []
                            time_a    = times[0].strftime('%I%p').lstrip('0') if len(times) > 0 else "9AM"
                            time_b    = times[1].strftime('%I%p').lstrip('0') if len(times) > 1 else "2PM"
                            return (
                                f"Perfect, for {day_label} — "
                                f"what works better: {time_a} or {time_b}?"
                            )
                        return "What time works best for you — morning or afternoon?"

                    if next_question == "area":
                        return "All good, what area are you in?"

                    #
                    if next_question == "name":
                        # If name was just captured this turn, ask for email (or confirm)
                        if self.appointment.customer_name and 'customer_name' in (updated_fields or []):
                            return self._confirm_or_request_email()
                        # Name declined this turn
                        if self._declines_sharing_name(incoming_message):
                            self._mark_customer_name_declined()
                            return (
                                "No problem at all. Your appointment is still confirmed — "
                                "we'll use this WhatsApp number for updates."
                            )
                        # Still waiting for name
                        return (
                            "One last thing — what name should we put on the booking? "
                            "If you'd rather not share it, just say no."
                        )
                # ── AI-driven retries ─────────────────────────────────────────────────
                appointment_context = self.get_appointment_context()
                retry_context_line = self._build_retry_context_line(updated_fields, next_question)
    
                system_prompt = f"""You are a member of the Homebase Plumbers team in Harare. You help customers book a free site visit over WhatsApp.

        Text like a real, warm person — short messages, natural, Zimbabwean English. Never robotic, never corporate.

        NEVER say: "I understand", "I apologize", "certainly", "more efficiently", "your plumbing needs", "as an AI"
        NEVER use bullet points in a chat message.
        NEVER stack two questions in one message.
        NEVER use contractions — write "we will" not "we'll", "they will" not "they'll".
        Emojis only when they fit naturally — not forced at the end of every message.
        Use "we" not "I" or "our" — you represent the whole team.
        The plumber's name is Takudzwa.

        CURRENT FLOW:
        1. service_type          ✅ or pending
        2. project_description   ✅ or pending
        3. area                  ✅ or pending
        4. availability_date     ✅ or pending
        5. availability_time     ✅ or pending

        CURRENT SITUATION:
        {appointment_context}

        Next question needed: {next_question}
        New info just received: {updated_fields if updated_fields else 'None'}
        Relevant line to weave in if helpful: {retry_context_line or 'None'}
        Retry count: {retry_count}

        INTELLIGENCE RULE:
        - Partial answers like "Tues", "Thurs", "12" are valid — confirm naturally: "Got it, this coming Tuesday?"
        - Never assume no match means they're wrong.

        RETRY ESCALATION (retry_count > 0):
        Retry 1 → Simplify to the bare minimum.
        Retry 2 → Offer two explicit choices.
        Retry 3 → Light urgency: "We're getting booked up this week."

        QUESTION MAPPINGS (rephrase — never word-for-word):
        - service_type        → which of our three services they need
        - project_description → what specifically needs doing, more detail = better quote
        - area                → which suburb/area they're in
        - availability_date   → which of two upcoming weekday dates works for the site visit
        - availability_time   → morning or afternoon on that date

        RULES:
        - ONE question at a time.
        - If new info received, acknowledge it briefly and naturally before the next question.
        - Match their tone and energy.
        - Short sentences.
        - NEVER ask for info already collected.

        Generate the response now:"""
    
                from bot.services.clients import deepseek_call
                reply = deepseek_call(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": f"Customer message: '{incoming_message}'"},
                    ],
                    temperature=0.7,
                    max_tokens=250,
                )
    
                # Reset / increment retry counter
                if updated_fields:
                    self._set_question_retry_count(next_question, 0)
                else:
                    self._set_question_retry_count(next_question, retry_count + 1)
                self._sync_retry_count_field(next_question)

                return reply
    
            except Exception as e:
                print(f"❌ Error generating contextual response: {str(e)}")
                return "Sorry, dropped that on our end — could you send that again?"


        def _is_standalone_question(self, message: str) -> bool:
            msg = (message or "").strip()
            if not msg or len(msg) < 3:
                return False

            # Fast-exit: all booking info already collected
            if (
                self.appointment.project_type and
                self.appointment.customer_area and
                self.appointment.scheduled_datetime and
                self._time_confirmed()
            ):
                return False

            if not deepseek_client:
                return False

            try:
                history = self.appointment.conversation_history or []
                last_bot = ""
                for msg_obj in reversed(history[:-1]):
                    if msg_obj.get("role") == "assistant":
                        content = (msg_obj.get("content") or "").strip()
                        if content and not content.startswith("["):
                            last_bot = content[:300]
                            break

                booking_state = []
                if not self.appointment.project_type:
                    booking_state.append("service type: not collected")
                if not self.appointment.project_description:
                    booking_state.append("project description: not collected")
                if not self.appointment.scheduled_datetime:
                    booking_state.append("appointment date: not collected")
                elif not self._time_confirmed():
                    booking_state.append("appointment time: not confirmed")
                if not self.appointment.customer_area:
                    booking_state.append("area: not collected")
                booking_state_str = ", ".join(booking_state) or "all booking details collected"

                prompt = f"""You are an intent classifier for a Zimbabwean plumbing company's WhatsApp chatbot.

        BOOKING STATE: {booking_state_str}

        BOT'S LAST MESSAGE:
        "{last_bot}"

        CUSTOMER'S REPLY:
        "{message}"

        Classify as GENUINE_QUESTION if the customer is:
        - Asking whether a specific service can be done
        - Asking about pricing, costs, or what's included
        - Asking about the company, its process, or how something works
        - Asking about materials, brands, or product availability
        - Asking anything that is clearly NOT answering what the bot just asked

        Classify as BOOKING_ANSWER if the customer is:
        - Providing a date, day name, time, or availability
        - Providing their area, suburb, or location
        - Saying yes/no/ok/sure to the bot's question
        - Describing their project in response to being asked
        - Giving their name

        Reply with ONLY valid JSON:
        {{"classification": "GENUINE_QUESTION" or "BOOKING_ANSWER", "confidence": "HIGH" or "LOW"}}"""

                from bot.services.clients import deepseek_call
                raw = deepseek_call(
                    messages=[
                        {"role": "system", "content": "Return ONLY valid JSON. No markdown."},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.1,
                    max_tokens=30,
                    json_response=True,
                )
                raw = raw.replace("```json", "").replace("```", "").strip()
                result = json.loads(raw)
                classification = result.get("classification", "BOOKING_ANSWER")
                confidence     = result.get("confidence", "LOW")
                is_question = (classification == "GENUINE_QUESTION" and confidence == "HIGH")
                print(f"🤖 Standalone Q check: '{message[:60]}' → {classification} ({confidence})")
                return is_question

            except Exception as exc:
                print(f"⚠️ Standalone question check failed: {exc}")
                return False


        def _answer_standalone_question(self, message: str) -> str:
            if not deepseek_client:
                return None

            # ── GREETING / GENERIC OPENER — short-circuit before any DeepSeek call
            if self.get_next_question_to_ask() == "service_type":
                return "Hello,\nHow may we assist you on plumbing services"


            try:
                service     = (self.appointment.project_type or "").replace("_", " ").lower()
                area        = self.appointment.customer_area or ""
                description = self.appointment.project_description or ""

                history = self.appointment.conversation_history or []
                skip = ("[AUTO", "[MANUAL", "[BULK", "[24HR", "[Sent ", "[FILE", "[VIDEO")
                recent_lines = []
                for msg_obj in history[-8:]:
                    content = (msg_obj.get("content") or "").strip()
                    if not content or any(content.startswith(p) for p in skip):
                        continue
                    role = "Customer" if msg_obj.get("role") == "user" else "Bot"
                    recent_lines.append(f"{role}: {content[:200]}")
                context_block = "\n".join(recent_lines) if recent_lines else "No prior conversation."

                prompt = f"""You are a knowledgeable WhatsApp assistant for Homebase Plumbers — a professional plumbing and renovation company based in Harare, Zimbabwe.

        CRITICAL RULE — GENERIC OPENERS:
        If the customer's message is a generic greeting, a vague request for more information, or an opening message with no specific question, you MUST reply with ONLY this exact text and nothing else:
        Hello,\n
        How may we assist you on plumbing services

        This applies to ALL of the following (and any equivalent):
        - Greetings: hi, hello, hey, hie, good morning, good afternoon, good evening, sawubona, mhoro, makadii, masikati, mangwanani, howzit, sharp, eita
        - Vague info requests: "more information", "more info", "tell me more", "how can you help", "what do you do", "I need help", "can you help me", "I saw your ad", "I'm interested"
        - Any combo of the above: "hello, I need more info", "hi, can I get more information on this", "good morning, tell me about your services"
        - Any language variant (Shona, Ndebele, informal Zim English) that is a greeting or vague opener with no specific question

        SERVICES WE OFFER:
        - Bathroom renovation: toilet, shower cubicle, bathtub, vanity unit, basin/sink, geyser, side chamber, tiling, pipe work
        - Kitchen renovation: kitchen sink, taps/mixers, dishwasher connections, pipe work
        - New plumbing installation: new builds, extensions, full house piping, borehole connections, JoJo tank setups
        - General plumbing: leak repairs, pipe repairs, drain unblocking, pressure pump installation
        - We CAN install sinks, taps, or water points in garages, outbuildings, and workshops
        - We supply AND install all fixtures (or install customer-supplied fixtures)

        PRICING GUIDE (rough supply + install):
        - Toilet: supply from US$50, install from US$20
        - Shower cubicle (900x900mm): supply from US$130, install from US$40
        - Vanity unit: supply from US$150, install from US$30
        - Geyser: supply from US$80, install from US$80
        - Bathtub (ordinary): supply from US$80, install from US$80
        - Freestanding tub: supply from US$400, mixer from US$150, install US$120
        - Side chamber: supply from US$130, install from US$30
        - Full bathroom package: from US$600+
        - Site assessment / visit: FREE

        COMPANY INFO:
        - Based in Hatfield, Harare
        - Works by appointment (not walk-ins)
        - Monday–Sunday except Saturday (closed Saturdays)
        - Business hours: 8 AM – 6 PM
        - Site assessment is free, plumber gives fixed quote on the spot
        - The plumber's name is Takudzwa
        - Plumber direct contact: {self.appointment.plumber_contact_number or "+263774819901"}

        CUSTOMER CONTEXT:
        - Service interest: {service or "not yet specified"}
        - Area: {area or "not yet specified"}
        - Project description: {description or "not yet provided"}

        RECENT CONVERSATION:
        {context_block}

        CUSTOMER'S QUESTION: "{message}"

        If this is NOT a generic opener, answer directly and honestly.
        - If we can do it: confirm clearly, briefly explain what's involved.
        - If we cannot (electrical, roofing, painting): say so and redirect to what we can help with.
        - If it's a pricing question: give the relevant range from the guide above.
        - Zimbabwean English. No bold, no bullets. Do NOT end with a question."""

                response = deepseek_client.chat.completions.create(
                    model=settings.DEEPSEEK_MODEL,
                    messages=[
                        {
                            "role": "system",
                            "content": (
                                "You are a WhatsApp assistant for a plumbing company. "
                                "IMPORTANT: If the customer message is a generic greeting or vague opener "
                                "with no specific question, reply with ONLY this exact text: "
                                "'Hello,\\nHow may we assist you on plumbing services' — nothing else. "
                                "For real questions: direct, helpful, human. "
                                "No bullet points. No markdown. Do not end with a question."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    temperature=0.5,
                    max_tokens=100,
                )
                answer = response.choices[0].message.content.strip().replace("**", "").replace("__", "")
                print(f"🤖 Dynamic answer for: '{message[:60]}'")
                return answer

            except Exception as exc:
                print(f"⚠️ Dynamic answer generation failed: {exc}")
                return None


        def _post_booking_contextual_reply(self, message: str) -> str:
            """
            Called when the appointment is confirmed+complete and the customer
            sends any follow-up message.  Uses DeepSeek to reply contextually —
            acknowledges what they said and reminds them of their booking.
            Falls back to a safe static reply if DeepSeek fails.
            """
            import pytz as _pytz
            from bot.services.clients import deepseek_call

            sa_tz = _pytz.timezone('Africa/Johannesburg')
            dt = self.appointment.scheduled_datetime
            appt_str = (
                dt.astimezone(sa_tz).strftime('%A, %B %d at %I:%M %p')
                if dt else "your booked time"
            )
            name_part = f", {self.appointment.customer_name}" if self.appointment.customer_name else ""

            system_prompt = (
                f"You are a WhatsApp assistant for Homebase Plumbers in Harare, Zimbabwe. "
                f"The plumber's name is Takudzwa. "
                f"The customer's appointment is CONFIRMED for {appt_str}. "
                f"They have just sent a follow-up message. "
                f"Reply warmly in 1-3 sentences. Acknowledge what they said. "
                f"If they ask who will come or who they are speaking to, say the plumber's name is Takudzwa. "
                f"If it is extra context about their job, acknowledge it positively. "
                f"If it mentions a time or date, confirm it matches their booking at {appt_str}. "
                f"Close with a friendly line such as 'See you on {appt_str}{name_part}! 😊'. "
                f"Never ask for information that has already been booked. "
                f"NEVER use the word 'our' — say 'we', 'the team', or 'us' instead. "
                f"NEVER use contractions — write 'we will' not 'we'll', 'they will' not 'they'll'. "
                f"Write like a friendly human texting — short, warm, no bullet points."
            )

            try:
                raw = deepseek_call(
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user",   "content": message},
                    ],
                    temperature=0.4,
                    max_tokens=120,
                )
                reply = (raw or "").strip()
                if reply:
                    return reply
            except Exception as exc:
                print(f"⚠️ Post-booking contextual reply failed: {exc}")

            # Safe fallback — never return None
            return f"Thanks for that{name_part}! We'll see you on {appt_str}. 😊"


        def _get_soft_booking_nudge(self) -> str:
            """
            Return a single soft one-line booking nudge to append after answering
            a standalone question. Picks the right nudge for whatever is still missing.
            """
            next_q = self.get_next_question_to_ask()
 
            if next_q == "availability_date":
                days = self._get_next_two_available_days()
                if len(days) >= 2:
                    day_a = self._format_day(days[0])
                    day_b = self._format_day(days[1])
                    return f"Would {day_a} or {day_b} work for a free site visit?"
                return "Would you like to book a free site visit?"
 
            if next_q == "availability_time":
                return "What time suits you best for the visit?"
 
            if next_q == "area":
                return "Which area are you in?"
 
            if next_q == "project_description":
                return "Could you tell me a bit more about what you need done?"
 
            if next_q == "service_type":
                return ""

            return ""


        def get_ai_performance_stats(self):
            """Get statistics on AI reschedule detection performance"""
            try:
                # This would query your log database if implemented
                # For now, just return placeholder stats
                return {
                    'total_reschedule_requests': 0,
                    'ai_detected_correctly': 0,
                    'ai_missed': 0,
                    'false_positives': 0,
                    'accuracy_rate': 0.0
                }
            except Exception as e:
                print(f"Error getting AI stats: {str(e)}")
                return None

