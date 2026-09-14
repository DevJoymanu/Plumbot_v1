"""
The model reading back what we are about to send.

Most of this is about the FENCES, not the happy path. Letting a model rewrite
outbound copy runs straight at the house rule that deterministic code writes
anything the customer must be able to trust, so what it may not do is the part
worth pinning.
"""

from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from .models import Appointment, Tenant
from .response_check import _fences_hold, verify_and_refine


def _reply(verdict='refine', text='', reason='out of the country'):
    import json
    return json.dumps({'verdict': verdict, 'reason': reason, 'reply': text})


class FenceTests(TestCase):
    """What a refinement is not allowed to do."""

    def test_a_new_figure_is_refused(self):
        ok, why = _fences_hold('We can come and take a look.',
                               'We can come and take a look for US$40.')
        self.assertFalse(ok)
        self.assertIn('figure', why)

    def test_keeping_the_same_figure_is_fine(self):
        ok, _ = _fences_hold('The visit is US$10, and it comes off the job.',
                             'The visit is US$10. It comes off the job.')
        self.assertTrue(ok)

    def test_a_new_promise_is_refused(self):
        for word in ('free', 'discount', 'guarantee', 'mahara'):
            ok, why = _fences_hold('We can come and take a look.',
                                   'We can come and take a look, %s.' % word)
            self.assertFalse(ok, word)
            self.assertIn('promise', why)

    def test_a_promise_already_in_the_draft_may_stay(self):
        ok, _ = _fences_hold('The first visit is free.',
                             'That first visit is free.')
        self.assertTrue(ok)

    def test_a_rewrite_is_refused_a_correction_is_not(self):
        draft = 'What area are you in?'
        self.assertFalse(_fences_hold(draft, 'x' * 400)[0])
        self.assertTrue(_fences_hold(draft, 'Whereabouts are you?')[0])

    def test_an_empty_refinement_is_refused(self):
        self.assertFalse(_fences_hold('What area are you in?', '')[0])
        self.assertFalse(_fences_hold('What area are you in?', '   ')[0])


class VerifyTests(TestCase):
    def setUp(self):
        self.tenant = Tenant.objects.create(name='Acme', slug='acme')
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+263771234567', tenant=self.tenant,
            customer_area='Norton', status='pending')
        self.lead.add_conversation_message('user', 'Currently im out of the country')
        self.lead.save()

    def test_an_ok_verdict_leaves_the_draft_exactly_as_it_was(self):
        draft = 'What works better, tomorrow or Tuesday?'
        with patch('bot.services.clients.deepseek_call',
                   return_value=_reply('ok')):
            out, note = verify_and_refine(draft, self.lead, 'hello')
        self.assertEqual(out, draft)
        self.assertIsNone(note)

    def test_a_refinement_replaces_the_draft_and_is_flagged(self):
        draft = 'What works better, tomorrow or this Tuesday?'
        fixed = 'No problem. When are you back in the country?'
        with patch('bot.services.clients.deepseek_call',
                   return_value=_reply('refine', fixed)):
            out, note = verify_and_refine(draft, self.lead, 'im out of the country')
        self.assertEqual(out, fixed)
        self.assertIn('corrected', note.lower())

    def test_a_refinement_that_breaks_a_fence_is_discarded(self):
        draft = 'We can come and take a look.'
        with patch('bot.services.clients.deepseek_call',
                   return_value=_reply('refine', 'We can come for free!')):
            out, note = verify_and_refine(draft, self.lead, 'how much')
        self.assertEqual(out, draft)      # the draft goes, not the refinement
        self.assertIsNone(note)

    def test_it_fails_open_on_any_error(self):
        draft = 'What area are you in?'
        for boom in (Exception('timeout'), ValueError('nope')):
            with patch('bot.services.clients.deepseek_call', side_effect=boom):
                out, note = verify_and_refine(draft, self.lead, 'hi')
            self.assertEqual(out, draft)
            self.assertIsNone(note)

    def test_it_fails_open_on_an_unparseable_body(self):
        with patch('bot.services.clients.deepseek_call',
                   return_value='not json at all'):
            out, note = verify_and_refine('What area are you in?', self.lead, 'hi')
        self.assertEqual(out, 'What area are you in?')
        self.assertIsNone(note)

    def test_the_switch_turns_it_off_without_a_deploy(self):
        with patch('bot.response_check.REPLY_CHECK_ENABLED', False):
            with patch('bot.services.clients.deepseek_call') as call:
                out, note = verify_and_refine('anything', self.lead, 'hi')
        self.assertEqual(out, 'anything')
        self.assertIsNone(note)
        call.assert_not_called()

    def test_the_checker_sees_the_facts_and_the_transcript(self):
        self.lead.timeline = 'back on the 22nd of December'
        self.lead.plan_status = 'plan_uploaded'
        self.lead.save()
        seen = {}

        def _capture(messages, **kw):
            seen['prompt'] = messages[-1]['content']
            return _reply('ok')

        with patch('bot.services.clients.deepseek_call', side_effect=_capture):
            verify_and_refine('A draft.', self.lead, 'hello')
        prompt = seen['prompt']
        self.assertIn('Norton', prompt)
        self.assertIn('22nd of December', prompt)
        self.assertIn('sent a plan', prompt)
        self.assertIn('out of the country', prompt)   # from the transcript


