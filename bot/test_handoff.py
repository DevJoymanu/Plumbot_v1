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
from io import StringIO
from unittest.mock import patch

from django.test import TestCase, override_settings
from django.utils import timezone

from bot import job_date_ladder as ladder
from bot.models import Appointment
from bot.plumber_link import LINK_SENT_TAG
from bot.test_views_actions import open_email_window

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
        # The handoff goes with the portfolio: the pre-filled link AND the
        # number, then (no email on this lead) the call question last.
        self.assertIn(PLUMBER_WA, reply)
        self.assertIn('+' + PLUMBER_WA.split('wa.me/')[1], reply)
        self.assertTrue(reply.endswith("you've got the help you need?"))

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


class SecondFollowupHandoffTests(OfflineTestCase):
    """Owner rule, 2026-09-21: the SECOND automatic touch of a silence is the
    plumber handoff, then nothing until the lead replies, and a reply resets
    the count. Two groups: a lead who gave a delay signal (the delay and
    parked nudge loops, no field requirements) and a lead with all three
    fields (the main loop, pinned in TEST 0)."""

    def setUp(self):
        super().setUp()
        from bot.management.commands.send_followups import Command, SA_TIMEZONE
        self.cmd = Command()
        self.t0 = SA_TIMEZONE.localize(datetime(2030, 3, 12, 6, 0))
        # These leads are not free-entry leads, and every loop refuses a send
        # Meta would charge for. That gate is not what is under test here.
        patcher = patch('bot.management.commands.send_followups.paid_sends_allowed',
                        return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _lead(self, n, notes, **kw):
        lead = make_lead(n, internal_notes=notes, **kw)
        self._replied(lead, self.t0)
        return lead

    def _replied(self, lead, when):
        lead.last_inbound_at = lead.last_customer_response = when
        lead.save(update_fields=['last_inbound_at', 'last_customer_response'])

    def _tick(self, loop, lead, hours):
        """Run one loop at t0 + hours; return the text sent, or None."""
        from bot.management.commands import send_followups as sf
        client = sf.get_client_for_tenant.return_value
        client.send_text_message.reset_mock()
        now = self.t0 + timedelta(hours=hours)
        with patch('django.utils.timezone.now', return_value=now):
            getattr(self.cmd, loop)(now, False)
        lead.refresh_from_db()
        call = client.send_text_message.call_args
        return call[0][1] if call else None

    def test_delay_signal_no_email_no_fields_second_nudge_is_the_handoff(self):
        """Lead 1217's shape: delay signal, email asked, never given, and the
        description a stray reply. Nudge 1 is the email ask; nudge 2 the link."""
        lead = self._lead(60, '[DELAY_SIGNAL]\n[OOS_PENDING] category=delay_email original=2030-03-26',
                          is_delayed=True, customer_area='', project_type=None,
                          project_description='Ok\nNow you are talking')
        first = self._tick('_nudge_delay_flow_ghosts', lead, 2.5)
        self.assertIn('email', first.lower())
        self.assertNotIn('wa.me', first)
        self.assertTrue(first.startswith('Hi Rudo, one thing'), first)  # no stray capital
        second = self._tick('_nudge_delay_flow_ghosts', lead, 8)
        self.assertIn(PLUMBER_WA, second)
        self.assertTrue(second.startswith("I've messaged a couple of times"), second)
        # ...and the plumber is NOT emailed for it (owner, 2026-09-21: not an
        # email for every handoff sent).
        from bot.plumber_notifications import send_email_to_recipients
        send_email_to_recipients.assert_not_called()
        self.assertNotIn('Now you are talking', second)
        # The handoff was the last touch of this silence.
        self.assertIsNone(self._tick('_nudge_delay_flow_ghosts', lead, 14))
        self.assertIsNone(self._tick('_nudge_delay_flow_ghosts', lead, 20))

    def test_a_reply_resets_the_delay_nudges(self):
        lead = self._lead(61, '[DELAY_SIGNAL]\n[OOS_PENDING] category=delay_timeframe original=',
                          is_delayed=True)
        self._tick('_nudge_delay_flow_ghosts', lead, 2.5)
        self.assertIn(PLUMBER_WA, self._tick('_nudge_delay_flow_ghosts', lead, 8))
        self._replied(lead, self.t0 + timedelta(hours=9))
        # A new silence: nudge 1 again (contextual), not "stopped" and not #3.
        again = self._tick('_nudge_delay_flow_ghosts', lead, 11.5)
        self.assertIsNotNone(again)
        self.assertNotIn('wa.me', again)
        self.assertIn(PLUMBER_WA, self._tick('_nudge_delay_flow_ghosts', lead, 17))

    def test_a_parked_lead_second_nudge_is_the_handoff_then_stops(self):
        lead = self._lead(62, '[PARKED]')
        first = self._tick('_nudge_parked_leads', lead, 9)
        self.assertIsNotNone(first)
        self.assertNotIn('wa.me', first)
        self.assertIn(PLUMBER_WA, self._tick('_nudge_parked_leads', lead, 13.5))
        self.assertIsNone(self._tick('_nudge_parked_leads', lead, 18))

    def test_the_main_loop_stops_after_the_handoff_until_they_reply(self):
        from bot.management.commands.send_followups import SA_TIMEZONE
        lead = self._lead(63, '')
        link_touch = f'[AUTO FOLLOW-UP] Hi there https://{PLUMBER_WA}?text=x'
        lead.conversation_history = [{'role': 'assistant', 'content': link_touch,
                                      'timestamp': (self.t0 + timedelta(hours=4)).isoformat()}]
        lead.followup_count = 2
        lead.save(update_fields=['conversation_history', 'followup_count'])
        now = self.t0 + timedelta(hours=12)
        with patch('django.utils.timezone.now', return_value=now):
            ready, why = self.cmd._is_ready_for_followup(lead, now.astimezone(SA_TIMEZONE), False)
            self.assertIsNone(self.cmd.next_followup_due_at(lead))
        self.assertFalse(ready)
        self.assertIn('handed off', why)


class DescriptionNetTests(OfflineTestCase):
    """Appointment.save never stores chat as the job, whichever path set it
    (bot/job_text.py). Lead 1217's description was "Ok\\nNow you are talking"."""

    def test_chat_is_not_stored_as_the_job(self):
        lead = make_lead(80, project_description='Ok\nNow you are talking')
        lead.refresh_from_db()
        self.assertIsNone(lead.project_description)

    def test_a_write_of_the_field_alone_is_caught_too(self):
        lead = make_lead(81)
        lead.project_description = 'Ok thank ,I will let you'
        lead.save(update_fields=['project_description'])
        lead.refresh_from_db()
        self.assertIsNone(lead.project_description)

    def test_a_real_description_is_kept(self):
        lead = make_lead(82, project_description='Kuita install copper pipes, 2 showers')
        lead.refresh_from_db()
        self.assertEqual(lead.project_description, 'Kuita install copper pipes, 2 showers')

    def test_a_save_of_other_fields_leaves_the_description_alone(self):
        """The net only acts on a save that writes the field, so saving other
        columns never changes what the caller holds in memory."""
        lead = make_lead(83)
        Appointment.objects.filter(pk=lead.pk).update(project_description='ok')
        lead.refresh_from_db()
        lead.customer_area = 'Ruwa'
        lead.save(update_fields=['customer_area'])
        self.assertEqual(lead.project_description, 'ok')

    def test_the_raw_message_fallback_does_not_take_a_reaction(self):
        """The path that stored it: next question is the description, and the
        batched reply is taken raw when the gate says it looks like one."""
        from bot.views.plumbot.response_mixin import ResponseMixin
        gate = ResponseMixin._looks_like_project_description_reply
        self.assertFalse(gate(ResponseMixin(), 'Ok\nNow you are talking'))


@override_settings(ALLOWED_HOSTS=['testserver', 'wa.homebase.test', 'wa.other.test'])
class ShortLinkTests(OfflineTestCase):
    """The business-domain short link (bot/short_links.py): short in the
    message, the per-lead wa.me link when tapped, and only on the lead's own
    tenant's domain."""

    def setUp(self):
        super().setUp()
        from unittest.mock import PropertyMock
        patcher = patch('bot.tenant_config.TenantConfig.short_link_domain',
                        new_callable=PropertyMock, return_value='wa.homebase.test')
        self.domain = patcher.start()
        self.addCleanup(patcher.stop)
        self.lead = make_lead(90, customer_area='Arlington East')

    def test_the_handoff_shows_the_short_link_and_no_apology(self):
        from bot.plumber_link import handoff_message
        from bot.short_links import encode
        msg = handoff_message(self.lead)
        self.assertIn(f'https://wa.homebase.test/q/{encode(self.lead.pk)}', msg)
        self.assertNotIn('?text=', msg)
        # Short, so not called long; it forwards to the per-lead link, so it
        # still carries their details.
        self.assertNotIn('so long', msg)
        self.assertIn('with your details already typed in', msg)
        self.assertTrue(msg.rstrip().endswith("number: +263774819901"), msg)

    def test_tapping_it_forwards_to_the_per_lead_wa_me_link(self):
        from bot.short_links import encode
        resp = self.client.get(f'/q/{encode(self.lead.pk)}', HTTP_HOST='wa.homebase.test')
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp['Location'].startswith('https://wa.me/263774819901?text='))
        self.assertIn('Arlington%20East', resp['Location'])
        self.assertEqual(resp['Cache-Control'], 'no-store')

    def test_another_domain_or_a_forged_code_is_a_404(self):
        from bot.short_links import encode
        code = encode(self.lead.pk)
        self.assertEqual(self.client.get(f'/q/{code}', HTTP_HOST='wa.other.test').status_code, 404)
        forged = code[:-1] + ('a' if code[-1] != 'a' else 'b')
        self.assertEqual(self.client.get(f'/q/{forged}', HTTP_HOST='wa.homebase.test').status_code, 404)
        self.assertEqual(self.client.get('/q/zz', HTTP_HOST='wa.homebase.test').status_code, 404)

    def test_a_handoff_with_the_short_link_still_stops_the_run(self):
        """The stop-after-handoff check used to look for wa.me only, and the
        business-domain link has none."""
        from bot.management.commands.send_followups import handoff_sent_since_last_reply
        from bot.plumber_link import handoff_message
        self.lead.last_customer_response = timezone.now() - timedelta(hours=5)
        self.lead.conversation_history = [{
            'role': 'assistant', 'content': f'[AUTO FOLLOW-UP] {handoff_message(self.lead)}',
            'timestamp': timezone.now().isoformat()}]
        self.assertTrue(handoff_sent_since_last_reply(self.lead))


