"""
Plan path: the plumber notification, the reminders, the form and the lead
follow-up (spec §10.3 to §10.5).

Same contract as the post-visit scheduler these are modelled on: anchors and
offsets rather than a job queue, every send gated by the timestamp written as
it goes out, and lead state re-checked before each one. All outbound is mocked.
"""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from bot.models import Appointment


def make_lead(suffix, **kwargs):
    defaults = {'phone_number': f'whatsapp:+1555000{suffix:04d}'}
    defaults.update(kwargs)
    return Appointment.objects.create(**defaults)


class PlanQuoteSchedulerTests(TestCase):
    """The cron for a lead who sent a plan."""

    def setUp(self):
        self.lead = make_lead(
            8600, customer_name='Rudo', status='pending',
            customer_email='rudo@example.com',
        )
        self.lead.project_description = 'ensuite, full redo'
        self.lead.customer_area = 'Budiriro'
        self.lead.timeline = 'next month'
        self.lead.save()

    def _row(self, when=None):
        from bot.plan_quote import ensure_request
        return ensure_request(self.lead, when=when)

    def _tick(self, now=None, **kw):
        from bot.plan_quote import run_plan_quote_tick
        return run_plan_quote_tick(now=now, **kw)

    def test_one_row_per_lead_however_many_files_arrive(self):
        """A lead who sends three shots of the same drawing gets one row."""
        first = self._row()
        self.lead.refresh_from_db()
        self.assertEqual(first.pk, self._row().pk)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_the_plumber_waits_the_full_hour_even_with_everything_in(self, send):
        """The hour is what the bot gets to try for a booking before the
        plumber is disturbed. Sending early because the questions were answered
        throws that away."""
        row = self._row()
        self.assertEqual(
            self._tick(now=row.plan_received_at + timedelta(minutes=2))
            ['plumber_emails'], 0)
        self.assertEqual(
            self._tick(now=row.plan_received_at + timedelta(hours=1, minutes=1))
            ['plumber_emails'], 1)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_a_lead_who_books_inside_the_hour_never_reaches_the_plumber(self, send):
        """The whole point of the delay. They booked, so there is nothing to
        chase and no reason to disturb anyone."""
        row = self._row()
        self.lead.status = 'confirmed'
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=2)
        self.lead.save(update_fields=['status', 'scheduled_datetime'])
        self.assertEqual(
            self._tick(now=row.plan_received_at + timedelta(hours=2))
            ['plumber_emails'], 0)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_a_plan_always_reaches_the_plumber_even_half_answered(self, send):
        """A plan goes over regardless. What the bot could not get is named in
        the email for the plumber to ask."""
        self.lead.timeline = ''
        self.lead.customer_area = ''
        self.lead.save(update_fields=['timeline', 'customer_area'])
        row = self._row()
        late = row.plan_received_at + timedelta(hours=1, minutes=1)
        self.assertEqual(self._tick(now=late)['plumber_emails'], 1)

    def test_the_email_names_what_the_bot_could_not_get(self):
        from bot.plan_quote import missing_info
        self.lead.timeline = ''
        self.lead.customer_area = ''
        self.lead.save(update_fields=['timeline', 'customer_area'])
        self.assertEqual(missing_info(self.lead), ['the area', 'when they want it done'])
        self.lead.refresh_from_db()

    def _with_timeline(self, raw, days):
        """Set the timeline the way the capture does: the words AND the number
        resolved from them, which is what everything downstream reads."""
        row = self._row()
        self.lead.timeline = raw
        self.lead.save(update_fields=['timeline'])
        row.timeline_days = days
        row.save(update_fields=['timeline_days'])
        self.lead.refresh_from_db()
        return row

    def test_a_far_off_timeline_is_flagged_as_not_urgent(self):
        """A lead who wants the job in three months must not be worked as an
        emergency callout."""
        from bot.plan_quote import timeline_is_slow
        self._with_timeline('in about three months', 91)
        self.assertTrue(timeline_is_slow(self.lead))

    def test_a_timeline_inside_the_week_is_not_flagged(self):
        from bot.plan_quote import timeline_is_slow
        self._with_timeline('this week', 3)
        self.assertFalse(timeline_is_slow(self.lead))

    def test_the_boundary_is_a_week(self):
        """Seven days is near, eight is not. 'next week' resolves to +9 and is
        therefore a nurture lead, not a booking push."""
        from bot.plan_quote import timeline_is_slow
        self._with_timeline('a week today', 7)
        self.assertFalse(timeline_is_slow(self.lead))
        self._with_timeline('next week', 9)
        self.assertTrue(timeline_is_slow(self.lead))

    def test_an_unresolved_timeline_counts_as_near(self):
        """Pushing for a day is recoverable. Quietly parking a lead who wanted
        the job this week is not."""
        from bot.plan_quote import timeline_is_slow
        self._with_timeline('whenever suits you really', None)
        self.assertFalse(timeline_is_slow(self.lead))

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_the_plumber_email_is_sent_once_not_every_tick(self, send):
        row = self._row()
        after = row.plan_received_at + timedelta(hours=1, minutes=1)
        self.assertEqual(self._tick(now=after)['plumber_emails'], 1)
        self.assertEqual(self._tick(now=after)['plumber_emails'], 0)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_reminders_fire_at_2_4_and_8_hours_then_stop(self, send, remind):
        row = self._row()
        self._tick(now=row.plan_received_at + timedelta(hours=1, minutes=1))
        row.refresh_from_db()
        anchor = row.plumber_email_sent_at

        for hours, expected in ((1, 0), (2, 1), (4, 1), (8, 1), (24, 0)):
            stats = self._tick(now=anchor + timedelta(hours=hours, minutes=1))
            self.assertEqual(stats['reminders'], expected, f'at +{hours}h')
        row.refresh_from_db()
        self.assertEqual(row.reminders_sent, 3)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_answering_the_form_stops_the_reminders(self, send, remind):
        from bot.plan_quote import apply_plumber_form
        row = self._row()
        self._tick(now=row.plan_received_at + timedelta(hours=1, minutes=1))
        row.refresh_from_db()
        anchor = row.plumber_email_sent_at

        apply_plumber_form(row, quote_sent=True)
        self.assertEqual(
            self._tick(now=anchor + timedelta(hours=9))['reminders'], 0)

    def test_the_form_is_single_use(self):
        """The same link goes out in four emails, so it will be tapped twice.
        The second tap must not overwrite the first answer."""
        from bot.plan_quote import apply_plumber_form
        row = self._row()
        self.assertTrue(apply_plumber_form(row, quote_sent=True,
                                           quote_amount=Decimal('450')))
        self.assertFalse(apply_plumber_form(row, quote_sent=False))
        row.refresh_from_db()
        self.assertEqual(row.quote_status, 'sent_confirmed')
        self.assertEqual(row.quote_amount, Decimal('450'))

    def test_the_form_can_add_a_missing_lead_email(self):
        """It is the only place one can be added, and without it there is no
        free way to reach the lead later at all."""
        from bot.plan_quote import apply_plumber_form
        self.lead.customer_email = ''
        self.lead.save(update_fields=['customer_email'])
        row = self._row()
        apply_plumber_form(row, quote_sent=True, lead_email='new@example.com')
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.customer_email, 'new@example.com')

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_lead_followup_waits_an_hour_after_the_plumber_confirms(self, send):
        from bot.plan_quote import apply_plumber_form
        row = self._row()
        self._tick(now=row.plan_received_at + timedelta(hours=1, minutes=1))
        row.refresh_from_db()
        apply_plumber_form(row, quote_sent=True)
        row.refresh_from_db()
        done = row.plumber_form_completed_at

        self.assertEqual(
            self._tick(now=done + timedelta(minutes=30))['lead_followups'], 0)
        self.assertEqual(
            self._tick(now=done + timedelta(hours=1, minutes=1))['lead_followups'], 1)
        row.refresh_from_db()
        self.assertEqual(row.quote_status, 'sent_confirmed')

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_a_silent_plumber_does_not_strand_the_lead(self, send, remind):
        """After 12h we assume the quote went out and follow up anyway, rather
        than let the lead go cold waiting on us.

        The anchor is pinned so +12h lands inside a contact window. Left on the
        real clock this test would pass or fail depending on the time of day it
        was run, which is worse than no test.
        """
        import pytz
        from datetime import datetime
        from bot.management.commands.send_followups import CONTACT_WINDOWS

        tz = pytz.timezone('Africa/Johannesburg')
        open_h, open_m = CONTACT_WINDOWS[0][:2]
        # 12 hours before the midday window opens, plus a few minutes so the
        # +12h moment sits inside it rather than exactly on the edge.
        anchor = tz.localize(datetime(2026, 6, 23, open_h, open_m + 5)) - timedelta(hours=12)

        row = self._row()
        row.plumber_email_sent_at = anchor
        row.save(update_fields=['plumber_email_sent_at'])

        self.assertEqual(
            self._tick(now=anchor + timedelta(hours=11))['lead_followups'], 0)
        self.assertEqual(
            self._tick(now=anchor + timedelta(hours=12))['lead_followups'], 1)
        row.refresh_from_db()
        self.assertEqual(row.quote_status, 'assumed')

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_a_booked_lead_is_never_chased(self, send):
        """The guard the old scheduler was missing. A lead can be booked or
        parked between two ticks, so state is re-checked before every send."""
        row = self._row()
        self.lead.status = 'confirmed'
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=2)
        self.lead.save(update_fields=['status', 'scheduled_datetime'])
        stats = self._tick(now=row.plan_received_at + timedelta(hours=2))
        self.assertEqual(stats['plumber_emails'], 0)
        self.assertEqual(stats['skipped'], 1)

    def test_dry_run_sends_nothing_and_writes_nothing(self):
        row = self._row()
        stats = self._tick(now=row.plan_received_at + timedelta(hours=1, minutes=1),
                           dry_run=True)
        self.assertEqual(stats['plumber_emails'], 1)
        row.refresh_from_db()
        self.assertIsNone(row.plumber_email_sent_at)


