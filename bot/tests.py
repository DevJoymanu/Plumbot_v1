from django.test import TestCase
from django.utils import timezone

from .models import Appointment, LeadStatus
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