class LinkQuestionTests(OfflineTestCase):
    """A lead wary of the plumber link gets the explanation, then the
    plumber's contact card, and the card never goes without the text."""

    def setUp(self):
        super().setUp()
        self.lead = make_lead(95)
        self.reply = 'Fair question. That link just opens a WhatsApp chat.'
        self.lead.add_conversation_message('assistant', self.reply)

    def _run(self, text_sent):
        from bot import whatsapp_webhook as wh

        def fake_delayed(sender, reply, *a, **k):
            if text_sent:
                Appointment.objects.get(pk=self.lead.pk).mark_message_sent(
                    'assistant', reply, 'wamid.TEXT')
        client = patch('bot.whatsapp_cloud_api.get_client_for_tenant').start()
        self.addCleanup(patch.stopall)
        client.return_value.send_contact_card.return_value = {'messages': [{'id': 'wamid.CARD'}]}
        with patch.object(wh, 'delayed_response', fake_delayed), \
                patch.object(wh.time, 'sleep'):
            wh._send_reply_then_contact_card('15550010095', self.reply, 0,
                                             appointment_pk=self.lead.pk)
        return client.return_value.send_contact_card

    def test_the_card_follows_the_text_and_is_recorded(self):
        card = self._run(text_sent=True)
        card.assert_called_once()
        sender, name, number = card.call_args[0][:3]
        self.assertEqual(number, '263774819901')
        self.lead.refresh_from_db()
        last = self.lead.conversation_history[-1]
        self.assertTrue(last['content'].startswith('[CONTACT CARD]'))
        self.assertIn('wamid.CARD', last.get('media_index', {}))

    def test_no_card_when_the_text_was_not_sent(self):
        self._run(text_sent=False).assert_not_called()

    def test_the_card_payload_carries_wa_id_so_whatsapp_shows_message(self):
        from bot.whatsapp_cloud_api import WhatsAppCloudAPI
        api = WhatsAppCloudAPI.__new__(WhatsAppCloudAPI)
        api.base_url, api.phone_number_id = 'https://graph.example', '123'
        with patch.object(WhatsAppCloudAPI, '_post_with_retry') as post:
            post.return_value.json.return_value = {'messages': [{'id': 'w'}]}
            api.send_contact_card('263786318169', 'Takudzwa', '+263 77 481 9901',
                                  organization='Homebase Plumbers')
        payload = post.call_args[0][1]
        self.assertEqual(payload['type'], 'contacts')
        phone = payload['contacts'][0]['phones'][0]
        self.assertEqual((phone['phone'], phone['wa_id']), ('+263774819901', '263774819901'))
        self.assertEqual(payload['contacts'][0]['org']['company'], 'Homebase Plumbers')

    def test_a_test_console_number_never_reaches_whatsapp(self):
        from bot.whatsapp_cloud_api import WhatsAppCloudAPI
        api = WhatsAppCloudAPI.__new__(WhatsAppCloudAPI)
        with patch.object(WhatsAppCloudAPI, '_post_with_retry') as post:
            result = api.send_contact_card('999000000001', 'Takudzwa', '263774819901')
        post.assert_not_called()
        self.assertTrue(result['messages'][0]['id'])