class ChainOrderTests(TestCase):
    """A refinement is just another draft: the strippers still apply to it."""

    def test_the_check_runs_before_the_strippers(self):
        import inspect
        from . import whatsapp_webhook as wh
        src = inspect.getsource(wh.finalise_outbound)
        # Compare against the CALL sites, not the import block at the top.
        self.assertLess(src.find('verify_and_refine(reply'),
                        src.find('strip_known_questions(reply'))
        self.assertLess(src.find('verify_and_refine(reply'),
                        src.find('strip_dashes(reply'))

    def test_a_refinement_still_loses_its_dashes(self):
        from . import whatsapp_webhook as wh
        tenant = Tenant.objects.create(name='Acme2', slug='acme2')
        lead = Appointment.objects.create(
            phone_number='whatsapp:+263770000001', tenant=tenant)
        with patch('bot.services.clients.deepseek_call',
                   return_value=_reply('refine', 'Sure thing - when suits you?')):
            out = wh.finalise_outbound('Sure thing. When suits you?', lead, 'hi')
        self.assertNotIn(' - ', out)


class QuotedMessageReachesTheReaderTests(TestCase):
    """A highlighted message is context, so the reader has to see it.

    WhatsApp delivers only the quoted message's WAMID, which the webhook
    resolves locally and stores on the inbound entry. Everything downstream
    that judges CONTEXT has to read it back, or "this one, how much?" reaches
    the reader as a message about nothing.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name='Quoting', slug='quoting')
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+263773333333', tenant=self.tenant)

    def test_the_transcript_marks_what_was_highlighted(self):
        from .response_check import _transcript
        self.lead.add_conversation_message(
            'assistant', 'Freestanding tub, from US$670.', message_id='wamid.T')
        self.lead.add_conversation_message(
            'user', 'this one how much?',
            quoted=self.lead.resolve_quoted_message('wamid.T'))
        out = _transcript(self.lead)
        self.assertIn('highlighting our earlier message', out)
        self.assertIn('Freestanding tub, from US$670.', out)
        self.assertIn('CUSTOMER', out.split('\n')[-1])

    def test_a_turn_with_no_quote_is_unchanged(self):
        from .response_check import _transcript
        self.lead.add_conversation_message('user', 'hi')
        self.assertEqual(_transcript(self.lead), 'CUSTOMER: hi')

    def test_the_batched_turn_keeps_the_quote_on_its_own_message(self):
        # The debounce joins rapid messages into ONE reply, and only one of
        # them carries the quote. Reading it off the entry rather than
        # threading one value through keeps it attached to the right message.
        from .response_check import _transcript
        self.lead.add_conversation_message(
            'assistant', 'Here are a couple we just finished.',
            message_id='wamid.P')
        self.lead.add_conversation_message(
            'user', 'this one', quoted='Here are a couple we just finished.')
        self.lead.add_conversation_message('user', 'how much is it')
        lines = _transcript(self.lead).split('\n')
        self.assertIn('highlighting', lines[1])
        self.assertNotIn('highlighting', lines[2])

    def test_the_reader_is_given_the_quote(self):
        # End to end through verify_and_refine: whatever the model is asked,
        # the highlighted text is in the payload it sees.
        from .response_check import verify_and_refine
        self.lead.add_conversation_message(
            'assistant', 'Freestanding tub, from US$670.', message_id='wamid.T')
        self.lead.add_conversation_message(
            'user', 'this one how much?', quoted='Freestanding tub, from US$670.')
        with patch('bot.services.clients.deepseek_call',
                   return_value=_reply('ok')) as call:
            verify_and_refine('From US$670.', self.lead, 'this one how much?')
        payload = call.call_args[0][0][1]['content']
        self.assertIn('highlighting our earlier message', payload)
        self.assertIn('Freestanding tub', payload)


class PhotoPathStampsItsWamidsTests(TestCase):
    """Both text messages around the gallery must be quotable.

    The images have carried a WAMID each since record_sent_media, but the
    lead-in and the follow-up never did, so a customer highlighting either of
    them resolved to None and was answered quote-blind - the "not found in
    history" log line.
    """

    def test_both_lines_are_stamped_and_resolve_back(self):
        import inspect
        from . import whatsapp_webhook as wh
        src = inspect.getsource(wh.send_previous_work_photos)
        self.assertEqual(src.count('_sent_wamid('), 2)
        self.assertEqual(src.count('mark_message_sent('), 2)

    def test_a_stamped_line_resolves_to_its_text(self):
        tenant = Tenant.objects.create(name='Stamped', slug='stamped')
        lead = Appointment.objects.create(
            phone_number='whatsapp:+263774444444', tenant=tenant)
        lead.add_conversation_message('assistant', 'What area are you in?')
        lead.mark_message_sent('assistant', 'What area are you in?', 'wamid.Q')
        self.assertEqual(lead.resolve_quoted_message('wamid.Q'),
                         'What area are you in?')

    def test_the_wamid_reader_never_raises_on_a_bad_result(self):
        from .whatsapp_webhook import _sent_wamid
        for bad in (None, {}, {'messages': []}, {'messages': [{}]}, 'nope', 42):
            self.assertIsNone(_sent_wamid(bad))
        self.assertEqual(_sent_wamid({'messages': [{'id': 'wamid.X'}]}), 'wamid.X')


class PhotoPathGetsTheChainTests(TestCase):
    """The photo path writes to the wire itself, so it must finalise itself.

    Its lead-in and its follow-up went straight to send_text_message, skipping
    the reply check, the memory check, both free-visit strippers, the
    visit-price note and the dash stripper. The follow-up is the one that bit:
    it carries the scripted next question, which for availability_date IS the
    availability ask, and that is the message the call-out fee has to be
    stated on.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name='Fee Co', slug='fee-co')
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+263772222222', tenant=self.tenant,
            project_description='bathroom refit', customer_area='Borrowdale')
        # The reader is a live DeepSeek call; these cases are about the
        # deterministic rules that follow it.
        self.addCleanup(patch.stopall)
        patch('bot.response_check.verify_and_refine',
              side_effect=lambda r, a, m=None: (r, None)).start()

    def _with_fee(self, fee):
        """Point tenant_config at a tenant that charges for the visit."""
        from bot import tenant_config as tc
        real = tc.get_config
        cfg = real(self.tenant)
        patch.object(type(cfg), 'consultation_fee', fee).start()
        return cfg

    def test_the_followup_carries_the_call_out_fee(self):
        from . import whatsapp_webhook as wh
        self._with_fee(20)
        ask = ('What works better for you, tomorrow at 9am or this Sunday '
               'at 2pm, for us to come through and have a quick look at the '
               'bathroom space?')
        out = wh._finalised_for_send(ask, self.lead, 'borrowdale')
        self.assertIn('US$20', out)
        self.assertIn('call-out', out.lower())

    def test_a_free_visit_claim_cannot_survive_for_a_fee_tenant(self):
        from . import whatsapp_webhook as wh
        self._with_fee(20)
        out = wh._finalised_for_send(
            'We can come out for a free site visit. What day suits you?',
            self.lead, 'ok')
        self.assertNotIn('free', out.lower())

    def test_dashes_are_stripped_on_this_path_too(self):
        from . import whatsapp_webhook as wh
        out = wh._finalised_for_send('Sure thing - when suits you?',
                                     self.lead, 'hi')
        self.assertNotIn(' - ', out)

    def test_the_split_marker_never_reaches_the_wire(self):
        # Only delayed_response knows how to flatten it, and this path does
        # not go through delayed_response.
        from . import whatsapp_webhook as wh
        from .views.plumbot.response_mixin import MESSAGE_SPLIT_MARKER
        out = wh._finalised_for_send(
            'Got it.%sWhat area are you in?' % MESSAGE_SPLIT_MARKER,
            self.lead, 'hi')
        self.assertNotIn(MESSAGE_SPLIT_MARKER, out)

    def test_it_fails_open_rather_than_losing_the_message(self):
        from . import whatsapp_webhook as wh
        with patch.object(wh, 'finalise_outbound', side_effect=RuntimeError('boom')):
            out = wh._finalised_for_send('Here are a couple we just finished.',
                                         self.lead, 'photos?')
        self.assertEqual(out, 'Here are a couple we just finished.')

    def test_check_false_skips_the_reader_but_not_the_rules(self):
        from . import whatsapp_webhook as wh
        with patch('bot.response_check.verify_and_refine') as reader:
            out = wh.finalise_outbound('Sure thing - when suits you?',
                                       self.lead, 'hi', check=False)
        reader.assert_not_called()
        self.assertNotIn(' - ', out)

    def test_both_photo_sends_go_through_the_helper(self):
        # Source-level: the sends happen inside a daemon thread behind a real
        # media upload, and what matters is that neither reaches
        # send_text_message with an unfinalised draft.
        import inspect
        from . import whatsapp_webhook as wh
        src = inspect.getsource(wh.send_previous_work_photos)
        sends = [ln for ln in src.split('\n') if 'send_text_message(' in ln]
        self.assertEqual(len(sends), 2, src)
        for line in sends:
            self.assertRegex(line, r'send_text_message\(sender, (intro_out|follow_up)\)')
        self.assertEqual(src.count('_finalised_for_send('), 2)