class PlanQuoteFormTests(TestCase):
    """The public, token-gated form the plumber taps from an email.

    Same shape as the site-visit debrief: a gate, then the questions, then
    straight into the quote screen. Only the gate's question differs, because
    nobody visited.
    """

    def setUp(self):
        self.lead = make_lead(8601, customer_name='Farai', status='pending')
        from bot.plan_quote import ensure_request
        self.row = ensure_request(self.lead)
        self.url = reverse('plan_quote_form', args=[self.row.token])

    def _quote_post(self, **over):
        data = {
            'outcome': 'quoting',
            'lead_email': 'farai@example.com',
            'expectation': 'timeframe',
            'expected_timeframe': 'two_weeks',
            'job_notes': 'Rip out and refit, wall-hung pan.',
        }
        data.update(over)
        return data

    def test_it_opens_with_no_login(self):
        """The plumber taps it on a driveway, from an email, with no session."""
        response = Client().get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Can you quote from it?')

    def test_a_bad_token_is_a_404_not_a_form(self):
        self.assertEqual(
            Client().get(reverse('plan_quote_form',
                                 args=['not-a-real-token'])).status_code, 404)

    def test_quoting_captures_everything_and_opens_the_quote(self):
        response = Client().post(self.url, self._quote_post())
        # Straight into the quote screen, like the site-visit form.
        self.assertEqual(response.status_code, 302)
        self.assertIn('create-quotation', response['Location'])

        self.row.refresh_from_db()
        self.assertIsNotNone(self.row.plumber_form_completed_at)
        self.assertEqual(self.row.outcome, 'quoting')
        self.assertEqual(self.row.expectation, 'timeframe')
        self.assertEqual(self.row.expected_timeframe, 'two_weeks')
        self.assertIn('wall-hung', self.row.job_notes)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.customer_email, 'farai@example.com')

    def test_the_job_notes_carry_into_the_quote_screen(self):
        """The plumber wrote them a minute ago and must not retype them."""
        Client().post(self.url, self._quote_post())
        self.lead.refresh_from_db()
        row = self.lead.plan_quote_request
        self.assertIn('wall-hung', row.job_notes)

    def test_a_named_date_is_captured(self):
        Client().post(self.url, self._quote_post(
            expectation='specific_date', expected_date='2026-11-20',
            expected_timeframe=''))
        self.row.refresh_from_db()
        self.assertEqual(str(self.row.expected_date), '2026-11-20')

    def test_quoting_without_an_email_is_refused(self):
        """It is the only channel that outlives the WhatsApp window, and this
        form is the one place a missing one can be added."""
        response = Client().post(self.url, self._quote_post(lead_email=''))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'email address')
        self.row.refresh_from_db()
        self.assertIsNone(self.row.plumber_form_completed_at)

    def test_quoting_without_a_timing_answer_is_refused(self):
        response = Client().post(self.url, self._quote_post(expectation=''))
        self.assertContains(response, 'expects the job done')
        self.row.refresh_from_db()
        self.assertIsNone(self.row.plumber_form_completed_at)

    def test_a_thin_plan_goes_back_to_the_site_visit(self):
        """Spec §10.9 item 6. The plan could not be priced, so the measure
        path and the paid visit come back into play."""
        self.lead.has_plan = True
        self.lead.plan_status = 'plan_uploaded'
        self.lead.save(update_fields=['has_plan', 'plan_status'])

        Client().post(self.url, {'outcome': 'needs_visit'})
        self.row.refresh_from_db()
        self.assertEqual(self.row.outcome, 'needs_visit')
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.has_plan)
        # on_plan_path must stop suppressing the visit close.
        from bot.controller import on_plan_path
        self.assertFalse(on_plan_path(self.lead))

    def test_not_proceeding_stops_everything(self):
        Client().post(self.url, {'outcome': 'not_proceeding'})
        self.row.refresh_from_db()
        self.assertEqual(self.row.outcome, 'not_proceeding')
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.is_lead_active)

    def test_no_outcome_re_renders_the_form_rather_than_guessing(self):
        response = Client().post(self.url, {})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'whether you can quote')
        self.row.refresh_from_db()
        self.assertIsNone(self.row.plumber_form_completed_at)

    def test_a_second_visit_shows_what_they_said(self):
        """The same link goes out in four emails, so it will be tapped twice."""
        client = Client()
        client.post(self.url, self._quote_post())
        response = client.get(self.url)
        self.assertContains(response, 'This one is done')