class TwoFollowupsAndSilenceTests(OfflineTestCase):
    """Owner rule, 2026-09-21: a lead who gets the plumber handoff gets TWO
    follow-ups (contextual, then the handoff) and nothing after it, from any
    loop, until they reply."""

    def _handoff_history(self, lead, hours_after_reply=5):
        from bot.plumber_link import handoff_message
        at = lead.last_inbound_at + timedelta(hours=hours_after_reply)
        lead.conversation_history = [{
            'role': 'assistant', 'content': f'[AUTO FOLLOW-UP] {handoff_message(lead)}',
            'timestamp': at.isoformat()}]
        lead.save(update_fields=['conversation_history'])

    def test_a_handoff_lead_has_two_follow_ups_not_four(self):
        from bot.management.commands.send_followups import max_followups_for
        now = timezone.now()
        qualified = make_lead(100, last_inbound_at=now, last_customer_response=now)
        missing_area = make_lead(101, customer_area='', last_inbound_at=now,
                                 last_customer_response=now)
        self.assertEqual(max_followups_for(qualified), 2)
        self.assertGreater(max_followups_for(missing_area), 2)

    @patch('bot.customer_emails._send', return_value=True)
    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
    def test_no_job_date_touch_after_a_handoff_but_the_plumber_is_still_told(self, plumber, send):
        from bot.management.commands.send_followups import Command, SA_TIMEZONE
        job = date(2030, 3, 20)
        lead = make_lead(102, customer_email='rudo@example.com',
                         last_inbound_at=SA_TIMEZONE.localize(datetime(2030, 3, 1, 9)))
        ladder.arm(lead, job)
        self._handoff_history(lead)
        cmd = Command()
        for day in (13, 17, 18):
            now = SA_TIMEZONE.localize(datetime(2030, 3, day, 10))
            with patch('bot.management.commands.send_followups.timezone.now', return_value=now):
                cmd._tick_job_ladder(lead, now, False, ladder)
            lead.refresh_from_db()
        send.assert_not_called()                       # no text to the lead
        plumber.assert_called_once()                   # the call brief still goes

    @patch('bot.customer_emails.send_delay_followup_email', return_value=True)
    def test_no_check_back_after_a_handoff(self, followup):
        from bot.management.commands.send_followups import Command
        lead = make_lead(103, is_delayed=True, customer_email='rudo@example.com',
                         last_inbound_at=timezone.now() - timedelta(days=3),
                         delay_followup_due_at=timezone.now() - timedelta(hours=1))
        self._handoff_history(lead)
        Command()._process_delayed_reactivations(timezone.now(), False)
        followup.assert_not_called()

    def test_a_scripted_reply_that_repeats_is_read_in_context(self):
        """finalise_outbound turns the model reader on for a check=False draft
        that repeats a recent sent message; a first ask keeps it off."""
        from bot.whatsapp_webhook import finalise_outbound
        said = "Have a look whenever suits, and if anything changes just send a message."
        lead = make_lead(104)
        with patch('bot.response_check.verify_and_refine',
                   side_effect=lambda r, a, m=None: (r, None)) as reader:
            finalise_outbound(said, lead, 'ok', check=False)
            reader.assert_not_called()                 # first time: scripted as is
            lead.conversation_history = [{'role': 'assistant', 'content': said,
                                          'sent_at': timezone.now().isoformat()}]
            finalise_outbound(said, lead, 'send it here', check=False)
            reader.assert_called_once()                # again: read in context

    def test_a_deferred_lead_keeps_counting_as_deferred_after_replying(self):
        """The delay tag is cleared on every inbound; the agreed date is not."""
        from bot.out_of_scope_handler import has_agreed_checkback
        lead = make_lead(105, delay_followup_due_at=timezone.now() + timedelta(days=20))
        self.assertTrue(has_agreed_checkback(lead))
        self.assertFalse(has_agreed_checkback(make_lead(106)))


