"""
The "we can't open that" notices for stickers, contact cards, GIFs and voice
notes (whatsapp_webhook.handle_unsupported_media / handle_audio_message).

Found 2026-09-22: the sticker handler used a lead it never looked up, so every
sticker and contact card raised a NameError that was swallowed, and the lead
got no reply at all. The voice-note handler did the same for a sender with no
lead yet. Both now reply, and record what they sent.
"""

from unittest.mock import patch

from django.test import TestCase

from bot import whatsapp_webhook as wh
from bot.models import Appointment


class _RunNow:
    """Stands in for threading.Thread: runs the target on start()."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self.target, self.args, self.kwargs = target, args, kwargs or {}

    def start(self):
        self.target(*self.args, **self.kwargs)


class MediaNoticeTests(TestCase):

    def setUp(self):
        # Nothing here may reach DeepSeek or the network. The first run of
        # these tests did: the reply checker went to the LIVE model because the
        # stub was not installed. The stub first, then a net under it.
        from tests.deepseek_mock import install
        install()
        for target, kwargs in (
                ('bot.whatsapp_webhook.threading.Thread', {'new': _RunNow}),
                ('bot.whatsapp_webhook.get_random_delay', {'return_value': 0}),
                ('requests.sessions.Session.send',
                 {'side_effect': AssertionError('live HTTP (requests)')}),
                ('httpx.Client.send', {'side_effect': AssertionError('live HTTP (httpx)')})):
            patcher = patch(target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)
        sender = patch('bot.whatsapp_webhook.delayed_response')
        self.sent = sender.start()
        self.addCleanup(sender.stop)

    def _lead(self, n):
        return Appointment.objects.create(
            phone_number=f'whatsapp:+1555003{n:04d}', status='pending')

    def test_a_sticker_gets_a_reply_and_it_is_recorded(self):
        lead = self._lead(1)
        wh.handle_unsupported_media('15550030001', 'sticker', tenant=lead.tenant)
        self.sent.assert_called_once()
        text = self.sent.call_args.args[1]
        self.assertIn('send a text or a photo', text)
        self.assertNotIn('—', text)                 # through the chain: no dash
        lead.refresh_from_db()
        self.assertEqual(lead.conversation_history[-1]['content'], text)

    def test_a_contact_card_gets_a_reply(self):
        lead = self._lead(2)
        wh.handle_unsupported_media('15550030002', 'contacts', tenant=lead.tenant)
        self.sent.assert_called_once()

    def test_a_voice_note_from_a_sender_with_no_lead_still_gets_a_reply(self):
        wh.handle_audio_message('15550039999', {})
        self.sent.assert_called_once()
        self.assertIn('type it out', self.sent.call_args.args[1])

    def test_a_voice_note_from_a_lead_is_recorded(self):
        lead = self._lead(3)
        wh.handle_audio_message('15550030003', {}, tenant=lead.tenant)
        self.sent.assert_called_once()
        lead.refresh_from_db()
        self.assertIn('type it out', lead.conversation_history[-1]['content'])

    # The reply names what we were waiting on (owner, 2026-09-22).
    def _voice_reply(self, n, last_bot):
        lead = self._lead(n)
        lead.conversation_history = [{'role': 'assistant', 'content': last_bot}]
        lead.save(update_fields=['conversation_history'])
        wh.handle_audio_message(f'1555003{n:04d}', {}, tenant=lead.tenant)
        return self.sent.call_args.args[1]

    def test_a_voice_note_names_the_question_we_asked(self):
        for n, (last_bot, want) in enumerate((
                ('All good, what area are you in?', "We were just asking which area you're in."),
                ('What works better for you, tomorrow at 9am or Thursday at 2pm?',
                 'We were just asking what day and time suit you.'),
                ('Hi, sure. What needs doing, and is it one bathroom or a few?',
                 'We were just asking what needs doing.'),
                ('Would you rather get a personalised quote from a quick look at the space, '
                 'or a quote online first?',
                 "We were just asking whether you'd rather have a quick look at the space "
                 "or a quote online.")), start=10):
            with self.subTest(last_bot=last_bot):
                self.sent.reset_mock()
                reply = self._voice_reply(n, last_bot)
                self.assertTrue(reply.startswith("We can't play voice notes on this line."), reply)
                self.assertTrue(reply.endswith(want), reply)
                self.assertEqual(reply.count('?'), 1, reply)

    def test_an_unknown_question_is_quoted_back(self):
        reply = self._voice_reply(20, 'Is the geyser on the roof or inside?')
        self.assertIn('We had just asked: "Is the geyser on the roof or inside?"', reply)

    def test_no_open_question_means_no_tail(self):
        reply = self._voice_reply(21, 'Thanks, speak soon.')
        self.assertTrue(reply.endswith('Could you type it out?'), reply)
