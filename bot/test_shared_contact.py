"""
A lead sends a contact card (bot/shared_contact.py, owner 2026-09-22).

Card + "contact them": the plumber is emailed a call script at once and the
lead hears "Thanks, we'll give Tendai a call." Card alone: we ask. Offline: the
DeepSeek stub is installed and all HTTP is blocked; the email send is mocked.
"""

from unittest.mock import patch

from django.test import TestCase

from bot import shared_contact as sc
from bot.models import Appointment

CARD = [{'name': {'formatted_name': 'Tendai Moyo', 'first_name': 'Tendai'},
         'phones': [{'phone': '+263 77 123 4567', 'wa_id': '263771234567', 'type': 'CELL'}]}]


class SharedContactTests(TestCase):

    def setUp(self):
        from tests.deepseek_mock import install
        install()
        for target, kwargs in (
                ('requests.sessions.Session.send', {'side_effect': AssertionError('live HTTP')}),
                ('httpx.Client.send', {'side_effect': AssertionError('live HTTP')})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        mail = patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
        self.mail = mail.start()
        self.addCleanup(mail.stop)
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+15550040001', status='pending', customer_name='Rudo',
            project_description='Two bathrooms, new tubs and showers', customer_area='Borrowdale')

    def _turn(self, text):
        return sc.handle_turn(self.lead, text)

    def test_the_card_is_read(self):
        self.assertEqual(sc.card_lines(CARD), [('Tendai Moyo', '+263 77 123 4567')])
        self.assertEqual(sc.marker_text(CARD), '[Sent contact] Tendai Moyo +263 77 123 4567')

    def test_card_and_contact_them_emails_the_plumber_at_once(self):
        reply = self._turn('Please call my husband\n' + sc.marker_text(CARD))
        self.assertEqual(reply, "Thanks, we'll give Tendai a call.")
        self.mail.assert_called_once()
        subject = self.mail.call_args.args[1]
        html = self.mail.call_args.kwargs['html_message']
        self.assertTrue(subject.startswith('Please call today: Tendai Moyo'), subject)
        # Script first, with what we know worked into it; visit first.
        self.assertLess(html.index('Script'), html.index('Lead details'))
        self.assertIn('Rudo passed on your number', html)
        self.assertIn('Two bathrooms, new tubs and showers in Borrowdale', html)
        self.assertIn('suit you better for us to come and have a quick look', html)
        self.assertIn('Log the call', html)
        self.assertIn('Please call my husband', html)

    def test_a_card_alone_gets_a_question(self):
        reply = self._turn(sc.marker_text(CARD))
        self.assertEqual(reply, "Thanks for Tendai's number. Should we give them a call about the job?")
        self.mail.assert_not_called()

    def test_yes_to_the_question_emails_the_plumber(self):
        self._turn(sc.marker_text(CARD))
        self.assertEqual(self._turn('Yes please'), "Thanks, we'll give Tendai a call.")
        self.mail.assert_called_once()

    def test_no_keeps_the_number_and_sends_nothing(self):
        self._turn(sc.marker_text(CARD))
        self.assertEqual(self._turn('No'), "No problem, we've kept Tendai's number on file.")
        self.mail.assert_not_called()

    def test_anything_else_after_the_question_carries_on(self):
        """The customer's own words win: a question about price is not a yes."""
        self._turn(sc.marker_text(CARD))
        self.assertIsNone(self._turn('How much is a tub?'))
        self.assertIsNone(self._turn('Yes'))            # the question is closed now
        self.mail.assert_not_called()

    def test_contact_them_after_the_card_turn_still_briefs_once(self):
        self._turn(sc.marker_text(CARD))
        self._turn('How much is a tub?')                 # the question lapses
        self.assertEqual(self._turn('Please contact him'), "Thanks, we'll give Tendai a call.")
        self.assertIsNone(self._turn('Please contact him'))   # briefed once
        self.mail.assert_called_once()

    def test_instructions_are_recognised(self):
        for text in ('Call him', 'please contact them', 'Speak to my wife',
                     'phone this number', 'he will explain', 'Mufonerei'):
            with self.subTest(text=text):
                self.assertTrue(sc.asks_us_to_contact(text))
        for text in ('Thanks', 'How much?', 'Borrowdale'):
            with self.subTest(text=text):
                self.assertFalse(sc.asks_us_to_contact(text))

    def test_the_brief_is_reminded_like_any_call_email(self):
        """Subject and layout match what the 24-hour reminder rewrites."""
        from bot.call_brief import CALL_SUBJECT_PREFIX, _DAY_PAIR
        self._turn('Call him\n' + sc.marker_text(CARD))
        subject = self.mail.call_args.args[1]
        html = self.mail.call_args.kwargs['html_message']
        self.assertTrue(subject.startswith(CALL_SUBJECT_PREFIX))
        self.assertTrue(_DAY_PAIR.search(html))
