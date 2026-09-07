"""
Future-dated visits: the proposal, the two check-ins, and the one moment a
penciled-in day becomes a real booking (spec §11).

The rule underneath all of it: WhatsApp is free only for 24 hours after the
lead's last message, and this flow runs for weeks. So every touch here is
email, and a lead with no email is handed to the plumber rather than dropped.
"""

from datetime import date, datetime, time, timedelta
from unittest.mock import patch

import pytz
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from bot.models import Appointment, VisitProposal

SAST = pytz.timezone('Africa/Johannesburg')


def make_lead(suffix, **kwargs):
    defaults = {'phone_number': f'whatsapp:+1555000{suffix:04d}'}
    defaults.update(kwargs)
    return Appointment.objects.create(**defaults)


def at(d, hour=10, minute=0):
    return SAST.localize(datetime.combine(d, time(hour=hour, minute=minute)))


class VisitProposalTests(TestCase):
    """Making, updating and answering a proposal."""

    def setUp(self):
        self.lead = make_lead(8700, customer_name='Chipo', status='pending',
                              customer_email='chipo@example.com')
        self.lead.project_description = 'full bathroom redo'
        self.lead.customer_area = 'Avondale'
        self.lead.save()
        self.target = date(2026, 10, 30)
        self.proposed = date(2026, 10, 23)

    def _row(self, **kw):
        from bot.visit_proposal import ensure_proposal
        return ensure_proposal(self.lead, target_date=kw.get('target', self.target),
                               proposed_date=kw.get('proposed', self.proposed))

    def test_one_proposal_per_lead_however_often_it_comes_up(self):
        """They have one visit in mind, however many times the conversation
        circles back to it."""
        first = self._row()
        self.lead.refresh_from_db()
        again = self._row(proposed=date(2026, 10, 21))
        self.assertEqual(first.pk, again.pk)
        again.refresh_from_db()
        self.assertEqual(again.proposed_date, date(2026, 10, 21))

    def test_a_new_date_reopens_a_declined_proposal(self):
        from bot.visit_proposal import apply_lead_answer
        row = self._row()
        apply_lead_answer(row, confirmed=False)
        row.refresh_from_db()
        self.assertEqual(row.state, 'declined')

        self.lead.refresh_from_db()
        reopened = self._row(proposed=date(2026, 10, 26))
        reopened.refresh_from_db()
        self.assertEqual(reopened.state, 'proposed')
        self.assertIsNone(reopened.responded_at)

    def test_a_yes_is_the_only_thing_that_books_it(self):
        """Until the lead answers, the diary must not show a booking. That is
        the whole reason the tentative state is not an Appointment.status."""
        from bot.visit_proposal import apply_lead_answer
        row = self._row()
        self.lead.refresh_from_db()
        self.assertNotEqual(self.lead.status, 'confirmed')
        self.assertIsNone(self.lead.scheduled_datetime)

        apply_lead_answer(row, confirmed=True)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'confirmed')
        self.assertEqual(
            self.lead.scheduled_datetime.astimezone(SAST).date(), self.proposed)

    def test_a_no_books_nothing(self):
        from bot.visit_proposal import apply_lead_answer
        row = self._row()
        apply_lead_answer(row, confirmed=False)
        self.lead.refresh_from_db()
        self.assertNotEqual(self.lead.status, 'confirmed')
        self.assertIsNone(self.lead.scheduled_datetime)

    def test_answering_twice_decides_once(self):
        """Both check-in emails carry the links, so a lead may tap two of them."""
        from bot.visit_proposal import apply_lead_answer
        row = self._row()
        self.assertTrue(apply_lead_answer(row, confirmed=True))
        self.assertFalse(apply_lead_answer(row, confirmed=False))
        row.refresh_from_db()
        self.assertEqual(row.state, 'confirmed')

    def test_a_refused_email_routes_to_the_plumber(self):
        from bot.visit_proposal import record_email_choice
        row = self._row()
        self.assertFalse(row.goes_to_the_plumber)
        record_email_choice(row, gave_email=False, channel='whatsapp')
        row.refresh_from_db()
        self.assertTrue(row.goes_to_the_plumber)
        self.assertEqual(row.portfolio_sent_channel, 'whatsapp')

    def test_no_email_on_file_also_routes_to_the_plumber(self):
        self.lead.customer_email = ''
        self.lead.save(update_fields=['customer_email'])
        row = self._row()
        row.refresh_from_db()
        self.assertTrue(row.goes_to_the_plumber)


