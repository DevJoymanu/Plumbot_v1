import os

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from datetime import timedelta
from unittest.mock import patch

from .models import Appointment, LeadStatus
from .services.lead_scoring import calculate_lead_score, refresh_lead_score


class LeadScoringTests(TestCase):
    def test_cold_when_zero_or_one_fields(self):
        appointment = Appointment.objects.create(phone_number="whatsapp:+10000000001")
        score, status = calculate_lead_score(appointment)
        self.assertEqual(score, 0)
        self.assertEqual(status, LeadStatus.COLD)

    def test_warm_when_two_flow_fields(self):
        """Only the 4 required flow fields score (25 pts each) — passive
        fields like property_type / timeline no longer count."""
        appointment = Appointment.objects.create(
            phone_number="whatsapp:+10000000002",
            project_type="bathroom_renovation",
            property_type="house",       # passive — must NOT score
            customer_area="Hatfield",
        )
        score, status = calculate_lead_score(appointment)
        self.assertEqual(score, 50)
        self.assertEqual(status, LeadStatus.WARM)

    def test_hot_when_three_flow_fields(self):
        appointment = Appointment.objects.create(
            phone_number="whatsapp:+10000000003",
            project_type="bathroom_renovation",
            customer_area="Hatfield",
            has_plan=False,              # an answered has_plan counts
            timeline="This month",       # passive — must NOT score
        )
        score, status = calculate_lead_score(appointment)
        self.assertEqual(score, 75)
        self.assertEqual(status, LeadStatus.HOT)

    def test_site_visit_override_sets_very_hot_and_100(self):
        appointment = Appointment.objects.create(
            phone_number="whatsapp:+10000000004",
            scheduled_datetime=timezone.now(),
        )
        score, status = calculate_lead_score(appointment)
        self.assertEqual(score, 100)
        self.assertEqual(status, LeadStatus.VERY_HOT)

    def test_refresh_persists_score_without_auto_pausing(self):
        """refresh_lead_score persists score/status; the chatbot is only
        ever paused manually — never auto-paused here."""
        appointment = Appointment.objects.create(
            phone_number="whatsapp:+10000000005",
            scheduled_datetime=timezone.now(),
        )
        refresh_lead_score(appointment)
        appointment.refresh_from_db()
        self.assertEqual(appointment.lead_score, 100)
        self.assertEqual(appointment.lead_status, LeadStatus.VERY_HOT)
        self.assertFalse(appointment.chatbot_paused)


class MessagingWindowTests(TestCase):
    """The old Command._send_followup_message tests rotted when the send path
    was refactored; the window rule itself now lives on the model (and the
    cadence logic is pinned in tests/test_bot_responses.py TEST 0)."""

    def test_window_open_inside_24_hours(self):
        lead = Appointment.objects.create(
            phone_number='whatsapp:+10000000006',
            last_customer_response=timezone.now() - timedelta(hours=2),
        )
        self.assertTrue(lead.messaging_window_open)

    def test_window_closed_after_24_hours(self):
        lead = Appointment.objects.create(
            phone_number='whatsapp:+10000000007',
            last_customer_response=timezone.now() - timedelta(days=2),
        )
        self.assertFalse(lead.messaging_window_open)


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


class SendPdfToLeadTests(TestCase):
    def setUp(self):
        from .models import Tenant, TenantMembership
        self.user = get_user_model().objects.create_user(
            username='pdf-sender',
            password='testpass123',
            is_staff=True,
        )
        TenantMembership.objects.create(
            user=self.user, tenant=Tenant.objects.get(slug='homebase'), role='staff')
        self.client.force_login(self.user)
        self.appointment = Appointment.objects.create(
            phone_number='whatsapp:+10000000009',
            customer_name='Test Customer',
        )

    @patch('bot.views.followups.whatsapp_api.send_local_document')
    def test_staff_can_send_pdf_from_appointment_detail(self, mock_send_document):
        document = SimpleUploadedFile(
            'site-plan.pdf',
            b'%PDF-1.4\n%%EOF\n',
            content_type='application/pdf',
        )

        response = self.client.post(
            reverse('send_pdf_to_lead', args=[self.appointment.pk]),
            {'document': document, 'caption': 'Site plan'},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.url, reverse('appointment_detail', args=[self.appointment.pk]))
        mock_send_document.assert_called_once()

        args = mock_send_document.call_args.args
        kwargs = mock_send_document.call_args.kwargs
        self.assertEqual(args[0], '10000000009')
        self.assertTrue(args[1].endswith('.pdf'))
        self.assertFalse(os.path.exists(args[1]))
        self.assertEqual(kwargs['caption'], 'Site plan')
        self.assertEqual(kwargs['filename'], 'site-plan.pdf')

        self.appointment.refresh_from_db()
        self.assertIsNotNone(self.appointment.last_outbound_at)
        self.assertEqual(
            self.appointment.conversation_history[-1]['content'],
            '[PDF SENT] site-plan.pdf | Caption: Site plan',
        )

    @patch('bot.views.followups.whatsapp_api.send_local_document')
    def test_non_pdf_upload_is_rejected(self, mock_send_document):
        document = SimpleUploadedFile(
            'notes.txt',
            b'hello',
            content_type='text/plain',
        )

        response = self.client.post(
            reverse('send_pdf_to_lead', args=[self.appointment.pk]),
            {'document': document},
        )

        self.assertEqual(response.status_code, 302)
        mock_send_document.assert_not_called()

        self.appointment.refresh_from_db()
        self.assertEqual(self.appointment.conversation_history, [])
        self.assertIsNone(self.appointment.last_outbound_at)