class AgreedCheckbackHoldsFollowupsTests(SecondFollowupHandoffTests):
    """Homebase 1233, 2026-09-21: deferred to "mid next month" (check back
    Thursday 15 October), took the portfolio on WhatsApp, said "Thank you
    ndaiwona", and at 18:05 got a portfolio check-in AND an [AUTO FOLLOW-UP]
    asking "What exactly needs doing?" four seconds apart. The check-in had
    overwritten delay_followup_due_at with its own moment, so once it went out
    the main loop no longer saw the agreed date, and neither loop could see the
    other's send. Inherits the frozen-clock helpers; the parent's tests are
    switched off below so they run once, in their own class."""

    def test_delay_signal_no_email_no_fields_second_nudge_is_the_handoff(self): pass
    def test_a_reply_resets_the_delay_nudges(self): pass
    def test_a_parked_lead_second_nudge_is_the_handoff_then_stops(self): pass
    def test_the_main_loop_stops_after_the_handoff_until_they_reply(self): pass

    def _deferred(self, n, replied_after_pdf=True):
        """1233's state at 18:05: agreed 15 days out, the portfolio check-in
        due, the portfolio sent 5 min before their last message."""
        from bot.management.commands.send_followups import SA_TIMEZONE
        agreed = (self.t0 + timedelta(days=15)).date().isoformat()
        pdf_at = self.t0 - timedelta(minutes=5)
        lead = self._lead(n, f'[FOLLOW_UP_DATE] {agreed}\n[DELAY_SIGNAL]\n'
                             f'[DELAY_KIND] pdf_checkin\n[LEAD_MAGNET_WA_SENT]\n'
                             f'[PDF_CHECKIN_FROM] {pdf_at.isoformat()}',
                          is_delayed=True, project_description='',
                          delay_followup_due_at=self.t0 + timedelta(hours=10))
        if not replied_after_pdf:
            self._replied(lead, pdf_at - timedelta(minutes=5))
        return lead

    def test_a_lead_who_replied_after_the_portfolio_gets_no_check_in(self):
        lead = self._deferred(120)
        self.assertIsNone(self._tick('_process_delayed_reactivations', lead, 11))
        self.assertFalse(lead.is_delayed)                # retired, not retried
        self.assertNotIn('pdf_checkin', lead.internal_notes)

    def test_a_lead_silent_since_the_portfolio_still_gets_the_check_in(self):
        lead = self._deferred(121, replied_after_pdf=False)
        sent = self._tick('_process_delayed_reactivations', lead, 11)
        self.assertIn('portfolio', sent)

    def test_the_main_loop_waits_for_the_agreed_date(self):
        from bot.management.commands.send_followups import SA_TIMEZONE
        lead = self._deferred(122)
        self._tick('_process_delayed_reactivations', lead, 11)
        # The column is in the past now; the agreed date in the notes is not.
        for hours in (11, 30, 24 * 7):
            now = self.t0 + timedelta(hours=hours)
            with patch('django.utils.timezone.now', return_value=now):
                ready, why = self.cmd._is_ready_for_followup(
                    lead, now.astimezone(SA_TIMEZONE), False)
                self.assertIsNone(self.cmd.next_followup_due_at(lead))
            self.assertFalse(ready)
            self.assertIn('check-back', why)

    def test_one_loops_send_blocks_another_loops_send_in_the_same_run(self):
        """A lead with no agreed date: a check-in just went out, so the main
        loop's quiet-after-outbound gap applies to it (the cron never stamps
        last_outbound_at; the transcript marker is what it reads)."""
        from bot.management.commands.send_followups import SA_TIMEZONE, last_proactive_at
        lead = self._lead(123, '')
        at = self.t0 + timedelta(hours=11)
        lead.conversation_history = [{'role': 'assistant',
                                      'content': '[DELAY PORTFOLIO CHECK-IN] Hi there',
                                      'timestamp': at.isoformat()}]
        lead.save(update_fields=['conversation_history'])
        self.assertEqual(last_proactive_at(lead), at)
        now = at + timedelta(seconds=4)
        with patch('django.utils.timezone.now', return_value=now):
            ready, why = self.cmd._is_ready_for_followup(
                lead, now.astimezone(SA_TIMEZONE), False)
        self.assertFalse(ready)
        self.assertIn('since we last messaged', why)

    def test_the_agreed_date_itself_is_not_held(self):
        """On the day, send_reminders owns the check-back; a date already
        passed is no promise any more."""
        from bot.out_of_scope_handler import has_agreed_checkback
        today = timezone.now().date()
        self.assertFalse(has_agreed_checkback(make_lead(124, internal_notes=f'[FOLLOW_UP_DATE] {today}')))
        self.assertFalse(has_agreed_checkback(make_lead(
            125, internal_notes=f'[FOLLOW_UP_DATE] {today - timedelta(days=3)}')))
        self.assertTrue(has_agreed_checkback(make_lead(
            126, internal_notes=f'[FOLLOW_UP_DATE] {today + timedelta(days=3)}')))