class VisitCheckinSchedulerTests(TestCase):
    """The two check-ins, at target minus 7 and minus 4 days."""

    def setUp(self):
        self.lead = make_lead(8701, customer_name='Tino', status='pending',
                              customer_email='tino@example.com')
        self.target = date(2026, 10, 30)
        from bot.visit_proposal import ensure_proposal
        self.row = ensure_proposal(self.lead, target_date=self.target,
                                   proposed_date=date(2026, 10, 23))

    def _tick(self, on, **kw):
        from bot.visit_proposal import run_visit_proposal_tick
        return run_visit_proposal_tick(now=at(on), **kw)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_the_first_checkin_lands_seven_days_out(self, send):
        self.assertEqual(self._tick(self.target - timedelta(days=9))['lead_emails'], 0)
        self.assertEqual(self._tick(self.target - timedelta(days=7))['lead_emails'], 1)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_the_second_lands_four_days_out_and_no_more_after(self, send):
        self._tick(self.target - timedelta(days=7))
        self.assertEqual(self._tick(self.target - timedelta(days=6))['lead_emails'], 0)
        self.assertEqual(self._tick(self.target - timedelta(days=4))['lead_emails'], 1)
        self.assertEqual(self._tick(self.target - timedelta(days=2))['lead_emails'], 0)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_a_late_proposal_does_not_fire_both_at_once(self, send):
        """Made five days out, it should send one and then the other, not two
        emails in the same minute."""
        stats = self._tick(self.target - timedelta(days=5))
        self.assertEqual(stats['lead_emails'], 1)
        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.checkin_1_sent_at)
        self.assertIsNone(self.row.checkin_2_sent_at)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_an_answered_proposal_is_never_chased(self, send):
        from bot.visit_proposal import apply_lead_answer
        apply_lead_answer(self.row, confirmed=True)
        self.assertEqual(self._tick(self.target - timedelta(days=7))['lead_emails'], 0)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_it_gives_up_rather_than_asking_the_morning_of(self, send):
        stats = self._tick(self.target + timedelta(days=1))
        self.assertEqual(stats['lapsed'], 1)
        self.row.refresh_from_db()
        self.assertEqual(self.row.state, 'lapsed')

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_a_lead_who_booked_meanwhile_is_left_alone(self, send):
        """Three weeks is a long time. State is re-checked before every send."""
        self.lead.status = 'confirmed'
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=3)
        self.lead.save(update_fields=['status', 'scheduled_datetime'])
        stats = self._tick(self.target - timedelta(days=7))
        self.assertEqual(stats['lead_emails'], 0)
        self.assertEqual(stats['skipped'], 1)

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        stats = self._tick(self.target - timedelta(days=7), dry_run=True)
        self.assertEqual(stats['lead_emails'], 1)
        self.row.refresh_from_db()
        self.assertIsNone(self.row.checkin_1_sent_at)


