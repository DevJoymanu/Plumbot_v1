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


class LeadMessageDraftTests(PhoneQuoteBase):
    """The drafted WhatsApp message the plumber sends themselves."""

    def test_the_page_shows_what_we_have_and_what_is_missing(self):
        self.lead.customer_area = 'Borrowdale'
        self.lead.project_description = 'Geyser swap'
        self.lead.save(update_fields=['customer_area', 'project_description'])
        html = self.client.get(
            reverse('lead_whatsapp_handoff', args=[self.lead.pk])
        ).content.decode()
        self.assertIn('Borrowdale', html)
        self.assertIn('Geyser swap', html)
        self.assertIn('Timeline', html)          # still missing
        self.assertIn('wa.me/263771234567', html)

    def test_the_draft_collects_everything_in_one_send(self):
        # Owner decision 2026-09-07: one message, every outstanding answer,
        # numbered, each carrying the reason we want it.
        from bot.lead_handoff import build_message
        self.lead.project_description = 'Geyser swap'
        self.lead.save(update_fields=['project_description'])
        msg = build_message(self.lead)
        for want in ('area', 'hoping to get it done', 'email'):
            self.assertIn(want, msg.lower())

    def test_the_gaps_are_listed_in_the_flow_order(self):
        from bot.lead_handoff import build_message
        msg = build_message(self.lead)
        order = [msg.lower().index(x) for x in
                 ('what exactly you want done', "what area you're in",
                  'hoping to get it done', 'the best email')]
        self.assertEqual(order, sorted(order))

    def test_a_booked_lead_is_confirmed_not_re_qualified(self):
        from bot.lead_handoff import build_message
        self.lead.status = 'confirmed'
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=2)
        self.lead.save(update_fields=['status', 'scheduled_datetime'])
        msg = build_message(self.lead)
        self.assertIn('confirming', msg.lower())
        self.assertNotIn('looking to get done', msg)

    def test_the_draft_obeys_the_copy_rules(self):
        from bot.lead_handoff import build_message
        msg = build_message(self.lead)
        self.assertNotIn('—', msg)
        self.assertNotIn('–', msg)
        # No emoji anywhere in the customer-facing draft.
        self.assertTrue(all(ord(c) < 0x1F300 for c in msg))

    def test_it_signs_with_this_tenant_and_never_another(self):
        from bot.lead_handoff import build_message
        msg = build_message(self.lead)
        self.assertIn('Acme', msg)
        self.assertNotIn('Homebase', msg)

    def test_the_button_is_on_the_appointment_screen(self):
        html = self.client.get(
            reverse('appointment_detail', args=[self.lead.pk])
        ).content.decode()
        self.assertIn(reverse('lead_whatsapp_handoff', args=[self.lead.pk]), html)


def build_message_of(lead):
    from bot.lead_handoff import build_message
    return build_message(lead)


