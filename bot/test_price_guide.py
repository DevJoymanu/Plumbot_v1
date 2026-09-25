"""
The price-guide flow through the real inbound pipeline (owner, 2026-09-21).

A lead who has given the three fields asks a GENERAL price question and gets
the price guide PDF and "a quick look at the space, or a quote online first?";
the answer routes to the plumber handoff or the booking question. Replayed
through `bot.scenario_runner.send_message` (the webhook, offline DeepSeek stub)
with the three fields SEEDED on the lead: the stub's extractor does not set a
service type, so a scenario file alone cannot reach the gate offline
(scenarios/price_guide_after_three_fields.txt covers the live run).

TransactionTestCase via OfflineScenarioSuite: the reply threads write from
their own connections (see that class). Its whole-suite test is switched off
here; the scenario gate runs it once.
"""

from unittest.mock import patch

# Imported as a module, not by class name: a class imported into this
# module's namespace is collected and run again here, whole replay and all.
from bot import test_scenarios as _scenarios


class PriceGuideTests(_scenarios.OfflineScenarioSuite):

    def test_scenarios_do_not_regress(self):
        """The full replay belongs to bot.test_scenarios, not this module."""

    def _lead(self, sender, **fields):
        from bot.models import Appointment, Tenant
        from bot.scenario_runner import reset_lead
        tenant = Tenant.objects.filter(slug='homebase').first()
        reset_lead(sender, tenant=tenant)
        from django.utils import timezone
        now = timezone.now().isoformat()
        # A conversation already under way: with no history the first-contact
        # opener answers before routing, which is not the case under test.
        history = [
            {'role': 'user', 'content': 'Hello! Can I get more info on this?', 'timestamp': now},
            {'role': 'assistant', 'content': 'All good, what area are you in?',
             'timestamp': now, 'sent_at': now},
            {'role': 'user', 'content': 'Borrowdale', 'timestamp': now},
            {'role': 'assistant', 'content': 'Great, would tomorrow at 9am work for us to come through?',
             'timestamp': now, 'sent_at': now},
        ]
        defaults = dict(project_type='bathroom_renovation',
                        project_description='whole bathroom, new tub and tiles',
                        customer_area='Borrowdale', status='pending',
                        conversation_history=history)
        defaults.update(fields)
        Appointment.objects.update_or_create(
            phone_number=f'whatsapp:+{sender}', tenant=tenant, defaults=defaults)
        return tenant

    def _send(self, sender, text, tenant):
        from bot.scenario_runner import send_message
        return ' / '.join(send_message(sender, text, tenant=tenant, media_wait=0))

    def _mark_pdf_sent(self, appointment, force=False, caption=None):
        """Stands in for the PDF send: records it the way the real one does,
        and keeps the caption so a test can check what the PDF carried."""
        self.pdf_caption = caption
        appointment.internal_notes = f"{appointment.internal_notes or ''}\n[LEAD_MAGNET_WA_SENT]"
        appointment.save(update_fields=['internal_notes'])
        return True

    def test_general_price_ask_gets_the_guide_then_online_gets_the_handoff(self):
        sender = '999000070001'
        tenant = self._lead(sender)
        with patch('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp',
                   side_effect=self._mark_pdf_sent) as pdf:
            first = self._send(sender, 'I need prices first', tenant)
        pdf.assert_called_once()
        self.assertIn('quick look at the space, or a quote online first', first)
        # The job they described ("whole bathroom, new tub and tiles") is
        # priced first (owner, 2026-09-22; a general ask was PDF only before).
        self.assertIn('US$', first)
        # Three messages: prices, PDF, question. No guide line between the
        # prices and the PDF (owner, 2026-09-25).
        from bot import copy_catalog as cc
        self.assertNotIn(cc.PRICE_GUIDE_INTRO, first)
        self.assertEqual(len(first.split(' / ')), 2, first)   # + the PDF
        # ...the guide line rides on the PDF as its caption.
        self.assertEqual(self.pdf_caption, cc.PRICE_GUIDE_INTRO)

        second = self._send(sender, 'online please', tenant)
        self.assertIn('https://wa.me/', second)
        self.assertIn('handles our quotes', second)

    def test_a_shona_price_ask_gets_the_shona_guide_and_shona_handoff(self):
        """A Shona lead is offered the online door too, in Shona. The sequence
        was English only, so a Shona lead got a price block and no choice."""
        from bot import copy_catalog as cc
        sender = '999000070006'
        tenant = self._lead(sender)
        with patch('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp',
                   side_effect=self._mark_pdf_sent) as pdf:
            first = self._send(sender, 'Mutengo wacho marii?', tenant)
        pdf.assert_called_once()
        # Prices, PDF, question: no guide line after a price (owner, 2026-09-25).
        self.assertNotIn(cc.PRICE_GUIDE_INTRO_SN, first)
        self.assertEqual(self.pdf_caption, cc.PRICE_GUIDE_INTRO_SN)
        self.assertTrue(first.rstrip().endswith(cc.PRICE_CHOICE_ASK_SN), first)
        self.assertNotIn('quote online first', first)

        second = self._send(sender, 'pa online', tenant)
        self.assertIn(cc.PORTFOLIO_HANDOFF_FREE_FIRST_SN, second)
        self.assertIn('https://wa.me/', second)

    def test_a_visit_answer_gets_the_booking_question(self):
        sender = '999000070002'
        tenant = self._lead(sender)
        with patch('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp',
                   side_effect=self._mark_pdf_sent):
            self._send(sender, 'how much will it cost?', tenant)
        reply = self._send(sender, 'A quick look is fine', tenant)
        self.assertNotIn('wa.me', reply)
        # The booking question, in whichever shape the chain gives it: two
        # slots, or on the first visit ask the tenant's visit-price close
        # (ensure_visit_price_note), "...Want me to book you a time?".
        self.assertRegex(reply.lower(),
                         r'what works better|work for us|\d(am|pm)|book you a time')

    def test_a_bare_yes_is_the_visit_not_the_question_again(self):
        """Barmak lead 1236 (2026-09-25) answered the choice with "Yes" and got
        "Do you want a quick look at the space, or a quote online first?"
        back: the controller drove book_visit ahead of STEP 0-b and the reply
        check rewrote the close into the question. A bare yes is the visit
        (owner, 2026-09-25), and the open tag is cleared."""
        from bot.models import Appointment
        from bot.price_guide import CHOICE_TAG
        sender = '999000070007'
        tenant = self._lead(sender)
        with patch('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp',
                   side_effect=self._mark_pdf_sent):
            self._send(sender, 'how much will it cost?', tenant)
        reply = self._send(sender, 'Yes', tenant)
        self.assertNotIn('quote online first', reply)
        self.assertNotIn('wa.me', reply)
        self.assertRegex(reply.lower(),
                         r'what works better|work for us|\d(am|pm)|book you a time')
        appt = Appointment.objects.get(phone_number=f'whatsapp:+{sender}', tenant=tenant)
        self.assertNotIn(CHOICE_TAG, appt.internal_notes or '')

    def test_the_controller_stands_aside_while_the_choice_is_open(self):
        """decide_move leaves the turn to STEP 0-b while the choice is open,
        as it does for the budget ladder's questions."""
        import types
        from bot import controller
        from bot.price_guide import CHOICE_TAG
        appt = types.SimpleNamespace(internal_notes='', status='pending',
                                     conversation_history=[], scheduled_datetime=None)
        with patch.object(controller, 'CONTROLLER_DRIVES_ROUTING', True), \
                patch.object(controller, 'plan_next_move', return_value='book_visit'), \
                patch.object(controller, 'plan_confidence', return_value=1.0), \
                patch.object(controller, 'plan_want_level', return_value='wants_it'), \
                patch.object(controller, 'project_next_move', return_value=None), \
                patch.object(controller, 'apply_fee_gate', side_effect=lambda m, a, w: m), \
                patch.object(controller, 'apply_plan_path_gate', side_effect=lambda m, a: m), \
                patch.object(controller, '_booking_half_made', return_value=False), \
                patch.object(controller, '_answering_the_close', return_value=False):
            self.assertEqual(controller.decide_move({}, appt), 'book_visit')
            appt.internal_notes = CHOICE_TAG
            self.assertIsNone(controller.decide_move({}, appt))

    def test_a_named_item_gets_its_price_then_the_guide_then_the_question(self):
        """Owner, 2026-09-22: every price question from a lead with the three
        fields, a named item included, gets the price, the PDF and the question."""
        sender = '999000070003'
        tenant = self._lead(sender)
        with patch('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp',
                   side_effect=self._mark_pdf_sent) as pdf:
            reply = self._send(sender, 'how much is a shower cubicle?', tenant)
        pdf.assert_called_once()
        self.assertIn('US$', reply)
        self.assertIn('quick look at the space, or a quote online first', reply)
        # The price comes first, the question last, and nothing between them
        # but the PDF (owner, 2026-09-25: three messages).
        from bot import copy_catalog as cc
        self.assertNotIn(cc.PRICE_GUIDE_INTRO, reply)
        self.assertEqual(self.pdf_caption, cc.PRICE_GUIDE_INTRO)
        self.assertLess(reply.index('US$'), reply.index('online first?'))
        self.assertTrue(reply.rstrip().endswith('online first?'), reply)

    def test_a_lead_who_has_the_pdf_gets_the_price_and_the_question(self):
        sender = '999000070005'
        tenant = self._lead(sender, internal_notes='[LEAD_MAGNET_WA_SENT]')
        with patch('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp',
                   side_effect=self._mark_pdf_sent) as pdf:
            reply = self._send(sender, 'how much is a shower cubicle?', tenant)
        pdf.assert_not_called()
        self.assertIn('US$', reply)
        self.assertNotIn("Here's our price guide", reply)
        self.assertIn('quote online first', reply)

    def test_without_the_three_fields_it_is_the_ordinary_price_reply(self):
        sender = '999000070004'
        # No area anywhere, history included: the stub extractor reads an area
        # out of the seeded "Borrowdale" turn and would fill the field.
        from django.utils import timezone
        now = timezone.now().isoformat()
        tenant = self._lead(sender, customer_area='', conversation_history=[
            {'role': 'user', 'content': 'Hello! Can I get more info on this?', 'timestamp': now},
            {'role': 'assistant', 'content': 'What exactly are you looking to get done?',
             'timestamp': now, 'sent_at': now},
            {'role': 'user', 'content': 'whole bathroom, new tub and tiles', 'timestamp': now},
            {'role': 'assistant', 'content': 'Got it. Is it the whole bathroom?',
             'timestamp': now, 'sent_at': now},
        ])
        with patch('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp',
                   side_effect=self._mark_pdf_sent) as pdf:
            reply = self._send(sender, 'I need prices first', tenant)
        pdf.assert_not_called()
        self.assertNotIn('quote online first', reply)