class JobLadderCronTests(OfflineTestCase):
    """send_followups walking the ladder, on a frozen clock."""

    JOB = date(2030, 3, 20)          # -7 = 13th, -3 = 17th, -2 = 18th

    def setUp(self):
        super().setUp()
        open_email_window(self)
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

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
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
        subject, body = plumber.call_args[0][1:3]    # (recipients, subject, text)
        self.assertTrue(subject.startswith('Please call today: '))
        self.assertIn('(no quote sent yet)', body)   # no quote on this lead
        self.assertEqual(ladder.step(self.lead), ladder.STEP_DONE)
        self.assertFalse(self.lead.is_delayed)

        self._tick(19)                               # finished: nothing more
        plumber.assert_called_once()

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
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

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
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

    @patch('bot.plumber_notifications.send_email_to_recipients', return_value=True)
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


class LadderEmailCopyTests(OfflineTestCase):
    """The job-date ladder emails carry the owner's copy (2026-09-22): what
    the lead told us about their date, a free online quote, and a Get quote
    button above WhatsApp and Call."""

    def _lead(self, **kw):
        kw.setdefault('customer_name', 'Jane')
        kw.setdefault('project_description', 'Full re-tile of the main bathroom')
        lead = Appointment.objects.create(phone_number='whatsapp:+15550007400', **kw)
        ladder.arm(lead, date(2026, 10, 21))
        return lead

    def test_first_email_says_back_their_date_and_offers_a_free_quote(self):
        subject, html = ladder.touch_email(self._lead(), ladder.STEP_FIRST)
        self.assertEqual(subject, 'Ahead of the 21st of October')
        self.assertIn("you mentioned you&#x27;d be going ahead with the", html)
        self.assertIn('around the 21st of October', html)
        self.assertIn('free quote', html)
        self.assertIn('Hi Jane', html)

    def test_get_quote_button_sits_above_whatsapp_and_call(self):
        lead = self._lead()
        with patch.object(Appointment, 'plumber_contact', return_value='+263771111111'):
            _, html = ladder.touch_email(lead, ladder.STEP_FIRST)
        self.assertIn('>Get quote</a>', html)
        self.assertIn('wa.me/263771111111', html)
        self.assertLess(html.index('Get quote'), html.index('Call us'))

    def test_second_email_asks_if_the_timing_moved(self):
        _, html = ladder.touch_email(self._lead(), ladder.STEP_SECOND)
        self.assertIn('or has the timing moved?', html)
        self.assertIn('free quote', html)

    def test_the_copy_has_no_dash_emoji_or_plumber(self):
        from bot.copy_catalog import LADDER_EMAIL_FIRST, LADDER_EMAIL_SECOND
        for text in (LADDER_EMAIL_FIRST, LADDER_EMAIL_SECOND):
            self.assertNotIn(' - ', text)
            self.assertNotIn('\u2014', text)
            self.assertNotIn('the plumber', text.lower())