class PlanQuoteContactWindowTests(TestCase):
    """Branch B fires on a clock, so it must not message a lead at 3am."""

    def setUp(self):
        self.lead = make_lead(8602, customer_name='Tapiwa', status='pending',
                              customer_email='tapiwa@example.com')
        from bot.plan_quote import ensure_request
        self.row = ensure_request(self.lead)

    def _at(self, hour, minute=0):
        """A moment on a fixed day, in the tenant's local time."""
        import pytz
        from datetime import datetime
        tz = pytz.timezone('Africa/Johannesburg')
        return tz.localize(datetime(2026, 6, 23, hour, minute))

    def test_the_window_is_read_from_send_followups_not_copied(self):
        """One definition. A second copy is always the one nobody updates."""
        from bot.management.commands.send_followups import CONTACT_WINDOWS
        from bot.plan_quote import in_contact_window

        for open_h, open_m, close_h, close_m in CONTACT_WINDOWS:
            self.assertTrue(in_contact_window(self._at(open_h, open_m)))
            # Half-open: the close minute itself is already shut.
            self.assertFalse(in_contact_window(self._at(close_h, close_m)))

    def test_the_small_hours_are_not_a_contact_window(self):
        from bot.plan_quote import in_contact_window
        self.assertFalse(in_contact_window(self._at(3, 0)))
        self.assertFalse(in_contact_window(self._at(9, 0)))
        self.assertFalse(in_contact_window(self._at(23, 0)))

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_branch_b_waits_for_a_window_rather_than_waking_the_lead(self, send, remind):
        """A plan at 3pm makes +12h land at 3am. The follow-up holds until the
        next window instead of going then."""
        from bot.plan_quote import run_plan_quote_tick

        self.row.plumber_email_sent_at = self._at(15, 0)
        self.row.save(update_fields=['plumber_email_sent_at'])

        at_3am = self._at(3, 0) + timedelta(days=1)
        self.assertEqual(
            run_plan_quote_tick(now=at_3am)['lead_followups'], 0)

        from bot.management.commands.send_followups import CONTACT_WINDOWS
        open_h, open_m = CONTACT_WINDOWS[0][:2]
        in_window = self._at(open_h, open_m + 5) + timedelta(days=1)
        self.assertEqual(
            run_plan_quote_tick(now=in_window)['lead_followups'], 1)

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_branch_a_does_not_wait_for_a_window(self, send):
        """It is triggered by the plumber tapping the form, and they only do
        that during the working day."""
        from bot.plan_quote import apply_plumber_form, run_plan_quote_tick

        self.row.plumber_email_sent_at = self._at(12, 40)
        self.row.save(update_fields=['plumber_email_sent_at'])
        apply_plumber_form(self.row, quote_sent=True, now=self._at(13, 0))
        self.row.refresh_from_db()

        # An hour later is 14:00, inside a window today, but the point is that
        # the branch does not consult one at all.
        self.assertEqual(
            run_plan_quote_tick(now=self._at(14, 1))['lead_followups'], 1)


