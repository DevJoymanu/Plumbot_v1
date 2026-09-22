"""
The 24-hour reminder for a "please call this lead" email (bot/call_brief.py).

Owner, 2026-09-22: if the plumber has not logged the call within 24 hours,
send the call email again, opening with "we didn't hear back", the script
repeated and the button again. One reminder only. Built from the stored call
email, so these tests store one the way `send_email_to_recipients` logs it.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from bot import call_brief
from bot.models import Appointment, SentEmail

INTRO = ("Hi Kudakwashe, please call this lead today and read the script below. They were "
         "chatting with our team on WhatsApp, so this is the first time they'll hear from you.")
SCRIPT = ("Hi, it's Kudakwashe from Barmak Plumbing. On Saturday you mentioned you'd let us "
          "know on Monday which day works. Would Wednesday or Thursday suit you better?")
BUTTON = 'https://plumbot.example.com/phone-quote/abc/'
HTML = ('<meta charset="utf-8"><div style="max-width:620px;"><p style="x">%s</p>'
        '<div><p style="y">"%s"</p></div><a href="%s">Log the call</a></div>' % (INTRO, SCRIPT, BUTTON))


def sast(day, hour):
    """A moment in September 2030 in SAST: the 11th is a Wednesday."""
    from bot.management.commands.send_followups import SA_TIMEZONE
    return SA_TIMEZONE.localize(datetime(2030, 9, day, hour, 0))


class CallReminderTests(TestCase):

    def setUp(self):
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+15550099001', status='pending',
            project_type='bathroom_renovation', project_description='tub swap',
            customer_area='Highfield')
        patcher = patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
        self.send = patcher.start()
        self.addCleanup(patcher.stop)

    def _call_email(self, sent_at, subject='Please call today: +263 77 242 4212, Highfield'):
        row = SentEmail.objects.create(
            appointment=self.lead, category='plumber_alert', to_role='plumber',
            recipients=['plumber@example.com'], subject=subject,
            html_body=HTML, text_body=INTRO + '\n\nLog the call: ' + BUTTON,
            status='sent', sent_at=sent_at)
        SentEmail.objects.filter(pk=row.pk).update(created_at=sent_at)
        return row

    def _run(self, now):
        self.send.reset_mock()
        with patch('django.utils.timezone.now', return_value=now):
            return call_brief.send_due_reminders(now=now)

    def test_after_24_hours_the_reminder_goes_with_the_script_and_the_button(self):
        self._call_email(sast(10, 9))                       # Tuesday 09:00
        self._run(sast(11, 10))                             # Wednesday 10:00
        self.send.assert_called_once()
        args, kwargs = self.send.call_args
        to, subject, text = args[:3]
        html = kwargs['html_message']
        self.assertEqual(to, ['plumber@example.com'])
        self.assertEqual(subject, 'Reminder: please call +263 77 242 4212, Highfield')
        self.assertIn("Hi Kudakwashe, we didn't hear back after yesterday's email", html)
        self.assertNotIn('please call this lead today', html)
        # The days move on from the stale pair: Thursday and Friday now.
        self.assertIn('Would Thursday or Friday suit you better?', html)
        self.assertNotIn('Wednesday or Thursday', html)
        self.assertIn(BUTTON, html)
        self.assertIn(BUTTON, text)

    def test_not_before_24_hours(self):
        self._call_email(sast(10, 11))
        self._run(sast(11, 10))
        self.send.assert_not_called()

    def test_only_one_reminder(self):
        self._call_email(sast(10, 9))
        self._run(sast(11, 10))
        self.send.assert_called_once()
        # The send path logs the reminder; that row is the gate.
        logged = SentEmail.objects.create(appointment=self.lead, subject='Reminder: please call x',
                                          status='sent', category='plumber_alert')
        SentEmail.objects.filter(pk=logged.pk).update(created_at=sast(11, 10))
        self._run(sast(11, 10) + timedelta(minutes=5))
        self._run(sast(12, 10))
        self.send.assert_not_called()

    def test_no_reminder_once_the_call_is_logged(self):
        from bot.models import PhoneQuoteRequest
        self._call_email(sast(10, 9))
        PhoneQuoteRequest.objects.create(appointment=self.lead, form_completed_at=sast(10, 15))
        self._run(sast(11, 10))
        self.send.assert_not_called()

    def test_no_reminder_when_the_lead_wrote_in_since(self):
        self._call_email(sast(10, 9))
        self.lead.last_customer_response = sast(10, 18)
        self.lead.save(update_fields=['last_customer_response'])
        self._run(sast(11, 10))
        self.send.assert_not_called()

    def test_no_reminder_once_booked(self):
        self._call_email(sast(10, 9))
        self.lead.status = 'confirmed'
        self.lead.scheduled_datetime = sast(12, 9)
        self.lead.save(update_fields=['status', 'scheduled_datetime'])
        self._run(sast(11, 10))
        self.send.assert_not_called()

    def test_a_stale_call_email_is_dropped(self):
        self._call_email(sast(6, 9))                        # five days back
        self._run(sast(11, 10))
        self.send.assert_not_called()

    def test_not_outside_the_contact_hours(self):
        self._call_email(sast(10, 1))
        self._run(sast(11, 3))
        self.send.assert_not_called()

    def test_two_days_on_it_names_the_day_rather_than_yesterday(self):
        self._call_email(sast(9, 19))                       # Monday evening
        self._run(sast(11, 10))                             # Wednesday
        html = self.send.call_args.kwargs['html_message']
        self.assertIn("didn't hear back after our email on Monday", html)

    def test_other_plumber_emails_never_get_a_reminder(self):
        self._call_email(sast(10, 9), subject='[Site visit] How did the Borrowdale lead go?')
        self._run(sast(11, 10))
        self.send.assert_not_called()
