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
    def test_there_are_no_plumber_reminders(self, send, remind):
        """One email only (owner decision G2, 2026-09-23): the 2/4/8-hour
        "have you quoted?" chases are gone."""
        row = self._row()
        self._tick(now=row.plan_received_at + timedelta(hours=1, minutes=1))
        row.refresh_from_db()
        anchor = row.plumber_email_sent_at
        for hours in (2, 4, 8, 24):
            stats = self._tick(now=anchor + timedelta(hours=hours, minutes=1))
            self.assertEqual(stats['reminders'], 0, f'at +{hours}h')
        row.refresh_from_db()
        self.assertEqual(row.reminders_sent, 0)

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

    @patch('bot.plan_quote.in_email_window', return_value=True)
    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_lead_followup_waits_an_hour_after_the_plumber_confirms(self, send, _window):
        """An hour after "I've sent the quote", one quote check (decision H1).
        This lead is outside the free WhatsApp window, so it goes by email."""
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
        self.lead.refresh_from_db()
        self.assertTrue(any('just checking the quote we sent' in str(t.get('content', ''))
                            for t in self.lead.conversation_history or []))

    @patch('bot.plan_quote.in_email_window', return_value=True)
    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_no_quote_check_when_the_plumber_did_not_send_one(self, send, _window):
        from bot.plan_quote import apply_plumber_form
        row = self._row()
        self._tick(now=row.plan_received_at + timedelta(hours=1, minutes=1))
        row.refresh_from_db()
        apply_plumber_form(row, quote_sent=False)
        row.refresh_from_db()
        self.assertEqual(self._tick(
            now=row.plumber_form_completed_at + timedelta(hours=2))['lead_followups'], 0)

    @patch('bot.plan_quote.in_email_window', return_value=True)
    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_a_silent_plumber_means_no_guessed_quote_check(self, send, remind, _window):
        """The old +12h "assume the quote went out" branch is gone (decision
        H1): the ordinary follow-ups keep going instead, their second touch the
        plumber handoff (decision F)."""
        row = self._row()
        self._tick(now=row.plan_received_at + timedelta(hours=1, minutes=1))
        row.refresh_from_db()
        anchor = row.plumber_email_sent_at
        for hours in (12, 24, 48):
            self.assertEqual(
                self._tick(now=anchor + timedelta(hours=hours))['lead_followups'], 0)

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
        # 07:00 is the hour before the single 08:03-20:33 window opens (09:00
        # was asserted here while the day was two afternoon blocks).
        self.assertFalse(in_contact_window(self._at(3, 0)))
        self.assertFalse(in_contact_window(self._at(7, 0)))
        self.assertFalse(in_contact_window(self._at(23, 0)))

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_an_emailed_quote_check_waits_for_the_email_hours(self, send):
        """Outside the free WhatsApp window the check goes by email, and email
        waits for EMAIL_WINDOWS (12:30-13:30, 18:00-19:30)."""
        from bot.plan_quote import apply_plumber_form, run_plan_quote_tick
        self.row.plumber_email_sent_at = self._at(12, 0)
        self.row.save(update_fields=['plumber_email_sent_at'])
        apply_plumber_form(self.row, quote_sent=True, now=self._at(13, 0))
        self.assertEqual(run_plan_quote_tick(now=self._at(14, 1))['lead_followups'], 0)
        self.assertEqual(run_plan_quote_tick(now=self._at(18, 5))['lead_followups'], 1)

    def test_inside_the_free_window_it_goes_on_whatsapp_through_the_chain(self):
        """Inside the free window and contact hours: WhatsApp, through the
        outbound chain, logged with its WAMID."""
        from unittest.mock import MagicMock
        from bot.plan_quote import apply_plumber_form, run_plan_quote_tick
        self.row.plumber_email_sent_at = self._at(12, 0)
        self.row.save(update_fields=['plumber_email_sent_at'])
        apply_plumber_form(self.row, quote_sent=True, now=self._at(13, 0))
        client = MagicMock()
        client.send_text_message.return_value = {'messages': [{'id': 'wamid.X'}]}
        with patch('bot.whatsapp_window.may_send_proactively', return_value=True), \
             patch('bot.whatsapp_cloud_api.get_client_for_tenant', return_value=client), \
             patch('bot.whatsapp_webhook._finalised_for_send', side_effect=lambda t, *a, **k: t) as chain:
            self.assertEqual(run_plan_quote_tick(now=self._at(14, 1))['lead_followups'], 1)
        chain.assert_called_once()
        sent = client.send_text_message.call_args[0][1]
        self.assertIn('just checking the quote we sent', sent)
        self.assertTrue(sent.endswith('Any questions on it?'))

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

    DeepSeek is stubbed out for the whole class. These cases run the delay and
    timeframe handlers, which call the classifier, and this suite is the
    offline commit gate: a live call here made the run depend on the network
    and on the model agreeing with itself twice, which showed up as three
    intermittent errors. Returning None is the same thing the code sees when
    the API is down, so every classifier takes its deterministic fallback,
    which is the path being asserted anyway.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._ds = patch('bot.services.clients.deepseek_call', return_value=None)
        cls._ds.start()

    @classmethod
    def tearDownClass(cls):
        cls._ds.stop()
        super().tearDownClass()

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