class PlanQuestionOrderTests(TestCase):
    """The plan path asks a different set of questions, in a different order.

        project_description -> area -> timeline -> book, or nurture.

    No service_type: they handed us the job on paper, and asking "bathroom or
    kitchen?" reads as though nobody looked at it. The type comes from the
    description they give.
    """

    def setUp(self):
        self.lead = make_lead(8800, customer_name='Sekai', status='pending')
        self.lead.has_plan = True
        self.lead.plan_status = 'plan_uploaded'
        self.lead.save()
        from bot.plan_quote import ensure_request
        self.row = ensure_request(self.lead)

    def _next(self):
        from bot.views import Plumbot
        bot = Plumbot(self.lead.phone_number, tenant=self.lead.tenant)
        bot.appointment = self.lead
        return bot.get_next_question_to_ask()

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self.lead, k, v)
        self.lead.save()

    def test_it_asks_for_the_description_first_not_the_service(self):
        self.assertEqual(self._next(), 'project_description')

    def test_then_the_area(self):
        self._set(project_description='rip out and refit the ensuite')
        self.assertEqual(self._next(), 'area')

    def test_then_the_timeline(self):
        self._set(project_description='rip out and refit', customer_area='Budiriro')
        self.assertEqual(self._next(), 'timeline')

    def test_a_near_timeline_goes_on_to_book(self):
        self._set(project_description='rip out and refit',
                  customer_area='Budiriro', timeline='this week')
        self.row.timeline_days = 3
        self.row.save(update_fields=['timeline_days'])
        self.lead.refresh_from_db()
        self.assertEqual(self._next(), 'availability_date')

    def test_a_far_timeline_is_not_pushed_for_a_day(self):
        """Over a week out the lead is nurtured, not chased for a slot."""
        self._set(project_description='rip out and refit',
                  customer_area='Budiriro', timeline='in about three months')
        self.row.timeline_days = 91
        self.row.save(update_fields=['timeline_days'])
        self.lead.refresh_from_db()
        self.assertEqual(self._next(), 'complete')

    def test_a_lead_with_no_plan_keeps_the_ordinary_order(self):
        """The plan order must not leak onto everyone else."""
        self.lead.has_plan = None
        self.lead.plan_status = None
        self.lead.save(update_fields=['has_plan', 'plan_status'])
        self.assertEqual(self._next(), 'service_type')

    def test_a_promised_plan_is_not_a_plan(self):
        """has_plan goes true when a lead SAYS one is coming. Until it lands
        there is nothing to reorder the flow around."""
        self.lead.plan_status = 'pending_upload'
        self.lead.save(update_fields=['plan_status'])
        self.assertEqual(self._next(), 'service_type')

    def test_the_timeline_question_names_their_own_job(self):
        from bot.views import Plumbot
        self._set(project_type='bathroom_renovation',
                  project_description='rip out and refit', customer_area='Budiriro')
        bot = Plumbot(self.lead.phone_number, tenant=self.lead.tenant)
        bot.appointment = self.lead
        asked = bot._get_first_pass_question('timeline')
        self.assertIn('bathroom renovation', asked)
        self.assertEqual(asked.count('?'), 1)