class LeadMessageRecapTests(PhoneQuoteBase):
    """The recap says back everything we hold; the ask is the next gap."""

    def _msg(self, **kw):
        from bot.lead_handoff import build_message
        for field, value in kw.items():
            setattr(self.lead, field, value)
        self.lead.save()
        return build_message(self.lead)

    def test_the_recap_carries_job_area_and_timeline_together(self):
        msg = self._msg(project_type='bathroom_renovation',
                        customer_area='Borrowdale', timeline='next week',
                        project_description='full ensuite')
        self.assertIn('full ensuite', msg)
        self.assertIn('Borrowdale', msg)
        self.assertIn('next week', msg)

    def test_their_own_words_beat_the_service_label(self):
        # "Bathroom Renovation" is the category we filed them under; their
        # description is the job, and the thing they can confirm or correct.
        msg = self._msg(project_type='bathroom_renovation',
                        project_description='full ensuite refit, new tub',
                        customer_area='Ruwa')
        self.assertIn("I've got you down for full ensuite refit, new tub "
                      'in Ruwa', msg)
        self.assertNotIn('a bathroom renovation', msg)

    def test_a_sentence_shaped_description_is_not_forced_into_the_frame(self):
        # 3 of the 256 descriptions on file read as sentences. Those cannot
        # follow "you down for", so the recap falls back rather than sending
        # "I have you down for we are redoing the whole upstairs bathroom".
        msg = self._msg(project_description=(
            'we are redoing the whole upstairs bathroom and moving the geyser'),
            customer_area='Ruwa')
        self.assertNotIn('down for we are', msg)
        self.assertIn("I've got you down as being in Ruwa", msg)
        # ...and it still does not ask them to describe what they described.
        self.assertNotIn('what exactly you want done', msg.lower())

    def test_an_ampersand_is_spoken_not_typed(self):
        msg = self._msg(project_description='bathroom & toilet mantainance',
                        customer_area='Ruwa')
        self.assertIn('bathroom and toilet mantainance', msg)
        self.assertNotIn('&', msg)

    def test_the_service_label_still_leads_when_there_is_no_description(self):
        msg = self._msg(project_type='bathroom_renovation',
                        customer_area='Ruwa', project_description='')
        self.assertIn("I've got you down for a bathroom renovation in Ruwa", msg)

    def test_absent_pieces_are_omitted_never_placeheld(self):
        msg = self._msg(project_type='geyser_repair')
        self.assertIn('a geyser repair', msg)
        for placeholder in ('None', 'Not given', 'your area', 'N/A'):
            self.assertNotIn(placeholder, msg)

    def test_a_plan_is_acknowledged_on_its_own(self):
        msg = self._msg(project_type='bathroom_renovation',
                        customer_area='Ruwa', timeline='in two weeks',
                        project_description='refit', plan_status='plan_uploaded')
        self.assertIn('plan', msg.lower())
        # ...and the email ask leans on it, because nothing needs measuring up.
        self.assertIn('price it off the plan', msg)

    def test_every_gap_is_named_not_just_the_next_one(self):
        msg = self._msg(project_description='geyser swap', timeline='next week')
        # Area AND email are both outstanding, so both are asked for at once.
        self.assertIn("what area you're in", msg.lower())
        self.assertIn('email', msg.lower())

    def test_the_ask_says_why_it_is_being_asked(self):
        msg = self._msg(project_description='geyser swap',
                        customer_area='Borrowdale', timeline='next month')
        self.assertIn('accurate quote', msg)

    def test_the_gaps_are_numbered_and_each_says_why(self):
        blank = Appointment.objects.create(
            phone_number='whatsapp:+263779999999', tenant=self.tenant)
        msg = build_message_of(blank)
        self.assertIn("I'll need a few more things:", msg)
        for n in ('1. ', '2. ', '3. ', '4. '):
            self.assertIn(n, msg)
        # The reason is the point of the format: a field name is a form, a
        # field name with a reason is a person explaining themselves.
        for why in ('so I know what to price', 'so I know if we cover you',
                    "so I can check we're free", 'so I can send the quote over'):
            self.assertIn(why, msg)
        # Never bullets, and never a dash list the stripper would rewrite.
        self.assertNotIn(chr(8226), msg)
        self.assertNotIn(chr(10) + '-', msg)

    def test_one_gap_is_a_sentence_not_a_list_of_one(self):
        msg = build_message_of(Appointment.objects.create(
            phone_number='whatsapp:+263778888888', tenant=self.tenant,
            project_description='refit', customer_area='Ruwa',
            timeline='next month', customer_name='Rudo'))
        self.assertIn('one more thing:', msg)
        self.assertNotIn('1. ', msg)

    def test_the_name_waits_for_the_booking(self):
        # In the owner's own takeovers the name comes last and on its own.
        # Stacked in with the quote details it is noise: nothing prices with it.
        msg = self._msg(project_type='bathroom_renovation')
        self.assertNotIn('name', msg.lower())
        # Alone, it is the booking question, and not framed as a quote need.
        msg = self._msg(project_description='refit', customer_area='Ruwa',
                        timeline='next month', customer_email='a@b.com',
                        customer_name='')
        self.assertIn('what name should I put it under', msg)
        self.assertNotIn('accurate quote', msg)

    def test_the_timeline_ask_says_why_it_matters(self):
        msg = self._msg(project_description='geyser swap',
                        customer_area='Borrowdale')
        self.assertIn('when you were hoping to get it done', msg.lower())
        self.assertIn('accurate quote', msg)

    def test_it_never_asks_what_it_just_said_it_knows(self):
        # Service known but no description: the ask must build on the recap,
        # not contradict it.
        msg = self._msg(project_type='bathroom_renovation')
        self.assertIn("I've got you down for a bathroom renovation", msg)
        # Not a flat "what exactly you want done" after saying we know it.
        self.assertNotIn('what exactly you want done', msg.lower())
        self.assertIn('what the bathroom renovation involves', msg.lower())

    def test_other_is_not_read_back_as_a_service(self):
        msg = self._msg(project_type='other', customer_area='Ruwa')
        self.assertNotIn('down for a other', msg)
        self.assertNotIn('Other', msg)

    def test_what_they_gave_us_is_acknowledged_before_more_is_asked(self):
        msg = self._msg(project_type='bathroom_renovation',
                        customer_area='Borrowdale')
        self.assertIn('Thanks for that.', msg)
        # The plan carries its own thanks, so it is never said twice.
        msg = self._msg(plan_status='plan_uploaded')
        self.assertIn('Thanks for sending the plan through.', msg)
        self.assertNotIn('Thanks for that.', msg)

    def test_nothing_on_file_means_no_hollow_thank_you(self):
        blank = Appointment.objects.create(
            phone_number='whatsapp:+263777777777', tenant=self.tenant)
        self.assertNotIn('Thanks', build_message_of(blank))

    def test_a_plan_is_not_asked_to_describe_itself(self):
        # The drawing IS what needs doing; asking again reads as though
        # nobody opened it.
        msg = self._msg(project_type='bathroom_renovation',
                        plan_status='plan_uploaded')
        self.assertIn('price it off the plan', msg)
        self.assertNotIn('what exactly needs doing', msg)


