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
