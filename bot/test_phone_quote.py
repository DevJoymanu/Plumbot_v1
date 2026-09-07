"""
Quoting over the phone: the form, the gate, and the one follow-up.

The third quote path, so most of what is asserted here is that it behaves like
the other two rather than inventing a third set of rules — single use, the
outcome radio as the gate, the same three-shape "when do they want it", and
lead state re-checked before the customer-facing send.
"""

from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Appointment, PhoneQuoteRequest, Tenant, TenantMembership
from .phone_quote import (
    apply_plumber_form,
    ensure_request,
    run_phone_quote_tick,
)


class PhoneQuoteBase(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name='Acme', slug='acme')
        self.user = User.objects.create_user(
            'plumber', 'plumber@example.com', 'pw', is_staff=True)
        TenantMembership.objects.create(
            user=self.user, tenant=self.tenant, role='staff')
        self.client = Client()
        self.client.force_login(self.user)
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+263771234567', tenant=self.tenant,
            customer_name='Tendai', status='pending')


class PhoneQuoteFormTests(PhoneQuoteBase):

    def test_the_button_opens_the_form_and_creates_one_row(self):
        r = self.client.get(
            reverse('phone_quote_start', args=[self.lead.pk]))
        self.assertEqual(r.status_code, 302)
        self.assertEqual(PhoneQuoteRequest.objects.count(), 1)
        row = PhoneQuoteRequest.objects.get()
        self.assertIn(str(row.token), r['Location'])
        # Opening it twice is the same row, or single-use means nothing.
        self.client.get(reverse('phone_quote_start', args=[self.lead.pk]))
        self.assertEqual(PhoneQuoteRequest.objects.count(), 1)

    def test_the_form_is_reachable_without_a_session(self):
        row = ensure_request(self.lead)
        anon = Client()
        r = anon.get(reverse('phone_quote_form', kwargs={'token': row.token}))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'Can you quote from the call?')

    def test_it_asks_all_four_things(self):
        row = ensure_request(self.lead)
        html = self.client.get(
            reverse('phone_quote_form', kwargs={'token': row.token})
        ).content.decode()
        for field in ('job_notes', 'lead_email', 'expectation', 'customer_area'):
            self.assertIn('name="%s"' % field, html)

    def test_quoting_captures_everything_and_lands_on_the_quote_screen(self):
        row = ensure_request(self.lead)
        r = self.client.post(
            reverse('phone_quote_form', kwargs={'token': row.token}),
            {'outcome': 'quoting', 'lead_email': 'tendai@example.com',
             'customer_area': 'Borrowdale', 'expectation': 'timeframe',
             'expected_timeframe': 'two_weeks',
             'job_notes': 'Swap the geyser, 150L.'})
        self.assertRedirects(
            r, reverse('create_quotation', kwargs={'pk': self.lead.pk}),
            fetch_redirect_response=False)
        row.refresh_from_db()
        self.lead.refresh_from_db()
        self.assertEqual(row.outcome, 'quoting')
        self.assertEqual(row.expected_timeframe, 'two_weeks')
        self.assertEqual(row.job_notes, 'Swap the geyser, 150L.')
        self.assertIsNotNone(row.form_completed_at)
        self.assertEqual(row.completed_by, self.user)
        # ...and it wrote through to the lead.
        self.assertEqual(self.lead.customer_email, 'tendai@example.com')
        self.assertEqual(self.lead.customer_area, 'Borrowdale')
        self.assertEqual(self.lead.project_description, 'Swap the geyser, 150L.')

    def test_a_typed_area_overwrites_one_the_flow_guessed(self):
        # The extraction prompt's example suburb reached 221 real leads as
        # though it were fact. An area a human asked for on a call wins.
        self.lead.customer_area = 'Avondale'
        self.lead.save(update_fields=['customer_area'])
        row = ensure_request(self.lead)
        self.client.post(
            reverse('phone_quote_form', kwargs={'token': row.token}),
            {'outcome': 'quoting', 'lead_email': 'a@b.com',
             'customer_area': 'Chitungwiza', 'expectation': 'unknown'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.customer_area, 'Chitungwiza')

    def test_the_form_is_single_use(self):
        row = ensure_request(self.lead)
        url = reverse('phone_quote_form', kwargs={'token': row.token})
        self.client.post(url, {'outcome': 'quoting', 'lead_email': 'a@b.com',
                               'expectation': 'unknown'})
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'already', status_code=200)
        # A second post cannot overwrite what the first one said.
        self.client.post(url, {'outcome': 'not_proceeding'})
        row.refresh_from_db()
        self.assertEqual(row.outcome, 'quoting')

    def test_quoting_needs_an_email(self):
        row = ensure_request(self.lead)
        r = self.client.post(
            reverse('phone_quote_form', kwargs={'token': row.token}),
            {'outcome': 'quoting', 'expectation': 'unknown'})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, 'email')
        row.refresh_from_db()
        self.assertTrue(row.is_open)

    def test_a_date_needs_an_actual_date(self):
        row = ensure_request(self.lead)
        r = self.client.post(
            reverse('phone_quote_form', kwargs={'token': row.token}),
            {'outcome': 'quoting', 'lead_email': 'a@b.com',
             'expectation': 'specific_date', 'expected_date': 'not-a-date'})
        self.assertEqual(r.status_code, 200)
        row.refresh_from_db()
        self.assertTrue(row.is_open)