class VisitCheckinHandoffTests(TestCase):
    """No email means no channel, so the plumber does the outreach."""

    def setUp(self):
        self.lead = make_lead(8702, customer_name='Anesu', status='pending')
        self.lead.project_description = 'new ensuite'
        self.lead.customer_area = 'Budiriro'
        self.lead.timeline = 'end of October'
        self.lead.save()
        self.target = date(2026, 10, 30)
        from bot.visit_proposal import ensure_proposal
        self.row = ensure_proposal(self.lead, target_date=self.target,
                                   proposed_date=date(2026, 10, 23))

    def _tick(self, on, hour=10, **kw):
        from bot.visit_proposal import run_visit_proposal_tick
        return run_visit_proposal_tick(now=at(on, hour=hour), **kw)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    def test_the_checkin_goes_to_the_plumber(self, send):
        stats = self._tick(self.target - timedelta(days=7))
        self.assertEqual(stats['plumber_emails'], 1)
        self.assertEqual(stats['lead_emails'], 0)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    def test_it_waits_for_the_morning(self, send):
        """A lead-outreach prompt at 3am gets read and forgotten."""
        self.assertEqual(
            self._tick(self.target - timedelta(days=7), hour=3)['plumber_emails'], 0)
        self.assertEqual(
            self._tick(self.target - timedelta(days=7), hour=9)['plumber_emails'], 1)

    def test_the_whatsapp_link_is_ready_to_send_as_is(self):
        """A plumber who has to edit it will not send it, so the message
        carries the lead's own job, area, timeline and the proposed day."""
        from bot.plumber_notifications import send_visit_handoff_email
        with patch('bot.plumber_notifications.send_plumber_notification_email',
                   return_value=True) as send:
            send_visit_handoff_email(self.row, number=1)
        body = send.call_args.args[1]
        self.assertIn('wa.me/', body)
        for detail in ('new%20ensuite', 'Budiriro', 'October'):
            self.assertIn(detail, body)

    def test_a_lead_with_no_phone_is_not_handed_over(self):
        """The link would be meaningless, and a plumber cannot ring a blank."""
        from bot.plumber_notifications import send_visit_handoff_email
        self.lead.phone_number = ''
        self.lead.save(update_fields=['phone_number'])
        self.row.refresh_from_db()
        self.assertFalse(send_visit_handoff_email(self.row, number=1))


class VisitProposalAnswerViewTests(TestCase):
    """The lead's one-tap yes or no."""

    def setUp(self):
        self.lead = make_lead(8703, customer_name='Nyasha', status='pending',
                              customer_email='nyasha@example.com')
        from bot.visit_proposal import ensure_proposal
        self.row = ensure_proposal(self.lead, target_date=date(2026, 10, 30),
                                   proposed_date=date(2026, 10, 23))

    def _url(self, answer):
        return reverse('visit_proposal_answer',
                       args=[self.row.token, answer])

    def test_yes_books_it_with_no_login(self):
        response = Client().get(self._url('yes'))
        self.assertEqual(response.status_code, 200)
        self.row.refresh_from_db()
        self.assertEqual(self.row.state, 'confirmed')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'confirmed')

    def test_no_records_it_and_books_nothing(self):
        Client().get(self._url('no'))
        self.row.refresh_from_db()
        self.assertEqual(self.row.state, 'declined')
        self.lead.refresh_from_db()
        self.assertIsNone(self.lead.scheduled_datetime)

    def test_a_second_tap_shows_the_answer_already_given(self):
        client = Client()
        client.get(self._url('yes'))
        response = client.get(self._url('no'))
        self.assertContains(response, 'already answered')
        self.row.refresh_from_db()
        self.assertEqual(self.row.state, 'confirmed')

    def test_a_bad_token_is_a_404(self):
        self.assertEqual(
            Client().get(reverse('visit_proposal_answer',
                                 args=['nope', 'yes'])).status_code, 404)

    def test_a_failed_booking_never_claims_it_was_booked(self):
        with patch('bot.visit_proposal._book_the_visit',
                   side_effect=RuntimeError('db down')):
            response = Client().get(self._url('yes'))
        self.assertEqual(response.status_code, 500)
        self.assertContains(response, 'could not save', status_code=500)
        self.assertNotContains(response, 'See you on', status_code=500)

        # And the row must not quietly believe it worked. Marking it before
        # the diary write would leave it 'confirmed' with nothing booked, the
        # check-ins stopped, and the lead told it failed.
        self.row.refresh_from_db()
        self.assertEqual(self.row.state, 'proposed')
        self.assertTrue(self.row.is_open)