class PlanQuoteEmailTests(TestCase):
    """The one plumber email for a plan lead (owner's plan sequence, 2026-09-23):
    the call-email layout with the plan, a pre-filled WhatsApp button, the
    missing fields asked in the script, and SLOW LEAD on a far-off timeline."""

    def setUp(self):
        from bot.plan_quote import ensure_request
        self.lead = make_lead(8610, customer_name='Rudo Moyo', status='pending',
                              project_description='ensuite, full redo')
        self.row = ensure_request(self.lead)

    def _send(self):
        from bot.plumber_notifications import send_plan_quote_email
        with patch('bot.plumber_notifications.send_email_to_recipients',
                   return_value=True) as send, \
             patch('bot.plumber_notifications.split_notification_recipients',
                   return_value=(['plumber@example.com'], [])):
            self.assertTrue(send_plan_quote_email(self.row))
        args, kwargs = send.call_args
        return args[1], args[2], kwargs['html_message']

    def test_script_first_asks_what_is_missing_and_has_the_whatsapp_button(self):
        from bot.call_brief import CALL_SUBJECT_PREFIX
        subject, text, html = self._send()
        self.assertTrue(subject.startswith('[Plan] Call'))
        self.assertFalse(subject.startswith(CALL_SUBJECT_PREFIX))   # no 24h chase
        self.assertLess(text.index('SCRIPT'), text.index('LEAD DETAILS'))
        self.assertIn('Whereabouts is the site?', text)           # area missing
        self.assertIn("I've sent the quote", html)
        self.assertNotIn('plumbing service', text)   # no type: "the plan" alone
        self.assertIn('Message them on WhatsApp', html)
        self.assertIn('wa.me/15550008610?text=', html)

    def test_a_far_off_timeline_is_labelled_slow_and_not_pushed(self):
        self.lead.customer_area = 'Budiriro'
        self.lead.timeline = 'in three months'
        self.lead.save()
        from bot.plan_quote import record_timeline
        self.row.timeline_days = 90
        self.row.save(update_fields=['timeline_days'])
        subject, text, _ = self._send()
        self.assertIn('SLOW LEAD', subject)
        self.assertIn('SLOW LEAD', text)
        self.assertNotIn('suit you better to get booked in', text)
        self.assertIn('Are you still looking at in three months to get started?', text)

    def test_the_instant_files_alert_is_held_while_the_plan_email_is_pending(self):
        from bot import whatsapp_webhook as wh
        with patch.object(wh, 'send_plumber_notification_email') as alert, \
             patch.object(wh, 'MEDIA_DEBOUNCE_SECONDS', 0):
            wh._schedule_plumber_alert('15550008610', self.lead, 'https://x/plan.pdf', 'document')
            import time
            time.sleep(0.3)
        alert.assert_not_called()

    def test_a_plan_lead_gets_the_plumber_handoff_as_its_second_follow_up(self):
        from bot.management.commands.send_followups import handoff_eligible
        self.lead.plan_status = 'plan_uploaded'
        self.lead.save(update_fields=['plan_status'])
        with patch.object(Appointment, 'plumber_contact', return_value='+263771111111'):
            self.assertTrue(handoff_eligible(self.lead))


class PlanLadderTests(TestCase):
    """A plan lead whose timeline is more than a week out joins the job-date
    sequence at the email and portfolio step, never re-asked the date (owner
    decision I, 2026-09-23)."""

    def test_a_far_timeline_arms_the_ladder_and_asks_for_the_email(self):
        from datetime import date
        from bot import job_date_ladder as ladder
        from bot.out_of_scope_handler import _read_pending, start_ladder_at_portfolio
        lead = make_lead(8611, customer_name='Rudo', status='pending')
        job = date.today() + timedelta(days=40)
        reply = start_ladder_at_portfolio(lead, job, source_message='in about 6 weeks')
        self.assertEqual(ladder.job_date(lead), job)
        self.assertNotIn('Will it be okay if we follow up', reply)
        self.assertIn('portfolio', reply)
        self.assertIn('email', reply.lower())
        pending = _read_pending(lead)
        self.assertEqual(pending['category'], 'delay_email')
        self.assertEqual(pending['original'], (job - timedelta(days=7)).isoformat())