class PhoneQuoteOtherOutcomeTests(PhoneQuoteBase):
    """The outcome radio is the gate, exactly as on the other two forms."""

    def test_needs_visit_raises_no_quote_and_notes_the_fallback(self):
        row = ensure_request(self.lead)
        r = self.client.post(
            reverse('phone_quote_form', kwargs={'token': row.token}),
            {'outcome': 'needs_visit'})
        self.assertEqual(r.status_code, 200)
        row.refresh_from_db()
        self.lead.refresh_from_db()
        self.assertEqual(row.outcome, 'needs_visit')
        self.assertFalse(row.is_open)
        self.assertIn('site visit', (self.lead.admin_notes or '').lower())
        # No follow-up: only 'quoting' rows are ever picked up.
        self.assertEqual(run_phone_quote_tick()['lead_followups'], 0)

    def test_not_proceeding_deactivates_the_lead(self):
        row = ensure_request(self.lead)
        self.client.post(
            reverse('phone_quote_form', kwargs={'token': row.token}),
            {'outcome': 'not_proceeding'})
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.is_lead_active)


class PhoneQuoteFollowupTests(PhoneQuoteBase):

    def _completed(self, **kw):
        row = ensure_request(self.lead)
        apply_plumber_form(row, outcome='quoting', lead_email='a@b.com',
                           expectation='unknown', **kw)
        return row

    def test_nothing_goes_out_before_the_hour_is_up(self):
        self._completed()
        self.assertEqual(run_phone_quote_tick()['lead_followups'], 0)

    def test_the_lead_is_asked_an_hour_later(self):
        row = self._completed()
        later = timezone.now() + timedelta(hours=2)
        stats = run_phone_quote_tick(now=later)
        self.assertEqual(stats['lead_followups'], 1)
        row.refresh_from_db()
        self.assertIsNotNone(row.lead_followup_sent_at)
        self.assertEqual(row.quote_status, 'assumed')

    def test_it_is_asked_once_however_often_the_cron_runs(self):
        self._completed()
        later = timezone.now() + timedelta(hours=2)
        run_phone_quote_tick(now=later)
        self.assertEqual(run_phone_quote_tick(now=later)['lead_followups'], 0)

    def test_a_lead_who_booked_in_the_meantime_is_left_alone(self):
        self._completed()
        self.lead.status = 'confirmed'
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=2)
        self.lead.save(update_fields=['status', 'scheduled_datetime'])
        later = timezone.now() + timedelta(hours=2)
        stats = run_phone_quote_tick(now=later)
        self.assertEqual(stats['lead_followups'], 0)
        self.assertEqual(stats['skipped'], 1)

    def test_a_dry_run_writes_nothing(self):
        row = self._completed()
        later = timezone.now() + timedelta(hours=2)
        self.assertEqual(
            run_phone_quote_tick(now=later, dry_run=True)['lead_followups'], 1)
        row.refresh_from_db()
        self.assertIsNone(row.lead_followup_sent_at)

    @patch('bot.post_visit.record_lead_expected_date')
    def test_a_named_date_goes_to_the_shared_resolver(self, rec):
        # All three quote paths must agree on what a date means.
        row = ensure_request(self.lead)
        day = (timezone.localdate() + timedelta(days=10))
        apply_plumber_form(row, outcome='quoting', lead_email='a@b.com',
                           expectation='specific_date', expected_date=day)
        rec.assert_called_once()


class PhoneQuoteButtonTests(PhoneQuoteBase):

    def test_the_button_sits_beside_log_the_visit(self):
        html = self.client.get(
            reverse('appointment_detail', args=[self.lead.pk])
        ).content.decode()
        marker = '<div class="visit-prompt-actions">'
        self.assertIn(marker, html)
        group = html.split(marker, 1)[1].split('</div>', 1)[0]
        self.assertIn(reverse('site_visit_start', args=[self.lead.pk]), group)
        self.assertIn(reverse('phone_quote_start', args=[self.lead.pk]), group)
        self.assertIn('Log the visit', group)
        self.assertIn('Quote on the phone', group)
