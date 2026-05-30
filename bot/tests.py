from django.test import TestCase
from django.utils import timezone

from datetime import timedelta
from unittest.mock import patch

from .models import Appointment, LeadStatus
from .management.commands.send_followups import Command
from .services.lead_scoring import calculate_lead_score, refresh_lead_score


class LeadScoringTests(TestCase):
    def test_cold_when_zero_or_one_fields(self):
        appointment = Appointment.objects.create(phone_number="whatsapp:+10000000001")
        score, status = calculate_lead_score(appointment)
        self.assertEqual(score, 0)
        self.assertEqual(status, LeadStatus.COLD)

    def test_warm_when_three_fields(self):
        appointment = Appointment.objects.create(
            phone_number="whatsapp:+10000000002",
            project_type="bathroom_renovation",
            property_type="house",
            customer_area="Hatfield",
        )
        score, status = calculate_lead_score(appointment)
        self.assertEqual(score, 60)
        self.assertEqual(status, LeadStatus.WARM)

    def test_hot_when_four_fields(self):
        appointment = Appointment.objects.create(
            phone_number="whatsapp:+10000000003",
            project_type="bathroom_renovation",
            property_type="house",
            customer_area="Hatfield",
            timeline="This month",
        )
        score, status = calculate_lead_score(appointment)
        self.assertEqual(score, 80)
        self.assertEqual(status, LeadStatus.HOT)

    def test_site_visit_override_sets_very_hot_and_100(self):
        appointment = Appointment.objects.create(
            phone_number="whatsapp:+10000000004",
            scheduled_datetime=timezone.now(),
        )
        score, status = calculate_lead_score(appointment)
        self.assertEqual(score, 100)
        self.assertEqual(status, LeadStatus.VERY_HOT)

    def test_refresh_sets_pause_for_very_hot(self):
        appointment = Appointment.objects.create(
            phone_number="whatsapp:+10000000005",
            scheduled_datetime=timezone.now(),
        )
        refresh_lead_score(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.lead_score, 100)
        self.assertEqual(appointment.lead_status, LeadStatus.VERY_HOT)
        self.assertTrue(appointment.chatbot_paused)


class FollowUpDeliveryModeTests(TestCase):
    def setUp(self):
        self.command = Command()

    @patch('bot.management.commands.send_followups.whatsapp_api.send_text_message')
    @patch('bot.management.commands.send_followups.whatsapp_api.send_template_message')
    def test_uses_freeform_text_inside_24_hour_window(self, mock_send_template, mock_send_text):
        lead = Appointment.objects.create(
            phone_number='whatsapp:+10000000006',
            last_customer_response=timezone.now() - timedelta(hours=2),
        )

        mode = self.command._send_followup_message(lead, 'Checking in on your plumbing job.')

        self.assertEqual(mode, 'text')
        mock_send_text.assert_called_once_with('10000000006', 'Checking in on your plumbing job.')
        mock_send_template.assert_not_called()

    @patch('bot.management.commands.send_followups.whatsapp_api.send_text_message')
    @patch('bot.management.commands.send_followups.whatsapp_api.send_template_message')
    def test_rejects_outside_24_hour_window(self, mock_send_template, mock_send_text):
        lead = Appointment.objects.create(
            phone_number='whatsapp:+10000000007',
            last_customer_response=timezone.now() - timedelta(days=2),
        )

        with self.assertRaises(RuntimeError):
            self.command._send_followup_message(lead, 'Checking in on your plumbing job.')

        mock_send_text.assert_not_called()
        mock_send_template.assert_not_called()


class CustomerEmailAsyncTests(TestCase):
    @patch('bot.customer_emails.threading.Thread')
    def test_delay_quote_email_queues_daemon_thread(self, mock_thread):
        from bot.customer_emails import send_delay_quote_email_async

        appointment = Appointment.objects.create(
            phone_number='whatsapp:+10000000008',
            customer_email='customer@example.com',
        )

        result = send_delay_quote_email_async(
            appointment,
            follow_up_date_str='Sunday 31 May',
        )

        mock_thread.assert_called_once()
        thread_kwargs = mock_thread.call_args.kwargs
        self.assertTrue(callable(thread_kwargs['target']))
        self.assertEqual(thread_kwargs['name'], f'delay-quote-email-{appointment.pk}')
        self.assertTrue(thread_kwargs['daemon'])
        mock_thread.return_value.start.assert_called_once()
        self.assertIs(result, mock_thread.return_value)