class LadderCallBriefTests(OfflineTestCase):
    """The job - 2 call email is the owner's call-email layout (2026-09-22):
    script first, only true facts in the intro, the visit priced by the lead's
    own tenant, a real Log the call link, and the 24-hour reminder's prefix."""

    def _lead(self, **kw):
        kw.setdefault('customer_name', 'Rudo Moyo')
        kw.setdefault('project_description', 'Full re-tile and new fittings')
        lead = Appointment.objects.create(phone_number='whatsapp:+263770007401', **kw)
        ladder.arm(lead, timezone.localdate() + timedelta(days=2))
        return lead

    def test_a_lead_we_could_not_reach_is_described_truthfully(self):
        subject, text, html = ladder.call_brief(self._lead())
        self.assertTrue(subject.startswith('Please call today: Rudo Moyo'))
        self.assertIn('no email on file', text)
        self.assertIn('your call is the first contact since then', text)
        self.assertNotIn('check-ins', text)
        self.assertLess(text.index('SCRIPT'), text.index('LEAD DETAILS'))
        self.assertIn('Log the call', html)
        self.assertNotIn('#log-the-call', html)

    def test_sent_touches_are_counted_from_the_transcript(self):
        lead = self._lead(customer_email='rudo@example.com')
        lead.add_conversation_message('assistant', f'{ladder.TRANSCRIPT_MARKER} (email) Ahead of')
        _, text, _ = ladder.call_brief(lead)
        self.assertIn('We checked in once since by email', text)

    def test_a_tenant_that_charges_a_call_out_is_never_promised_a_free_visit(self):
        from unittest.mock import MagicMock
        cfg = MagicMock(currency='US$', consultation_fee=10)
        cfg.visit_is_free.return_value = False
        cfg.visit_fee_waived_on_job.return_value = True
        with patch('bot.tenant_config.get_config', return_value=cfg), \
             patch.object(ladder, 'quote_given', return_value=False):
            offer = ladder._visit_offer(self._lead())
        self.assertNotIn('free', offer)
        self.assertIn('The call-out is US$10', offer)
        self.assertIn('taken off if you go ahead', offer)

    def test_it_goes_to_the_plumber_as_a_call_alert(self):
        lead = self._lead()
        with patch('bot.plumber_notifications.send_email_to_recipients',
                   return_value=True) as send:
            self.assertTrue(ladder.send_call_brief(lead))
        kwargs = send.call_args.kwargs
        self.assertEqual(kwargs['to_role'], 'plumber')
        self.assertEqual(kwargs['category'], 'plumber_alert')
        self.assertIn('Log the call', kwargs['html_message'])


class LadderCallPermissionTests(OfflineTestCase):
    """Option A (owner, 2026-09-22): a job-date lead with no email takes the
    portfolio on WhatsApp and is ASKED, in one to two lines, whether we may
    call two days before their date; an explicit no means the plumber gets no
    call brief, and no answer means the call goes ahead."""

    def _lead(self, **kw):
        lead = Appointment.objects.create(
            phone_number='whatsapp:+263770007402', customer_name='Rudo',
            project_description='Full re-tile and new fittings', **kw)
        ladder.arm(lead, date(2026, 10, 21))
        return lead

    def _ask(self, lead):
        from bot.out_of_scope_handler import _portfolio_on_whatsapp_ack
        with patch.object(Appointment, 'plumber_contact', return_value='+263771111111'):
            return _portfolio_on_whatsapp_ack(lead)

    def _answer(self, lead, text):
        from bot.out_of_scope_handler import _read_pending, _handle_call_permission_answer
        return _handle_call_permission_answer(text, _read_pending(lead), lead)

    def test_the_reply_is_ack_then_handoff_then_the_call_question(self):
        from bot.views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER
        parts = self._ask(self._lead()).split(MESSAGE_SPLIT_MARKER)
        self.assertEqual(parts[0], "That's fine, we've sent the portfolio here.")
        self.assertIn('wa.me/263771111111?text=', parts[1])
        self.assertIn('which is why', parts[1])
        # The offer leads: a free online quote first, no visit (owner, 2026-09-22).
        self.assertTrue(parts[1].startswith('You can get a free online quote first.'))
        self.assertTrue(parts[1].rstrip().endswith('+263771111111'))
        self.assertEqual(parts[2], "Would it be okay if we called you on the 19th of "
                                   "October, just to see if you've got the help you need?")
        # Short lines: no line of copy (the link aside) runs past ~110 characters,
        # about two lines on a phone (the owner's call question is 102).
        for line in '\n'.join(parts).splitlines():
            if 'wa.me' not in line:
                self.assertLessEqual(len(line), 110, line)

    def test_a_lead_with_an_email_gets_the_handoff_but_no_call_question(self):
        reply = self._ask(self._lead(customer_email='rudo@example.com'))
        self.assertIn('+263771111111', reply)
        self.assertNotIn('called you', reply)

    def test_a_yes_confirms_the_day(self):
        lead = self._lead()
        self._ask(lead)
        self.assertEqual(self._answer(lead, 'Yes sure'),
                         "Perfect, we'll give you a call on the 19th of October.")
        self.assertIn(ladder.CALL_OK_TAG, lead.internal_notes)

    def test_an_explicit_no_stops_the_plumber_call(self):
        lead = self._lead()
        self._ask(lead)
        self.assertIn("we won't call", self._answer(lead, 'Ok but no calls please'))
        self.assertIn(ladder.NO_CALL_TAG, lead.internal_notes)
        from bot.management.commands.send_followups import Command
        with patch.object(ladder, 'send_call_brief') as brief:
            Command(stdout=StringIO())._ladder_call(
                lead, ladder.job_date(lead), date(2026, 10, 19), False, ladder)
        brief.assert_not_called()
        self.assertEqual(ladder.step(lead), ladder.STEP_DONE)

    def test_something_else_is_answered_by_the_normal_flow(self):
        lead = self._lead()
        self._ask(lead)
        self.assertIsNone(self._answer(lead, 'How much is a new toilet?'))
        self.assertNotIn(ladder.NO_CALL_TAG, lead.internal_notes)
        from bot.out_of_scope_handler import _read_pending
        self.assertIsNone(_read_pending(lead))

    def test_the_brief_says_whether_they_agreed(self):
        lead = self._lead()
        self._ask(lead)
        _, silent, _ = ladder.call_brief(lead)
        self.assertIn('they did not answer, so the call goes ahead', silent)
        self.assertIn('If they already got a quote from you on WhatsApp', silent)
        self._answer(lead, 'ok')
        _, agreed, _ = ladder.call_brief(lead)
        self.assertIn('They said yes when we asked', agreed)

    def test_get_quote_always_carries_the_lead_details(self):
        from bot.customer_emails import _get_quote_button
        lead = self._lead()
        with patch.object(Appointment, 'plumber_contact', return_value='+263771111111'),              patch('bot.plumber_link._tenant_short_link',
                   return_value='https://wa.me/message/ABC123'):
            html = _get_quote_button(lead)
        self.assertIn('wa.me/263771111111?text=', html)
        self.assertNotIn('wa.me/message/', html)

    def test_the_whole_reply_survives_the_outbound_chain(self):
        """finalise_outbound runs on every send (one question per part, the
        free-visit and dash passes, speak-as-we); the link, the number and the
        call question must all come out the other side."""
        from bot.whatsapp_webhook import finalise_outbound
        lead = self._lead()
        reply = self._ask(lead)
        with patch.object(Appointment, 'plumber_contact', return_value='+263771111111'):
            out = finalise_outbound(reply, lead, 'no thanks, just send it here', check=False)
        self.assertIn('wa.me/263771111111?text=', out)
        self.assertIn('+263771111111', out)
        self.assertIn("Would it be okay if we called you on the 19th of October", out)


