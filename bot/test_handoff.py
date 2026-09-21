"""
The handoff brief (owner, 2026-09-21), end to end against the database.

Rule 1: a delay to a date more than a week out asks permission for job - 7 as
a literal date, sends the portfolio and the plumber's quote link together, and
arms the job-date ladder (-7 and -3 touches, the plumber's call at -2 if they
have not answered). Rule 2 (the ghosted lead's second follow-up) and the pure
pieces are pinned in TEST 0 ("plumber link" / "job ladder"). All outbound is
mocked; the delay flow's date reader is patched so no model is called.
"""

from datetime import date, datetime, timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from bot import job_date_ladder as ladder
from bot.models import Appointment
from bot.plumber_link import LINK_SENT_TAG

PLUMBER_WA = 'wa.me/263774819901'


def make_lead(suffix, **kwargs):
    defaults = {'phone_number': f'whatsapp:+1555001{suffix:04d}',
                'customer_name': 'Rudo', 'status': 'pending',
                'project_type': 'bathroom_renovation',
                'project_description': 'Full re-tile and new fittings',
                'customer_area': 'Borrowdale'}
    defaults.update(kwargs)
    return Appointment.objects.create(**defaults)


def _date_reader(day):
    """Stands in for _compute_followup_date, which may ask DeepSeek."""
    return lambda _msg: (day.isoformat(), day.strftime('%A %d %B'))


class OfflineTestCase(TestCase):
    """Nothing in this module may reach DeepSeek, WhatsApp or an email API.

    `manage.py test` loads the real .env, so a path the test did not expect
    (a classifier fallback, the PDF-on-WhatsApp branch) otherwise makes a PAID
    live call and a real send; the first run of the date-correction case did
    exactly that. Every live seam is shut here, and a test that needs a seam
    to return something patches it again on top.
    """

    def setUp(self):
        super().setUp()
        for target, kwargs in (
            ('bot.out_of_scope_handler._DEEPSEEK_KEY', {'new': ''}),
            ('bot.services.clients.deepseek_call', {'side_effect': AssertionError('live DeepSeek call')}),
            ('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp', {'return_value': True}),
            ('bot.plumber_notifications.send_email_to_recipients', {'return_value': True}),
            ('bot.customer_emails.send_delay_quote_email_async', {}),
            ('bot.management.commands.send_followups.get_client_for_tenant', {}),
            # The net under the seams above: any HTTP at all fails the test.
            ('requests.sessions.Session.send', {'side_effect': AssertionError('live HTTP (requests)')}),
            ('httpx.Client.send', {'side_effect': AssertionError('live HTTP (httpx)')}),
        ):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)


