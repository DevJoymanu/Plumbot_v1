"""
Is the follow-up cron running? (bot/cron_health.py)

2026-09-21 12:48: a Follow_Ups deployment lost its cron schedule, showed
SUCCESS, and sent nothing for about 20 hours; follow-ups only went out when the
owner pressed "Run check". The cron now records each run, and the
Email_Follow_Ups cron emails the operator once when it goes quiet.
"""

from datetime import datetime, timedelta
from unittest.mock import patch

from django.test import TestCase

from bot import cron_health


def sast(hour, minute=0, day=22):
    from bot.management.commands.send_followups import SA_TIMEZONE
    return SA_TIMEZONE.localize(datetime(2026, 9, day, hour, minute))


class CronHealthTests(TestCase):

    def setUp(self):
        self.sent = []

    def _send(self, subject, body):
        self.sent.append((subject, body))
        return True

    def test_a_run_is_recorded_and_read_back(self):
        self.assertIsNone(cron_health.last_run())
        cron_health.beat(sast(10, 0))
        self.assertEqual(cron_health.last_run(), sast(10, 0))

    def test_a_fresh_heartbeat_sends_no_alert(self):
        cron_health.beat(sast(10, 0))
        self.assertFalse(cron_health.alert_if_stale(sast(10, 20), send=self._send))
        self.assertEqual(self.sent, [])

    def test_a_quiet_cron_alerts_once_per_outage(self):
        cron_health.beat(sast(10, 0))
        self.assertTrue(cron_health.alert_if_stale(sast(10, 45), send=self._send))
        self.assertIn('has not run since', self.sent[0][1])
        # Every 5-minute check after that is the same outage: no second email.
        self.assertFalse(cron_health.alert_if_stale(sast(10, 50), send=self._send))
        self.assertFalse(cron_health.alert_if_stale(sast(13, 0), send=self._send))
        self.assertEqual(len(self.sent), 1)

    def test_a_run_after_an_outage_arms_the_alert_again(self):
        cron_health.beat(sast(10, 0))
        cron_health.alert_if_stale(sast(10, 45), send=self._send)
        cron_health.beat(sast(11, 0))
        self.assertTrue(cron_health.alert_if_stale(sast(11, 45), send=self._send))
        self.assertEqual(len(self.sent), 2)

    def test_never_ran_alerts_too(self):
        self.assertTrue(cron_health.alert_if_stale(sast(12, 0), send=self._send))
        self.assertIn('never', self.sent[0][1])

    def test_no_alert_outside_the_sending_hours_or_just_after_they_open(self):
        cron_health.beat(sast(21, 0, day=21))
        self.assertFalse(cron_health.alert_if_stale(sast(6, 0), send=self._send))
        self.assertFalse(cron_health.alert_if_stale(sast(8, 20), send=self._send))
        self.assertFalse(cron_health.alert_if_stale(sast(22, 0), send=self._send))
        self.assertEqual(self.sent, [])

    def test_only_the_scheduled_run_beats_not_run_check(self):
        """Run Check runs the same command in the web service; it must not make
        a dead cron look alive."""
        from django.core.management import call_command
        from io import StringIO
        with patch('bot.management.commands.send_followups.Command._in_contact_window',
                   return_value=False), \
             patch.dict('os.environ', {'PLUMBOT_CRON': ''}):
            call_command('send_followups', stdout=StringIO())
        self.assertIsNone(cron_health.last_run())
        with patch('bot.management.commands.send_followups.Command._in_contact_window',
                   return_value=False), \
             patch.dict('os.environ', {'PLUMBOT_CRON': 'send_followups'}):
            call_command('send_followups', stdout=StringIO())
        self.assertIsNotNone(cron_health.last_run())