class VolunteeredEmailTests(TestCase):
    """The email capture, exercised rather than grepped.

    The first version of this was a TEST 0 case asserting the ROUTER SOURCE
    contained the right marker string. It passed while the code raised
    NameError on every message, because whatsapp_webhook has no top-level
    `import re` and the block had no local one. Production logged
    "Could not capture a volunteered email: name 're' is not defined" twice
    before anybody noticed. A static check cannot see a NameError; this runs it.
    """

    def setUp(self):
        self.tenant = Tenant.objects.create(name='Acme3', slug='acme3')
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+263771111111', tenant=self.tenant)

    def _run(self, text):
        from . import whatsapp_webhook as wh
        # The capture sits at the top of the router, before any branch.
        # These two are imported INSIDE the router, so they patch at source.
        with patch('bot.out_of_scope_handler.is_hard_stop_request',
                   return_value=True), \
             patch('bot.out_of_scope_handler.build_hard_stop_reply',
                   return_value='ok'), \
             patch.object(wh, 'delayed_response'), \
             patch('bot.services.clients.deepseek_call',
                   return_value=_reply('ok')):
            wh._generate_and_schedule_reply(
                '263771111111', text, tenant=self.tenant)
        self.lead.refresh_from_db()
        return self.lead.customer_email

    def test_an_email_in_the_message_is_captured(self):
        self.assertEqual(self._run('sdziwotizeyi@gmail.com'),
                         'sdziwotizeyi@gmail.com')

    def test_it_is_found_inside_a_batched_turn(self):
        self.lead.customer_email = None
        self.lead.save(update_fields=['customer_email'])
        got = self._run('needs demolition and new brickwork\nrudo@example.com')
        self.assertEqual(got, 'rudo@example.com')

    def test_an_existing_address_is_never_overwritten(self):
        self.lead.customer_email = 'first@example.com'
        self.lead.save(update_fields=['customer_email'])
        self.assertEqual(self._run('second@example.com'), 'first@example.com')

    def test_a_message_with_no_address_changes_nothing(self):
        self.assertIsNone(self._run('what area are you in'))