class DelayFlowLadderTests(OfflineTestCase):
    """What the lead is told when they put the job off, and what gets armed."""

    def setUp(self):
        super().setUp()
        self.job = date.today() + timedelta(days=30)
        self.checkback = self.job - timedelta(days=7)

    def _far_answer(self, lead, message='in a month'):
        from bot.out_of_scope_handler import _handle_delay_timeframe_answer
        with patch('bot.out_of_scope_handler._compute_followup_date',
                   _date_reader(self.job)):
            return _handle_delay_timeframe_answer(message, {}, lead)

    def test_far_date_asks_permission_for_the_literal_date_then_the_email(self):
        from bot.views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER
        from bot import copy_catalog
        lead = make_lead(1)
        reply = self._far_answer(lead)
        ask, second = reply.split(MESSAGE_SPLIT_MARKER)
        self.assertIn(f'on {ladder.spoken_date(self.checkback)}?', ask)
        self.assertIn('go over a quote', ask)
        self.assertTrue(second.rstrip().endswith(copy_catalog.BEST_EMAIL_ASK))
        # The link waits for the portfolio: no email yet, so no portfolio yet.
        self.assertNotIn(PLUMBER_WA, reply)

        lead.refresh_from_db()
        self.assertEqual(ladder.job_date(lead), self.job)
        self.assertEqual(ladder.step(lead), ladder.STEP_FIRST)
        self.assertEqual(timezone.localtime(lead.delay_followup_due_at).date(),
                         self.checkback)
        self.assertIn(f'category=delay_email original={self.checkback.isoformat()}',
                      lead.internal_notes)

    def test_each_part_keeps_its_one_question_through_the_outbound_chain(self):
        """The brief asks both things in one go; the chain keeps ONE question
        per part, so they must arrive as two parts or one is deleted."""
        from bot.whatsapp_webhook import finalise_outbound
        lead = make_lead(2)
        out = finalise_outbound(self._far_answer(lead), lead, 'in a month',
                                check=False)
        self.assertIn(f'{ladder.spoken_date(self.checkback)}?', out)
        self.assertIn("What's the best email for it?", out)

    @patch('bot.customer_emails.send_delay_quote_email_async')
    def test_email_answer_sends_the_portfolio_and_the_plumber_link_together(self, portfolio):
        from bot.out_of_scope_handler import _handle_delay_email_answer, _read_pending
        lead = make_lead(3)
        self._far_answer(lead)
        lead.refresh_from_db()
        reply = _handle_delay_email_answer('rudo@example.com', _read_pending(lead), lead)

        portfolio.assert_called_once()
        self.assertIn('portfolio is on its way', reply)
        self.assertIn(f'on {ladder.spoken_date(self.checkback)}', reply)
        self.assertIn(PLUMBER_WA, reply)
        self.assertIn('free online quote', reply)
        lead.refresh_from_db()
        self.assertIn(LINK_SENT_TAG, lead.internal_notes)
        self.assertEqual(ladder.job_date(lead), self.job)   # still armed

    @patch('bot.customer_emails.send_delay_quote_email_async')
    def test_email_on_file_gets_the_portfolio_and_link_straight_away(self, portfolio):
        from bot.views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER
        lead = make_lead(4, customer_email='rudo@example.com')
        ask, second = self._far_answer(lead).split(MESSAGE_SPLIT_MARKER)
        portfolio.assert_called_once()
        self.assertIn(ladder.spoken_date(self.checkback), ask)
        self.assertIn('portfolio', second)
        self.assertIn(PLUMBER_WA, second)

    @patch('bot.out_of_scope_handler._deliver_pdf_and_schedule_checkin', return_value=True)
    def test_a_declined_email_still_gets_the_link_with_the_pdf(self, _pdf):
        from bot.out_of_scope_handler import _handle_delay_email_answer, _read_pending
        lead = make_lead(5)
        self._far_answer(lead)
        lead.refresh_from_db()
        with patch('bot.out_of_scope_handler._classify_email_step_reply',
                   return_value='whatsapp'):
            reply = _handle_delay_email_answer('just send it here', _read_pending(lead), lead)
        self.assertIn(PLUMBER_WA, reply)

    def test_a_new_date_in_answer_to_the_permission_question_moves_the_ladder(self):
        """'No, make it in two months' answers the date question. It used to
        classify as a declined email and send the PDF instead."""
        from bot.out_of_scope_handler import (_handle_delay_email_answer,
                                              _handle_delay_timeframe_answer,
                                              _read_pending)
        lead = make_lead(6)
        self._far_answer(lead)
        lead.refresh_from_db()
        later = self.job + timedelta(days=30)
        with patch('bot.out_of_scope_handler._compute_followup_date',
                   _date_reader(later)):
            reply = _handle_delay_email_answer(
                'No, rather in two months', _read_pending(lead), lead)
        lead.refresh_from_db()
        self.assertEqual(ladder.job_date(lead), later)
        self.assertIn(ladder.spoken_date(later - timedelta(days=7)), reply)
        self.assertNotIn(PLUMBER_WA, reply)

    def test_a_near_self_defer_keeps_the_old_shape_and_no_ladder(self):
        from bot.out_of_scope_handler import _handle_delay_timeframe_answer
        lead = make_lead(7)
        near = date.today() + timedelta(days=3)
        with patch('bot.out_of_scope_handler._compute_followup_date',
                   _date_reader(near)):
            reply = _handle_delay_timeframe_answer(
                "I'll get in touch in 3 days", {}, lead)
        lead.refresh_from_db()
        self.assertIsNone(ladder.job_date(lead))
        self.assertNotIn('Will it be okay', reply)


class VagueTimeframeTests(OfflineTestCase):
    """A vague timeframe is an answer: we assume a date and never re-ask
    (owner rule, 2026-09-21). Runs the real delay handler, date reader
    included; bot.vague_dates answers before any model is consulted."""

    def test_vague_delay_answers_are_never_asked_again(self):
        from bot.out_of_scope_handler import _handle_delay_timeframe_answer
        for n, phrase in enumerate(('month end', 'mid next week', 'early next month',
                                    'in a few weeks', 'after payday'), start=40):
            lead = make_lead(n)
            reply = _handle_delay_timeframe_answer(phrase, {}, lead)
            lower = reply.lower()
            self.assertNotIn('roughly when', lower, phrase)
            self.assertNotIn('what day', lower, phrase)
            self.assertNotIn('beginning', lower, phrase)
            # Either the near-date booking pivot (a day assumed, time asked) or
            # the far-date ladder (a literal follow-up date asked about).
            self.assertTrue("let's say" in lower or 'will it be okay if we follow up' in lower,
                            f'{phrase}: {reply}')
            lead.refresh_from_db()
            self.assertNotIn('[DELAY_TF_REASK]', lead.internal_notes or '', phrase)


