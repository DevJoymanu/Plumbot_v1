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

from django.test import TestCase, override_settings
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
        # ...and the plumber is told who is about to message him.
        from bot.plumber_notifications import send_email_to_recipients
        subjects = [c[0][1] for c in send_email_to_recipients.call_args_list]
        self.assertTrue(any(s.startswith('[Quote lead]') for s in subjects), subjects)
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
    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
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
