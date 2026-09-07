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