class JobLadderCronTests(OfflineTestCase):
    """send_followups walking the ladder, on a frozen clock."""

    JOB = date(2030, 3, 20)          # -7 = 13th, -3 = 17th, -2 = 18th

    def setUp(self):
        super().setUp()
        from bot.management.commands.send_followups import Command, SA_TIMEZONE
        self.cmd = Command()
        self.tz = SA_TIMEZONE
        self.lead = make_lead(20, customer_email='rudo@example.com', is_delayed=True)
        ladder.arm(self.lead, self.JOB)

    def _at(self, day, hour=10):
        return self.tz.localize(datetime(2030, 3, day, hour, 0))

    def _tick(self, day, hour=10):
        now = self._at(day, hour)
        with patch('bot.management.commands.send_followups.timezone.now',
                   return_value=now):
            self.cmd._tick_job_ladder(self.lead, now, False, ladder)
        self.lead.refresh_from_db()

    @patch('bot.customer_emails._send', return_value=True)
    def test_not_due_before_the_day(self, send):
        self._tick(12)
        send.assert_not_called()
        self.assertEqual(ladder.step(self.lead), ladder.STEP_FIRST)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_minus_seven_then_minus_three_then_the_plumber_call(self, send, plumber):
        self._tick(13)
        self.assertEqual(send.call_count, 1)
        self.assertEqual(ladder.step(self.lead), ladder.STEP_SECOND)
        self.assertIsNotNone(ladder.first_sent_at(self.lead))
        self.assertIn(ladder.TRANSCRIPT_MARKER,
                      self.lead.conversation_history[-1]['content'])

        self._tick(16)                               # not yet -3
        self.assertEqual(send.call_count, 1)
        self._tick(17)
        self.assertEqual(send.call_count, 2)
        self.assertEqual(ladder.step(self.lead), ladder.STEP_CALL)

        self._tick(18)
        plumber.assert_called_once()
        subject, body = plumber.call_args[0][:2]
        self.assertTrue(subject.startswith('[Call]'))
        self.assertIn('B2.', body)                   # no quote on this lead
        self.assertEqual(ladder.step(self.lead), ladder.STEP_DONE)
        self.assertFalse(self.lead.is_delayed)

        self._tick(19)                               # finished: nothing more
        plumber.assert_called_once()

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_a_reply_after_the_first_touch_cancels_the_rest(self, send, plumber):
        self._tick(13)
        self.lead.last_customer_response = self._at(14)
        self.lead.save(update_fields=['last_customer_response'])
        self._tick(17)
        self._tick(18)
        self.assertEqual(send.call_count, 1)
        plumber.assert_not_called()
        self.assertIsNone(ladder.job_date(self.lead))

    @patch('bot.customer_emails._send', return_value=True)
    def test_a_booked_lead_is_taken_off_the_ladder(self, send):
        self.lead.status = 'confirmed'
        self.lead.save(update_fields=['status'])
        self._tick(13)
        send.assert_not_called()
        self.assertIsNone(ladder.job_date(self.lead))

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    def test_no_email_and_a_shut_window_still_reaches_the_plumber_call(self, plumber):
        """With no channel to the lead the touches cannot go, and the call is
        the only reach left, so the steps still advance."""
        self.lead.customer_email = ''
        self.lead.save(update_fields=['customer_email'])
        with patch.object(type(self.cmd), '_delay_wa_allowed',
                          return_value=(False, 'free-form window closed')):
            self._tick(13)
            self._tick(17)
            self._tick(18)
        plumber.assert_called_once()

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_a_cron_back_from_an_outage_fires_only_the_latest_step(self, send, plumber):
        self._tick(18)
        send.assert_not_called()
        plumber.assert_called_once()

    @patch('bot.customer_emails.send_delay_followup_email', return_value=True)
    def test_the_reactivation_loop_leaves_ladder_leads_alone(self, followup):
        """The stored check-back IS job - 7, so without the exclusion the old
        loop fired the same day with its own copy."""
        self.lead.delay_followup_due_at = timezone.now() - timedelta(hours=1)
        self.lead.save(update_fields=['delay_followup_due_at'])
        self.cmd._process_delayed_reactivations(timezone.now(), False)
        followup.assert_not_called()