class HesitationTests(OfflineTestCase):
    """Hesitation over the visit gets the free online quote once; a push for an
    exact figure gets the no-guess line and the online quote (owner decisions
    1B, 6B, D, 2026-09-23)."""

    OFFER = ("Great, what works better for you, tomorrow at 9am or Thursday at "
             "2pm, for us to come through and take a quick look?")

    def _lead(self, **kw):
        lead = make_lead(7500 + Appointment.objects.count(), **kw)
        lead.add_conversation_message('assistant', self.OFFER)
        return lead

    def _reply(self, lead, text):
        from bot.hesitation import reply_for
        return reply_for(text, lead)

    def test_reluctance_gets_the_handoff_once(self):
        lead = self._lead()
        reply = self._reply(lead, "Can't you just quote without coming?")
        self.assertTrue(reply.startswith('No pressure at all on the visit.'))
        self.assertIn('You can get a free online quote first.', reply)
        self.assertIn(PLUMBER_WA, reply)
        self.assertTrue(reply.rstrip().endswith('+263774819901'))
        self.assertIsNone(self._reply(lead, 'Not sure yet'))     # once only

    def test_unsure_counts_only_right_after_a_visit_offer(self):
        lead = make_lead(7590)
        lead.add_conversation_message('assistant', 'What area are you in?')
        self.assertIsNone(self._reply(lead, 'not sure'))
        self.assertIsNotNone(self._reply(self._lead(), 'not sure yet'))

    def test_a_lead_who_has_the_link_gets_one_short_line(self):
        lead = self._lead(internal_notes=LINK_SENT_TAG)
        reply = self._reply(lead, 'Do you have to come?')
        self.assertIn('You can still get a free online quote first', reply)
        self.assertIn('+263774819901', reply)
        self.assertNotIn(PLUMBER_WA, reply)

    def test_an_exact_figure_push_gets_the_no_guess_lines(self):
        reply = self._reply(self._lead(), 'Just give me the exact price')
        self.assertTrue(reply.startswith(
            "We'd rather not guess. A quick look and you get a firm price.\n"
            "Or if it's easier, send a few photos for a free online quote first."))
        self.assertIn(PLUMBER_WA, reply)

    def test_never_to_a_booked_lead_a_shona_lead_or_a_real_answer(self):
        booked = self._lead(status='confirmed')
        self.assertIsNone(self._reply(booked, "Can't you just quote?"))
        self.assertIsNone(self._reply(self._lead(), 'Hameno, ndichaona'))
        self.assertIsNone(self._reply(self._lead(), 'Tomorrow at 9am works'))
        self.assertIsNone(self._reply(self._lead(), 'next month'))


