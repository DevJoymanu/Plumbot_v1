"""
The hourly check for a lead message that never got a reply (bot/unanswered_sweep.py).

Owner, 2026-09-22: lead 1161's "How much" landed while a deploy was swapping
servers and was saved but never answered. Once an hour, anything a lead said
that we never answered gets answered through the normal pipeline.
"""

from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from bot import unanswered_sweep as sweep
from bot.models import Appointment


class UnansweredSweepTests(TestCase):

    def setUp(self):
        self.now = timezone.now()
        patcher = patch('bot.whatsapp_webhook._generate_and_schedule_reply')
        self.reply = patcher.start()
        self.addCleanup(patcher.stop)
        window = patch.object(Appointment, 'messaging_window_open', new=True)
        window.start()
        self.addCleanup(window.stop)

    def _lead(self, n, history, **kw):
        last_in = kw.pop('last_in', self.now - timedelta(hours=1))
        return Appointment.objects.create(
            phone_number=f'whatsapp:+1555002{n:04d}', status='pending',
            conversation_history=history, last_customer_response=last_in, **kw)

    def _turn(self, role, content, ago):
        return {'role': role, 'content': content,
                'timestamp': (self.now - ago).isoformat()}

    def test_a_message_with_no_reply_is_answered(self):
        lead = self._lead(1, [self._turn('assistant', 'What area are you in?', timedelta(hours=2)),
                              self._turn('user', 'How much', timedelta(hours=1))])
        counts = sweep.answer_unanswered(self.now)
        self.assertEqual(counts['answered'], 1)
        sender, text = self.reply.call_args.args[:2]
        self.assertEqual((sender, text), ('15550020001', 'How much'))
        self.assertEqual(self.reply.call_args.kwargs['tenant'], lead.tenant)

    def test_several_unanswered_messages_are_answered_together(self):
        self._lead(2, [self._turn('user', 'How much', timedelta(hours=2)),
                       self._turn('user', 'And this one', timedelta(hours=1))])
        sweep.answer_unanswered(self.now)
        self.assertEqual(self.reply.call_args.args[1], 'How much\nAnd this one')

    def test_answered_messages_are_left_alone(self):
        self._lead(3, [self._turn('user', 'How much', timedelta(hours=2)),
                       self._turn('assistant', 'From US$235', timedelta(hours=1))])
        sweep.answer_unanswered(self.now)
        self.reply.assert_not_called()

    def test_a_paused_lead_is_a_humans_conversation(self):
        self._lead(4, [self._turn('user', 'Looking for a job', timedelta(hours=1))],
                   chatbot_paused=True)
        sweep.answer_unanswered(self.now)
        self.reply.assert_not_called()

    def test_too_fresh_the_live_reply_may_still_be_coming(self):
        self._lead(5, [self._turn('user', 'How much', timedelta(minutes=5))],
                   last_in=self.now - timedelta(minutes=5))
        sweep.answer_unanswered(self.now)
        self.reply.assert_not_called()

    def test_a_message_is_only_ever_tried_once(self):
        """A bare "thank you" the pipeline chose to leave is not retried hourly."""
        lead = self._lead(6, [self._turn('user', 'Ok thank you', timedelta(hours=1))])
        sweep.answer_unanswered(self.now)
        sweep.answer_unanswered(self.now + timedelta(hours=1))
        self.assertEqual(self.reply.call_count, 1)
        lead.refresh_from_db()
        self.assertTrue(lead.conversation_history[-1].get(sweep.SWEEP_KEY))

    def test_a_photo_turn_is_not_answered_as_text(self):
        self._lead(7, [self._turn('user', '[Sent image] a bathroom', timedelta(hours=1))])
        sweep.answer_unanswered(self.now)
        self.reply.assert_not_called()

    def test_outside_the_free_window_nothing_is_sent(self):
        self._lead(8, [self._turn('user', 'How much', timedelta(hours=1))])
        with patch.object(Appointment, 'messaging_window_open', new=False):
            sweep.answer_unanswered(self.now)
        self.reply.assert_not_called()

    def test_replies_go_out_immediately_during_the_sweep(self):
        """The cron process exits after the command; a delayed send would die."""
        from bot import whatsapp_webhook as wh
        seen = []
        self.reply.side_effect = lambda *a, **k: seen.append(wh.get_random_delay())
        self._lead(9, [self._turn('user', 'How much', timedelta(hours=1))])
        sweep.answer_unanswered(self.now)
        self.assertEqual(seen, [0])
        self.assertFalse(wh._immediate_replies)          # switched back off

    def test_it_sweeps_on_the_first_tick_of_each_hour_only(self):
        from bot.management.commands.send_followups import SA_TIMEZONE
        from datetime import datetime
        self.assertTrue(sweep.is_sweep_tick(SA_TIMEZONE.localize(datetime(2030, 3, 1, 10, 2))))
        self.assertFalse(sweep.is_sweep_tick(SA_TIMEZONE.localize(datetime(2030, 3, 1, 10, 7))))