class TimelineCasingTests(PhoneQuoteBase):
    """The timeline is the customer's own wording, proper nouns included."""

    def test_proper_nouns_survive_the_timeline_clause(self):
        from bot.lead_handoff import _timeline_phrase
        out = _timeline_phrase('Back in Zimbabwe on the 22nd of December')
        self.assertIn('Zimbabwe', out)
        self.assertIn('December', out)
        self.assertTrue(out.startswith('looking to get it done back in'))

    def test_an_acronym_is_left_alone(self):
        from bot.lead_handoff import _timeline_phrase
        self.assertEqual(_timeline_phrase('ASAP'),
                         'looking to get it done as soon as possible')
        self.assertIn('NOW-ish', _timeline_phrase('NOW-ish please'))

    def test_a_month_name_keeps_its_capital(self):
        from bot.lead_handoff import _timeline_phrase
        self.assertIn('December 22nd', _timeline_phrase('December 22nd'))

    def test_an_ordinary_phrase_still_flows_lowercase(self):
        from bot.lead_handoff import _timeline_phrase
        self.assertEqual(_timeline_phrase('In two weeks'),
                         'looking to get it done in two weeks')


class PlumberBookingAlertTests(PhoneQuoteBase):
    """Sending the plumber the booking details by hand."""

    def setUp(self):
        super().setUp()
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=2)
        self.lead.save(update_fields=['scheduled_datetime'])

    def test_it_sends_and_stamps_when_it_was_sent(self):
        with patch('bot.views.plumbot.base.Plumbot') as P:
            P.return_value.extract_appointment_details.return_value = {}
            r = self.client.post(
                reverse('notify_plumber_of_booking', args=[self.lead.pk]))
        self.assertEqual(r.status_code, 302)
        self.lead.refresh_from_db()
        self.assertIsNotNone(self.lead.plumber_contacted_at)
        P.return_value.notify_team.assert_called_once()

    def test_it_works_on_a_booking_that_never_confirmed(self):
        # The case it exists for: slot on file, status still pending, so
        # book_appointment's automatic alert never ran.
        self.assertEqual(self.lead.status, 'pending')
        with patch('bot.views.plumbot.base.Plumbot') as P:
            P.return_value.extract_appointment_details.return_value = {}
            self.client.post(
                reverse('notify_plumber_of_booking', args=[self.lead.pk]))
        P.return_value.notify_team.assert_called_once()

    def test_it_may_be_sent_again(self):
        # A second press means the first did not arrive.
        self.lead.plumber_contacted_at = timezone.now()
        self.lead.save(update_fields=['plumber_contacted_at'])
        with patch('bot.views.plumbot.base.Plumbot') as P:
            P.return_value.extract_appointment_details.return_value = {}
            self.client.post(
                reverse('notify_plumber_of_booking', args=[self.lead.pk]))
        P.return_value.notify_team.assert_called_once()

    def test_no_slot_means_nothing_to_tell_them(self):
        self.lead.scheduled_datetime = None
        self.lead.save(update_fields=['scheduled_datetime'])
        with patch('bot.views.plumbot.base.Plumbot') as P:
            self.client.post(
                reverse('notify_plumber_of_booking', args=[self.lead.pk]))
        P.return_value.notify_team.assert_not_called()

    def test_the_button_is_on_the_page(self):
        html = self.client.get(
            reverse('appointment_detail', args=[self.lead.pk])
        ).content.decode()
        self.assertIn(reverse('notify_plumber_of_booking', args=[self.lead.pk]),
                      html)


class HalfMadeBookingTests(PhoneQuoteBase):
    """close_pleasantry must not end a conversation mid-booking."""

    def test_it_is_held_back_while_a_slot_is_unconfirmed(self):
        from bot.controller import decide_move
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=2)
        self.lead.project_description = 'refit'
        self.lead.previous_work_photos_sent_at = timezone.now()
        self.lead.save()
        uc = {'next_move': 'close_pleasantry', 'move_confidence': 0.9,
              'intent': 'ack', 'confidence': 'HIGH',
              'state_update': {'want_level': 'interested'}}
        self.assertEqual(self.lead.status, 'pending')
        self.assertIsNone(decide_move(uc, self.lead))

    def test_it_is_allowed_once_the_booking_is_confirmed(self):
        from bot.controller import decide_move
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=2)
        self.lead.status = 'confirmed'
        self.lead.project_description = 'refit'
        self.lead.previous_work_photos_sent_at = timezone.now()
        self.lead.save()
        uc = {'next_move': 'close_pleasantry', 'move_confidence': 0.9,
              'intent': 'ack', 'confidence': 'HIGH',
              'state_update': {'want_level': 'interested'}}
        self.assertEqual(decide_move(uc, self.lead), 'close_pleasantry')