class NearDateContactTests(OfflineTestCase):
    """A day within the week, two kinds of lead (owner, 2026-09-23).

    Type 1 "I'll contact you on Monday": we wait; if the day passes with no
    word, no quote and no visit, the plumber gets a call email the morning
    after. Type 2 "Contact me on Monday": we ask for the email; with none, the
    plumber gets the call email that morning and the lead is told we'll call."""

    def setUp(self):
        super().setUp()
        self.day = timezone.localdate() + timedelta(days=2)

    def _answer(self, lead, message):
        from bot.out_of_scope_handler import _handle_delay_timeframe_answer
        with patch('bot.out_of_scope_handler._compute_followup_date',
                   _date_reader(self.day)):
            return _handle_delay_timeframe_answer(message, {}, lead)

    def _at(self, day, hour):
        import pytz
        return pytz.timezone('Africa/Johannesburg').localize(
            datetime(day.year, day.month, day.day, hour, 0))

    def test_type_1_we_wait_and_do_not_chase(self):
        from bot import copy_catalog
        lead = make_lead(9101)
        reply = self._answer(lead, "I'll contact you on Monday")
        # We wait, and still ask for the email (owner, 2026-09-23).
        self.assertTrue(reply.startswith(copy_catalog.NEAR_WAIT_FOR_THEM))
        self.assertIn('can I get your email', reply)
        self.assertNotIn('check back', reply.lower())
        lead.refresh_from_db()
        self.assertIn('[NEAR_AWAIT]', lead.internal_notes)
        self.assertNotIn('[FOLLOW_UP_DATE]', lead.internal_notes)   # no dated check-back
        self.assertGreater(lead.delay_followup_due_at, timezone.now())   # follow-ups held

    def test_type_2_asks_for_the_email_instead_of_booking_a_visit(self):
        from bot import copy_catalog
        from bot.out_of_scope_handler import _read_pending
        lead = make_lead(9102)
        reply = self._answer(lead, 'Contact me on Monday')
        self.assertEqual(reply, copy_catalog.NEAR_EMAIL_ASK)
        self.assertNotIn('What time suits you', reply)
        lead.refresh_from_db()
        self.assertIn('[NEAR_CALL]', lead.internal_notes)
        self.assertEqual(_read_pending(lead)['category'], 'delay_email')

    @patch('bot.out_of_scope_handler._deliver_pdf_and_schedule_checkin', return_value=True)
    def test_type_2_without_an_email_is_told_we_will_call(self, _pdf):
        from bot.out_of_scope_handler import _handle_delay_email_answer, _read_pending
        lead = make_lead(9103)
        self._answer(lead, 'Contact me on Monday')
        lead.refresh_from_db()
        with patch('bot.out_of_scope_handler._classify_email_step_reply',
                   return_value='whatsapp'):
            reply = _handle_delay_email_answer('no thanks, just send it here',
                                               _read_pending(lead), lead)
        self.assertIn("No problem, we'll give you a call on", reply)
        self.assertNotIn('Would it be okay if we called', reply)

    def _tick(self, now):
        from bot.near_date_call import run_tick
        with patch('bot.plumber_notifications.split_notification_recipients',
                   return_value=(['plumber@example.com'], [])), \
             patch('bot.plumber_notifications.send_email_to_recipients',
                   return_value=True) as send:
            stats = run_tick(now=now)
        return stats, send

    def test_type_1_plumber_is_emailed_the_morning_after_not_before(self):
        lead = make_lead(9104)
        self._answer(lead, "I'll contact you on Monday")
        stats, send = self._tick(self._at(self.day, 10))          # the day itself
        send.assert_not_called()
        stats, send = self._tick(self._at(self.day + timedelta(days=1), 7))   # too early
        send.assert_not_called()
        stats, send = self._tick(self._at(self.day + timedelta(days=1), 9))
        send.assert_called_once()
        subject, text = send.call_args[0][1], send.call_args[0][2]
        self.assertTrue(subject.startswith('Please call today: '))
        self.assertIn('said they would contact us', text)
        self.assertLess(text.index('SCRIPT'), text.index('LEAD DETAILS'))
        stats, send = self._tick(self._at(self.day + timedelta(days=1), 12))
        send.assert_not_called()                                    # once only

    def test_type_1_no_call_if_they_wrote_booked_or_got_a_quote(self):
        lead = make_lead(9105)
        self._answer(lead, "I'll contact you on Monday")
        lead.refresh_from_db()
        lead.last_customer_response = timezone.now() + timedelta(minutes=5)
        lead.save(update_fields=['last_customer_response'])
        stats, send = self._tick(self._at(self.day + timedelta(days=1), 9))
        send.assert_not_called()
        self.assertEqual(stats['skipped'], 1)

    def test_type_2_plumber_is_emailed_that_morning_only_without_an_email(self):
        lead = make_lead(9106)
        self._answer(lead, 'Contact me on Monday')
        stats, send = self._tick(self._at(self.day, 9))
        send.assert_called_once()
        self.assertIn('asked us to call them today', send.call_args[0][1])
        mailed = make_lead(9107, customer_email='rudo@example.com')
        self._answer(mailed, 'Contact me on Monday')
        stats, send = self._tick(self._at(self.day, 10))
        send.assert_not_called()

    @patch('bot.customer_emails.send_delay_quote_email_async')
    def test_type_1_email_gets_the_portfolio_and_no_check_back(self, portfolio):
        from bot import copy_catalog
        from bot.out_of_scope_handler import _handle_delay_email_answer, _read_pending
        lead = make_lead(9108)
        self._answer(lead, "I'll contact you on Monday")
        lead.refresh_from_db()
        reply = _handle_delay_email_answer('rudo@example.com', _read_pending(lead), lead)
        self.assertEqual(reply, copy_catalog.NEAR_EMAIL_THANKS)
        portfolio.assert_called_once()
        lead.refresh_from_db()
        self.assertFalse(lead.is_delayed)
        self.assertNotIn('[FOLLOW_UP_DATE]', lead.internal_notes)

    @patch('bot.out_of_scope_handler.send_lead_magnet_on_whatsapp', return_value=True)
    def test_type_1_declining_email_is_not_chased_or_parked(self, _pdf):
        from bot.out_of_scope_handler import _handle_delay_email_answer, _read_pending
        lead = make_lead(9109)
        self._answer(lead, "I'll contact you on Monday")
        lead.refresh_from_db()
        with patch('bot.out_of_scope_handler._classify_email_step_reply',
                   return_value='whatsapp'):
            reply = _handle_delay_email_answer('no thanks, send it here',
                                               _read_pending(lead), lead)
        self.assertNotIn('give you a call', reply)
        lead.refresh_from_db()
        self.assertFalse(lead.is_delayed)
        self.assertNotIn('pdf_checkin', lead.internal_notes)
        self.assertGreater(lead.delay_followup_due_at, timezone.now())
        stats, send = self._tick(self._at(self.day + timedelta(days=1), 9))
        send.assert_called_once()                     # the plumber still calls