class PlanFarTimelineTests(TestCase):
    """A plan lead who wants the job months out.

    The flow stops asking, which is right, but 'nothing left to ask' is not the
    same as 'nothing left to say'. Without an explicit branch they fall into the
    retry machinery with a question that does not exist.
    """

    def setUp(self):
        self.lead = make_lead(8900, customer_name='Blessing', status='pending')
        self.lead.has_plan = True
        self.lead.plan_status = 'plan_uploaded'
        self.lead.project_description = 'full ensuite refit'
        self.lead.customer_area = 'Budiriro'
        self.lead.timeline = 'in about three months'
        self.lead.save()
        from bot.plan_quote import ensure_request
        self.row = ensure_request(self.lead)
        self.row.timeline_days = 91
        self.row.save(update_fields=['timeline_days'])
        self.lead.refresh_from_db()

    def _bot(self):
        from bot.views import Plumbot
        bot = Plumbot(self.lead.phone_number, tenant=self.lead.tenant)
        bot.appointment = self.lead
        return bot

    def test_the_flow_stops_asking(self):
        self.assertEqual(self._bot().get_next_question_to_ask(), 'complete')

    def test_it_asks_for_an_email_rather_than_going_vague(self):
        """Email is the only channel that outlives the 24h WhatsApp window, and
        their date is months away."""
        reply = self._bot()._plan_parked_reply()
        self.assertIn('email', reply.lower())
        self.assertEqual(reply.count('?'), 1)

    def test_it_does_not_ask_again_when_we_have_one(self):
        self.lead.customer_email = 'blessing@example.com'
        self.lead.save(update_fields=['customer_email'])
        reply = self._bot()._plan_parked_reply()
        self.assertNotIn('email', reply.lower())
        self.assertIn('check back', reply.lower())

    def test_it_never_mentions_a_quote(self):
        """They have not asked for one."""
        for email in ('', 'blessing@example.com'):
            self.lead.customer_email = email
            self.lead.save(update_fields=['customer_email'])
            self.assertNotIn('quot', self._bot()._plan_parked_reply().lower())

    def test_it_parks_them_on_the_delayed_sequence(self):
        """Otherwise nothing owns them: the booking flow has no question left
        and the nurture flow does not know they exist."""
        self._bot()._plan_parked_reply()
        self.lead.refresh_from_db()
        notes = (self.lead.internal_notes or '')
        self.assertTrue(
            self.lead.is_delayed or 'DELAY' in notes.upper(),
            msg=f'lead was not parked: is_delayed={self.lead.is_delayed!r}')

    def test_no_emoji_and_no_dash_punctuation(self):
        reply = self._bot()._plan_parked_reply()
        self.assertNotIn('—', reply)
        self.assertNotIn(' - ', reply)
        self.assertTrue(all(ord(c) < 0x2500 for c in reply))