class DocumentSenderTests(TestCase):
    """Whose document is this: a customer's plan, or somebody selling to us?"""

    def setUp(self):
        self.tenant = Tenant.objects.create(name='Acme4', slug='acme4')
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+263772222222', tenant=self.tenant)

    def _said(self, *msgs):
        for m in msgs:
            self.lead.add_conversation_message('user', m)
        self.lead.save()

    def test_asking_for_it_needs_no_call_at_all(self):
        from bot.plan_detection import is_their_own_plan
        with patch('bot.services.clients.deepseek_call') as call:
            self.assertTrue(is_their_own_plan(self.lead, asked_for_it=True))
        call.assert_not_called()

    def test_a_supplier_pitch_is_not_their_plan(self):
        # Barmak 966, verbatim. Caught by keywords, so no call is needed.
        self._said("I'm Primrose from Edenvine construction, a leading "
                   "supplier of bathroom fittings", 'Here is our catalogue')
        from bot.plan_detection import is_their_own_plan
        with patch('bot.services.clients.deepseek_call') as call:
            self.assertFalse(is_their_own_plan(self.lead))
        call.assert_not_called()

    def test_an_ordinary_customer_document_is_their_plan(self):
        # Barmak 1012: sent a PDF unprompted, then asked for a quotation.
        self._said('New installation', 'I need a quotation of both ensuite and toilats')
        from bot.plan_detection import is_their_own_plan
        import json as _j
        with patch('bot.services.clients.deepseek_call',
                   return_value=_j.dumps({'is_customer_plan': True})):
            self.assertTrue(is_their_own_plan(self.lead))

    def test_the_model_can_say_no_where_keywords_saw_nothing(self):
        self._said('Good day, attached is our latest range for your review')
        import json as _j
        from bot.plan_detection import is_their_own_plan
        with patch('bot.services.clients.deepseek_call',
                   return_value=_j.dumps({'is_customer_plan': False})):
            self.assertFalse(is_their_own_plan(self.lead))

    def test_it_defaults_to_the_customer_when_the_api_is_down(self):
        # A wrong NO asks a customer for a plan they already sent; a wrong YES
        # chases the plumber about a pitch. The first is commoner and cheaper.
        self._said('Here you go')
        from bot.plan_detection import is_their_own_plan
        with patch('bot.services.clients.deepseek_call',
                   side_effect=Exception('down')):
            self.assertTrue(is_their_own_plan(self.lead))

    def test_the_prompt_carries_the_word_json(self):
        # DeepSeek 400s in JSON mode without it, and the fallback then hides
        # the fact that the model was never consulted.
        import inspect
        from bot import plan_detection
        src = inspect.getsource(plan_detection.is_their_own_plan)
        self.assertIn('json', src.lower())
