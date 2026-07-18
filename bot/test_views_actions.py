"""
View-level regression suite for the staff dashboard.

Two layers:

1. ``PageSmokeTests`` — GET every staff page (including the filter/tab/
   pagination variants that changed rendering paths) and assert it renders
   without a server error. This layer catches template-time crashes like the
   2026-07-13 production 500 on /conversations/ (EmptyPage raised by
   ``previous_page_number`` on page 1).

2. Action tests — POST every mutating dashboard action and assert the
   database effect. All outbound (WhatsApp / DeepSeek / plumber alerts) is
   mocked; the suite never talks to the network.

Run everything:      python manage.py test bot
Run just this file:  python manage.py test bot.test_views_actions

settings.py switches to an in-memory SQLite DB + local file storage when
'test' is in sys.argv, so the suite never touches the production database
or the R2 bucket and runs fully offline.
"""

import os
import unittest
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Appointment,
    Job,
    Quotation,
    QuotationTemplate,
    ScheduledFollowup,
    ScheduledReminder,
    Tenant,
    TenantMembership,
    TenantProfile,
    get_default_tenant_id,
)


def make_lead(suffix, **kwargs):
    """A minimal lead; suffix keeps phone numbers unique per test."""
    defaults = {'phone_number': f'whatsapp:+1555000{suffix:04d}'}
    defaults.update(kwargs)
    return Appointment.objects.create(**defaults)


class StaffClientTestCase(TestCase):
    """Logged-in staff client, shared by every test class below. Staff needs
    an explicit homebase membership since the admin/homebase separation —
    mirroring migration 0056's backfill for real users."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='staff-tester', password='pass12345', is_staff=True,
        )
        TenantMembership.objects.create(
            user=self.user, tenant=Tenant.objects.get(slug='homebase'), role='staff')
        self.client.force_login(self.user)


# ======================================================================
# 1. Page smoke tests — every staff page must render, in every variant
# ======================================================================

class PageSmokeTests(StaffClientTestCase):
    @classmethod
    def setUpTestData(cls):
        now = timezone.now()
        cls.lead = Appointment.objects.create(
            phone_number='whatsapp:+15559990001',
            customer_name='Smoke Lead',
            customer_area='Hatfield',
            project_type='bathroom_renovation',
            project_description='Full bathroom install',
            scheduled_datetime=now + timedelta(days=1),
            last_customer_response=now,
            conversation_history=[
                {'role': 'user', 'content': 'Hi, I need a plumber',
                 'timestamp': now.isoformat()},
                {'role': 'assistant', 'content': 'Hello, how may we assist you',
                 'timestamp': now.isoformat()},
            ],
        )
        cls.job = Appointment.objects.create(
            phone_number='whatsapp:+15559990002',
            customer_name='Job Lead',
            appointment_type='job_appointment',
            job_scheduled_datetime=now + timedelta(days=2),
        )
        # Enough recent leads that /conversations/ paginates (20 per page).
        for i in range(25):
            Appointment.objects.create(
                phone_number=f'whatsapp:+1555100{i:04d}',
                customer_name=f'Bulk Lead {i}',
            )
        cls.quote = Quotation.objects.create(appointment=cls.lead)
        cls.template = QuotationTemplate.objects.create(name='Standard Bathroom')

    def test_core_pages_render(self):
        """Every core staff page returns 200 in each meaningful variant."""
        conversations = reverse('conversations_list')
        detail = reverse('appointment_detail', args=[self.lead.pk])
        pages = [
            reverse('dashboard'),
            conversations,
            conversations + '?status_filter=booked',
            conversations + '?status_filter=pending',
            conversations + '?status_filter=cancelled',
            conversations + '?status_filter=delayed',
            # The 2026-07-13 production 500: paginated list, first page.
            conversations + '?response_age=all',
            conversations + '?response_age=all&page=1',
            conversations + '?response_age=all&page=2',
            reverse('conversation_detail', args=[self.lead.pk]),
            reverse('appointments_list'),
            reverse('priority_leads'),
            detail,
            detail + '?source=conversations&frame=1&hidetabs=1&tab=details',
            detail + '?source=priority_leads',
            detail + '?source=followups',
            detail + '?source=dashboard',
            reverse('appointment_detail', args=[self.job.pk]),
            reverse('appointment_documents', args=[self.lead.pk]),
            reverse('job_appointments_list'),
            reverse('calendar'),
            reverse('quotations_list'),
            reverse('view_quotation', args=[self.quote.pk]),
            reverse('edit_quotation', args=[self.quote.pk]),
            reverse('create_quotation', args=[self.lead.pk]),
            reverse('quotation_templates_list'),
            reverse('quotation_template_detail', args=[self.template.pk]),
            reverse('create_quotation_template'),
            reverse('edit_quotation_template', args=[self.template.pk]),
            reverse('followup_dashboard'),
            reverse('profile'),
            reverse('change_password'),
            # GET on these renders their confirm pages.
            reverse('mark_lead_inactive', args=[self.lead.pk]),
            reverse('reactivate_lead', args=[self.lead.pk]),
        ]
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(
                    response.status_code, 200,
                    f'{url} returned {response.status_code}',
                )

    def test_secondary_pages_do_not_error(self):
        """Tool/settings pages may redirect or reject the method, but a
        server error is always a regression."""
        pages = [
            reverse('settings'),
            reverse('calendar_settings'),
            reverse('ai_settings'),
            reverse('standalone_quotation'),
            reverse('test_whatsapp'),
            reverse('send_bulk_followup'),
            reverse('followup_test_suite'),
            reverse('complete_site_visit', args=[self.lead.pk]),
            reverse('schedule_job', args=[self.lead.pk]),
            reverse('reschedule_job', args=[self.job.pk]),
            reverse('lead_email_preview', args=[self.lead.pk]),
            reverse('lead_email_edit_data', args=[self.lead.pk]),
        ]
        for url in pages:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertLess(
                    response.status_code, 500,
                    f'{url} returned {response.status_code}',
                )

    def test_export_appointments_returns_csv(self):
        response = self.client.get(reverse('export_appointments'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('csv', response['Content-Type'])

    def test_pages_require_staff_login(self):
        """Anonymous requests never see staff pages."""
        self.client.logout()
        for name, args in [
            ('dashboard', []),
            ('conversations_list', []),
            ('appointment_detail', [self.lead.pk]),
            ('followup_dashboard', []),
        ]:
            with self.subTest(page=name):
                response = self.client.get(reverse(name, args=args))
                self.assertIn(response.status_code, (302, 403))

    def test_glance_card_shows_job_datetime_for_job_appointments(self):
        """Job appointments read job_scheduled_datetime (appointment_type is
        'job_appointment', NOT 'job' — regression for the glance hero)."""
        response = self.client.get(reverse('appointment_detail', args=[self.job.pk]))
        self.assertContains(response, 'Job appointment')
        local = timezone.localtime(self.job.job_scheduled_datetime)
        self.assertContains(response, local.strftime('%H:%M'))


# ======================================================================
# 2. Appointment lifecycle actions
# ======================================================================

class AppointmentLifecycleActionTests(StaffClientTestCase):
    def setUp(self):
        super().setUp()
        self.lead = make_lead(1, customer_name='Action Lead')

    def detail_url(self):
        return reverse('appointment_detail', args=[self.lead.pk])

    def test_detail_post_updates_fields(self):
        response = self.client.post(self.detail_url(), {
            'customer_name': 'Updated Name',
            'project_type': 'bathroom_renovation',
            'property_type': 'house',
            'customer_area': 'Avondale',
            'project_description': 'Install a wall-hung toilet',
            'customer_email': 'lead@example.com',
            'follow_up_status': 'in_progress',
            'admin_notes': 'note from test',
        })
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.customer_name, 'Updated Name')
        self.assertEqual(self.lead.customer_area, 'Avondale')
        self.assertEqual(self.lead.customer_email, 'lead@example.com')
        self.assertEqual(self.lead.follow_up_status, 'in_progress')

    def test_plan_upload_sets_plan_state(self):
        """The glance-card plan form: upload sets the file + plan flags and
        must not touch any other field."""
        self.lead.customer_name = 'Keep Me'
        self.lead.save(update_fields=['customer_name'])
        response = self.client.post(self.detail_url(), {
            'plan_file': SimpleUploadedFile(
                'plan.pdf', b'%PDF-1.4 test', content_type='application/pdf'),
        })
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.plan_file)
        self.assertTrue(self.lead.has_plan)
        self.assertEqual(self.lead.plan_status, 'plan_uploaded')
        self.assertIsNotNone(self.lead.plan_uploaded_at)
        self.assertEqual(self.lead.customer_name, 'Keep Me')
        # The uploaded plan is index 0 — the View plan links depend on this.
        files = self.lead.get_all_uploaded_files()
        self.assertTrue(files and str(self.lead.plan_file) in str(files[0]))

    def test_serve_and_download_plan_document(self):
        self.client.post(self.detail_url(), {
            'plan_file': SimpleUploadedFile(
                'plan.pdf', b'%PDF-1.4 test', content_type='application/pdf'),
        })
        view = self.client.get(
            reverse('appointment_document_file', args=[self.lead.pk, 0]))
        self.assertEqual(view.status_code, 200)
        download = self.client.get(
            reverse('appointment_document_file', args=[self.lead.pk, 0]) + '?dl=1')
        self.assertEqual(download.status_code, 200)

    @patch('bot.views.plumbot.base.Plumbot')
    def test_confirm_marks_confirmed_and_sends_confirmation(self, mock_plumbot):
        """Regression: Plumbot was never imported in appointments.py, so the
        Confirm button's WhatsApp confirmation NameError'd and was silently
        swallowed by the bare except — no confirmation ever went out."""
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=1)
        self.lead.save(update_fields=['scheduled_datetime'])
        response = self.client.get(
            reverse('confirm_appointment', args=[self.lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'confirmed')
        mock_plumbot.return_value.send_confirmation_message.assert_called_once()

    @patch('bot.views.plumbot.base.Plumbot')
    def test_confirm_without_datetime_sends_nothing(self, mock_plumbot):
        response = self.client.get(
            reverse('confirm_appointment', args=[self.lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'confirmed')
        mock_plumbot.assert_not_called()

    def test_unbook_returns_to_pending(self):
        self.lead.status = 'confirmed'
        self.lead.chatbot_paused = True
        self.lead.is_lead_active = False
        self.lead.save()
        response = self.client.get(reverse('unbook_appointment', args=[self.lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'pending')
        self.assertFalse(self.lead.chatbot_paused)
        self.assertTrue(self.lead.is_lead_active)

    def test_cancel_appointment(self):
        response = self.client.get(reverse('cancel_appointment', args=[self.lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'cancelled')

    def test_complete_lead_requires_post_and_completes(self):
        rejected = self.client.get(
            reverse('complete_lead_appointment', args=[self.lead.pk]))
        self.assertEqual(rejected.status_code, 405)
        response = self.client.post(
            reverse('complete_lead_appointment', args=[self.lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'completed')
        self.assertEqual(self.lead.follow_up_status, 'completed')
        self.assertFalse(self.lead.is_lead_active)

    def test_pause_and_resume_chatbot(self):
        response = self.client.post(reverse('pause_chatbot', args=[self.lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.chatbot_paused)
        self.assertIn('[DELAY_SIGNAL]', self.lead.internal_notes or '')

        response = self.client.post(reverse('resume_chatbot', args=[self.lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.chatbot_paused)
        self.assertNotIn('[DELAY_SIGNAL]', self.lead.internal_notes or '')

    def test_mark_inactive_and_reactivate(self):
        response = self.client.post(
            reverse('mark_lead_inactive', args=[self.lead.pk]),
            {'reason': 'manual'},
        )
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.is_lead_active)

        response = self.client.post(reverse('reactivate_lead', args=[self.lead.pk]))
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertTrue(self.lead.is_lead_active)
        self.assertIsNone(self.lead.lead_marked_inactive_at)


# ======================================================================
# 3. Follow-up / messaging actions (outbound fully mocked)
# ======================================================================

class FollowupActionTests(StaffClientTestCase):
    def setUp(self):
        super().setUp()
        self.lead = make_lead(
            2,
            customer_name='Followup Lead',
            last_customer_response=timezone.now(),
        )

    @patch('bot.views.followups.whatsapp_api.send_text_message')
    def test_send_manual_followup(self, mock_send):
        response = self.client.post(
            reverse('send_followup', args=[self.lead.pk]),
            {'message': 'Hi {name}, checking in.'},
        )
        self.assertEqual(response.status_code, 302)
        mock_send.assert_called_once()
        sent_text = mock_send.call_args.args[1]
        self.assertIn('Followup Lead', sent_text)  # {name} personalised
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.followup_count, 1)
        self.assertIn('[MANUAL FOLLOW-UP]',
                      self.lead.conversation_history[-1]['content'])

    @patch('bot.views.followups.whatsapp_api.send_text_message')
    def test_send_manual_followup_rejects_empty_message(self, mock_send):
        response = self.client.post(
            reverse('send_followup', args=[self.lead.pk]), {'message': '  '})
        self.assertEqual(response.status_code, 302)
        mock_send.assert_not_called()

    def test_schedule_edit_and_cancel_whatsapp_followup(self):
        response = self.client.post(
            reverse('schedule_followup', args=[self.lead.pk]),
            {'channel': 'whatsapp', 'scheduled_for': '2030-01-01T10:00',
             'message': 'Hi {name}'},
        )
        self.assertEqual(response.status_code, 302)
        sf = ScheduledFollowup.objects.get(appointment=self.lead)
        self.assertEqual(sf.status, 'pending')
        self.assertEqual(sf.channel, 'whatsapp')

        response = self.client.post(
            reverse('edit_scheduled_followup', args=[sf.pk]),
            {'scheduled_for': '2030-02-02T12:30', 'message': 'Updated text'},
        )
        self.assertEqual(response.status_code, 302)
        sf.refresh_from_db()
        self.assertEqual(sf.message, 'Updated text')

        response = self.client.post(
            reverse('cancel_scheduled_followup', args=[sf.pk]))
        self.assertEqual(response.status_code, 302)
        sf.refresh_from_db()
        self.assertIn(sf.status, ('cancelled',))

    def test_schedule_edit_and_cancel_reminder(self):
        response = self.client.post(
            reverse('schedule_reminder', args=[self.lead.pk]),
            {'target': 'plumber', 'channel': 'email',
             'scheduled_for': '2030-01-01T09:00', 'subject': 'Bring fittings',
             'message': 'Geyser fittings for {name}'},
        )
        self.assertEqual(response.status_code, 302)
        reminder = ScheduledReminder.objects.get(appointment=self.lead)
        self.assertEqual(reminder.target, 'plumber')
        self.assertEqual(reminder.status, 'pending')

        response = self.client.post(
            reverse('edit_scheduled_reminder', args=[reminder.pk]),
            {'scheduled_for': '2030-03-03T09:00', 'subject': 'Updated',
             'message': 'Updated body'},
        )
        self.assertEqual(response.status_code, 302)
        reminder.refresh_from_db()
        self.assertEqual(reminder.subject, 'Updated')

        response = self.client.post(
            reverse('cancel_scheduled_reminder', args=[reminder.pk]))
        self.assertEqual(response.status_code, 302)
        reminder.refresh_from_db()
        self.assertIn(reminder.status, ('cancelled',))

    def test_update_followup_schedule(self):
        response = self.client.post(
            reverse('update_followup_schedule', args=[self.lead.pk]),
            {'next_follow_up_at': '2030-01-05T15:00',
             'follow_up_status': 'waiting_customer'},
        )
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.follow_up_status, 'waiting_customer')
        self.assertIsNotNone(self.lead.next_follow_up_at)

    def test_update_lead_email(self):
        response = self.client.post(
            reverse('update_lead_email', args=[self.lead.pk]),
            {'customer_email': 'new@example.com'},
        )
        self.assertEqual(response.status_code, 302)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.customer_email, 'new@example.com')

    def test_pause_and_resume_auto_followup_endpoints_respond(self):
        for name in ('pause_auto_followup', 'resume_auto_followup'):
            with self.subTest(action=name):
                response = self.client.post(
                    reverse(name, args=[self.lead.pk]),
                    {'pause_duration': 'permanent'},
                )
                self.assertEqual(response.status_code, 302)

    @unittest.expectedFailure
    def test_pause_auto_followup_actually_persists(self):
        """KNOWN DEAD FEATURE: pause_auto_followup writes
        manual_followup_paused / manual_followup_paused_until, but those
        fields were REMOVED in migration 0018 — the view sets plain Python
        attributes that save() never persists, so the 'Pause auto follow-ups'
        button does nothing. Kept as an expectedFailure so the suite starts
        failing loudly the day someone re-adds the fields (then promote this
        to a real test and wire send_followups eligibility to honour it)."""
        self.client.post(
            reverse('pause_auto_followup', args=[self.lead.pk]),
            {'pause_duration': 'permanent'},
        )
        self.lead.refresh_from_db()
        self.assertTrue(getattr(self.lead, 'manual_followup_paused', False))

    @patch('bot.views.followups.whatsapp_api.send_media_message')
    def test_send_image_to_lead(self, mock_send):
        response = self.client.post(
            reverse('send_image_to_lead', args=[self.lead.pk]),
            {'image_url': 'https://example.com/pic.jpg', 'caption': 'Our work'},
        )
        self.assertEqual(response.status_code, 302)
        mock_send.assert_called_once()
        self.lead.refresh_from_db()
        self.assertIn('[IMAGE SENT]',
                      self.lead.conversation_history[-1]['content'])


# ======================================================================
# 4. Quotation & template actions
# ======================================================================

class QuotationActionTests(StaffClientTestCase):
    def setUp(self):
        super().setUp()
        self.lead = make_lead(3, customer_name='Quote Lead')
        self.quote = Quotation.objects.create(appointment=self.lead)
        self.template = QuotationTemplate.objects.create(name='Geyser Swap')

    def test_duplicate_quotation(self):
        response = self.client.post(
            reverse('duplicate_quotation', args=[self.quote.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.lead.quotations.count(), 2)

    def test_delete_quotation(self):
        response = self.client.post(
            reverse('delete_quotation', args=[self.quote.pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Quotation.objects.filter(pk=self.quote.pk).exists())

    def test_duplicate_and_delete_reject_get(self):
        self.assertEqual(
            self.client.get(reverse('duplicate_quotation', args=[self.quote.pk])).status_code, 405)
        self.assertEqual(
            self.client.get(reverse('delete_quotation', args=[self.quote.pk])).status_code, 405)
        self.assertTrue(Quotation.objects.filter(pk=self.quote.pk).exists())

    def test_toggle_template_status(self):
        initial = self.template.is_active
        response = self.client.post(
            reverse('toggle_template_status', args=[self.template.pk]))
        self.assertLess(response.status_code, 500)
        self.template.refresh_from_db()
        self.assertEqual(self.template.is_active, not initial)

    def test_use_template_creates_quotation_for_appointment(self):
        before = self.lead.quotations.count()
        response = self.client.get(
            reverse('use_template_for_appointment',
                    args=[self.template.pk, self.lead.pk]))
        self.assertLess(response.status_code, 500)
        self.assertGreater(self.lead.quotations.count(), before)


# ======================================================================
# Tenant isolation (Phase 0 — docs/MULTI_TENANT_PLAN.md §6, §9)
# Two tenants in-memory; assert for_tenant() never leaks a row across,
# and that untagged writes resolve to the homebase seed when it exists.
# ======================================================================

class TenantIsolationTests(TestCase):
    """The non-negotiable isolation rules, pinned before any view is scoped."""

    def setUp(self):
        # get_or_create: the test-DB post_migrate hook (bot/apps.py) already
        # seeds homebase, mirroring migration 0041 on real databases.
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.hb_lead = make_lead(9001, tenant=self.homebase)
        self.hb_lead2 = make_lead(9002, tenant=self.homebase)
        self.acme_lead = make_lead(9003, tenant=self.acme)

    def test_for_tenant_returns_only_own_rows(self):
        hb = Appointment.objects.for_tenant(self.homebase)
        acme = Appointment.objects.for_tenant(self.acme)
        self.assertEqual(set(hb), {self.hb_lead, self.hb_lead2})
        self.assertEqual(set(acme), {self.acme_lead})

    def test_for_tenant_zero_cross_leakage(self):
        self.assertFalse(
            Appointment.objects.for_tenant(self.acme).filter(pk=self.hb_lead.pk).exists())
        self.assertFalse(
            Appointment.objects.for_tenant(self.homebase).filter(pk=self.acme_lead.pk).exists())

    def test_for_tenant_composes_with_existing_scopes(self):
        # .real() / .test_lines() must stack with tenant scoping
        test_line = Appointment.objects.create(
            phone_number='whatsapp:+9990001111', tenant=self.acme)
        self.assertEqual(
            set(Appointment.objects.for_tenant(self.acme).real()), {self.acme_lead})
        self.assertEqual(
            set(Appointment.objects.for_tenant(self.acme).test_lines()), {test_line})

    def test_untagged_write_defaults_to_homebase_seed(self):
        # Pre-Phase-1 code paths create rows without passing a tenant; the FK
        # default must resolve them to the homebase seed, never leave orphans.
        lead = make_lead(9004)
        self.assertEqual(lead.tenant_id, self.homebase.pk)

    def test_untagged_write_fails_loudly_without_seed(self):
        # Non-null FK (Phase 0.2): with no homebase seed, an untagged write
        # must ERROR, never produce an ownerless row (nullability rule: no
        # silent fallbacks for business ownership).
        from django.db import IntegrityError, transaction
        Appointment.objects.all().delete()
        Tenant.objects.all().delete()
        self.assertIsNone(get_default_tenant_id())
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                make_lead(9005)

    def test_same_customer_two_tenants_is_two_leads(self):
        # Decision #1: phone uniqueness is per-tenant, not global.
        phone = 'whatsapp:+15550009900'
        a = Appointment.objects.create(phone_number=phone, tenant=self.homebase)
        b = Appointment.objects.create(phone_number=phone, tenant=self.acme)
        self.assertNotEqual(a.pk, b.pk)
        self.assertEqual(Appointment.objects.for_tenant(self.homebase).filter(
            phone_number=phone).count(), 1)
        self.assertEqual(Appointment.objects.for_tenant(self.acme).filter(
            phone_number=phone).count(), 1)

    def test_tenant_delete_is_protected(self):
        # PROTECT on purpose: deleting a tenant must never cascade leads away.
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            self.acme.delete()

    def test_membership_roles_and_uniqueness(self):
        user = get_user_model().objects.create_user(username='owner1', password='x')
        TenantMembership.objects.create(user=user, tenant=self.acme, role='owner')
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                TenantMembership.objects.create(user=user, tenant=self.acme, role='staff')

    def test_profile_is_fully_optional(self):
        # Nullability rule: a bare profile must be creatable with zero facts.
        profile = TenantProfile.objects.create(tenant=self.acme)
        self.assertEqual(profile.plumber_name, '')
        self.assertFalse(profile.licensed_claim_enabled)
        self.assertEqual(profile.excluded_areas, [])


class TenantSwitcherTests(TestCase):
    """The Phase-0 platform console: superuser-only session tenant switch."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')

    def test_superuser_can_switch_and_middleware_pins_it(self):
        get_user_model().objects.create_superuser(
            username='root', password='pass12345', email='root@example.com')
        self.client.login(username='root', password='pass12345')
        response = self.client.post(
            reverse('switch_tenant'), {'tenant': 'acme', 'next': '/dashboard/'})
        self.assertEqual(response.status_code, 302)
        follow = self.client.get(reverse('dashboard'))
        self.assertEqual(follow.wsgi_request.tenant, self.acme)

    def test_staff_cannot_switch(self):
        get_user_model().objects.create_user(
            username='plainstaff', password='pass12345', is_staff=True)
        self.client.login(username='plainstaff', password='pass12345')
        response = self.client.post(reverse('switch_tenant'), {'tenant': 'acme'})
        self.assertIn(response.status_code, (302, 403))  # redirected to login, never applied
        follow = self.client.get(reverse('dashboard'))
        self.assertNotEqual(getattr(follow.wsgi_request, 'tenant', None), self.acme)

    def test_membership_pins_tenant_for_staff(self):
        user = get_user_model().objects.create_user(
            username='acmestaff', password='pass12345', is_staff=True)
        TenantMembership.objects.create(user=user, tenant=self.acme, role='staff')
        self.client.login(username='acmestaff', password='pass12345')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.wsgi_request.tenant, self.acme)

    def test_no_membership_is_blocked_not_homebase(self):
        # Homebase/admin separation: a staff login with NO membership must be
        # clearly blocked — never silently dropped into homebase's data.
        get_user_model().objects.create_user(
            username='legacystaff', password='pass12345', is_staff=True)
        self.client.login(username='legacystaff', password='pass12345')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 403)
        self.assertIn('No workspace assigned', response.content.decode())
        # Public/auth surfaces stay reachable.
        self.assertLess(self.client.get('/logout/').status_code, 400)

    def test_superuser_defaults_to_homebase_lens(self):
        get_user_model().objects.create_superuser(
            username='root2', password='pass12345', email='r2@example.com')
        self.client.login(username='root2', password='pass12345')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.wsgi_request.tenant, self.homebase)


class TenantViewScopingTests(TestCase):
    """Phase 3.1: every staff view is tenant-scoped. An acme staff member
    sees only acme's leads; homebase objects 404 (never 403 — §6.3)."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.hb_lead = make_lead(9801, tenant=self.homebase, customer_name='HB Lead')
        self.acme_lead = make_lead(9802, tenant=self.acme, customer_name='Acme Lead')
        user = get_user_model().objects.create_user(
            username='acme-staff', password='pass12345', is_staff=True)
        TenantMembership.objects.create(user=user, tenant=self.acme, role='staff')
        self.client.login(username='acme-staff', password='pass12345')

    def test_lists_show_only_own_tenant(self):
        for name in ('dashboard', 'conversations_list', 'appointments_list'):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertEqual(response.status_code, 200)
                body = response.content.decode()
                self.assertNotIn('HB Lead', body, name)

    def test_foreign_detail_views_404(self):
        for name in ('appointment_detail', 'conversation_detail', 'update_appointment'):
            with self.subTest(view=name):
                response = self.client.get(reverse(name, args=[self.hb_lead.pk]))
                self.assertEqual(response.status_code, 404)

    def test_own_detail_still_renders(self):
        response = self.client.get(reverse('appointment_detail', args=[self.acme_lead.pk]))
        self.assertEqual(response.status_code, 200)

    def test_foreign_action_views_404(self):
        response = self.client.post(reverse('confirm_appointment', args=[self.hb_lead.pk]))
        self.assertEqual(response.status_code, 404)
        response = self.client.post(reverse('cancel_appointment', args=[self.hb_lead.pk]))
        self.assertEqual(response.status_code, 404)

    def test_child_records_inherit_lead_tenant(self):
        # Dashboard-created children belong to the lead's tenant, never the
        # homebase default (Phase 3.1 _inherit_tenant).
        quote = Quotation.objects.create(appointment=self.acme_lead)
        self.assertEqual(quote.tenant_id, self.acme.pk)
        followup = ScheduledFollowup.objects.create(
            appointment=self.acme_lead, channel='whatsapp',
            scheduled_for=timezone.now() + timedelta(days=1))
        self.assertEqual(followup.tenant_id, self.acme.pk)
        job = Job.objects.create(
            site_visit=self.acme_lead, scheduled_datetime=timezone.now(),
            description='x', status='scheduled')
        self.assertEqual(job.tenant_id, self.acme.pk)


class PlatformConsoleTests(TestCase):
    """Phase 3.2: superuser-only operator console — list, create, toggle,
    config editor. Plain staff never get in."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.root = get_user_model().objects.create_superuser(
            username='root', password='pass12345', email='root@example.com')
        self.client.login(username='root', password='pass12345')

    def test_console_lists_tenants(self):
        response = self.client.get(reverse('platform_console'))
        self.assertEqual(response.status_code, 200)
        self.assertIn('Homebase Plumbers', response.content.decode())

    def test_staff_cannot_access_console(self):
        get_user_model().objects.create_user(
            username='plainstaff2', password='pass12345', is_staff=True)
        self.client.login(username='plainstaff2', password='pass12345')
        for name, args in [('platform_console', []),
                           ('platform_tenant_config', ['homebase'])]:
            response = self.client.get(reverse(name, args=args))
            self.assertIn(response.status_code, (302, 403), name)

    def test_create_tenant_with_blank_profile(self):
        response = self.client.post(reverse('platform_create_tenant'),
                                    {'name': 'Acme Plumbing'})
        self.assertEqual(response.status_code, 302)
        tenant = Tenant.objects.get(slug='acme-plumbing')
        self.assertTrue(TenantProfile.objects.filter(tenant=tenant).exists())
        # Blank profile = nullability rule: no facts, no claims.
        self.assertEqual(tenant.profile.plumber_name, '')

    def test_duplicate_tenant_name_is_friendly_not_500(self):
        # Prod 2026-07-16: creating 'John Deo' twice 500'd on the name unique
        # constraint — the view only checked the slug.
        self.client.post(reverse('platform_create_tenant'), {'name': 'John Deo'})
        response = self.client.post(reverse('platform_create_tenant'),
                                    {'name': 'john deo', 'slug': 'john-deo-2'})
        self.assertEqual(response.status_code, 302)  # friendly redirect, no crash
        self.assertEqual(Tenant.objects.filter(name__iexact='john deo').count(), 1)

    def test_toggle_tenant_but_never_homebase_off(self):
        acme = Tenant.objects.create(name='Acme', slug='acme')
        self.client.post(reverse('platform_toggle_tenant', args=['acme']))
        acme.refresh_from_db()
        self.assertFalse(acme.is_active)
        self.client.post(reverse('platform_toggle_tenant', args=['homebase']))
        self.homebase.refresh_from_db()
        self.assertTrue(self.homebase.is_active)  # refused

    def test_delete_tenant_password_gated(self):
        from .models import TenantMembership, TestScenario
        acme = Tenant.objects.create(name='Doomed Plumbing', slug='doomed')
        lead = make_lead(9901, tenant=acme)
        TestScenario.objects.create(tenant=acme, name='doomed sc', content='> hi\nexpect: x')
        staff = get_user_model().objects.create_user(
            username='doomedstaff', password='pass12345', is_staff=True)
        TenantMembership.objects.create(user=staff, tenant=acme, role='staff')

        # Wrong password → nothing deleted.
        response = self.client.post(reverse('platform_delete_tenant', args=['doomed']),
                                    {'delete_password': 'nope'})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Tenant.objects.filter(slug='doomed').exists())
        self.assertTrue(Appointment.objects.filter(pk=lead.pk).exists())

        # Right password → tenant + business data gone; orphan staff deactivated.
        from .views.platform import PLATFORM_DELETE_PASSWORD
        self.client.post(reverse('platform_delete_tenant', args=['doomed']),
                         {'delete_password': PLATFORM_DELETE_PASSWORD})
        self.assertFalse(Tenant.objects.filter(slug='doomed').exists())
        self.assertFalse(Appointment.objects.filter(pk=lead.pk).exists())
        self.assertFalse(TestScenario.objects.filter(name='doomed sc').exists())
        staff.refresh_from_db()
        self.assertFalse(staff.is_active)

        # Homebase is never deletable, even with the right password.
        self.client.post(reverse('platform_delete_tenant', args=['homebase']),
                         {'delete_password': PLATFORM_DELETE_PASSWORD})
        self.assertTrue(Tenant.objects.filter(slug='homebase').exists())

    def test_staff_login_lifecycle(self):
        # Checklist 6.6: create login → member sees own tenant; deactivate
        # blocks login; reset password works; superusers never managed here.
        from .models import TenantMembership
        acme = Tenant.objects.create(name='Acme', slug='acme')
        response = self.client.post(reverse('platform_add_staff', args=['acme']), {
            'username': 'acmeboss', 'email': 'boss@acme.test',
            'password': 'trustno1!', 'role': 'owner'})
        self.assertEqual(response.status_code, 302)
        user = get_user_model().objects.get(username='acmeboss')
        self.assertTrue(user.is_staff)
        self.assertFalse(user.is_superuser)
        membership = TenantMembership.objects.get(user=user)
        self.assertEqual((membership.tenant, membership.role), (acme, 'owner'))
        # New login works and lands in their own tenant.
        client2 = self.client.__class__()
        self.assertTrue(client2.login(username='acmeboss', password='trustno1!'))
        self.assertEqual(client2.get(reverse('dashboard')).wsgi_request.tenant, acme)
        # Deactivate blocks login.
        self.client.post(reverse('platform_toggle_staff', args=['acme', user.pk]))
        self.assertFalse(client2.login(username='acmeboss', password='trustno1!'))
        # Reactivate + reset password.
        self.client.post(reverse('platform_toggle_staff', args=['acme', user.pk]))
        self.client.post(reverse('platform_reset_staff_password', args=['acme', user.pk]),
                         {'password': 'newpass99!'})
        self.assertTrue(client2.login(username='acmeboss', password='newpass99!'))
        # Duplicate username is friendly.
        response = self.client.post(reverse('platform_add_staff', args=['acme']), {
            'username': 'ACMEBOSS', 'password': 'whatever123'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(get_user_model().objects.filter(
            username__iexact='acmeboss').count(), 1)
        # Managing a superuser through this surface 404s.
        response = self.client.post(
            reverse('platform_toggle_staff', args=['acme', self.root.pk]))
        self.assertEqual(response.status_code, 404)

    def test_config_page_renders_and_saves(self):
        response = self.client.get(reverse('platform_tenant_config', args=['homebase']))
        self.assertEqual(response.status_code, 200)
        # Minimal valid POST: profile fields + empty price formset management.
        data = {
            'plumber_name': 'Takudzwa', 'plumber_contact': '+263774819901',
            'business_whatsapp': '+263776255077',
            'location_line': "We're in Hatfield, Harare.",
            'location_area': 'Hatfield', 'location_city': 'Harare',
            'business_hours': '{"days": "Sunday-Friday", "open": "08:00", "close": "18:00", "closed": ["sat"]}',
            'timezone_name': 'Africa/Johannesburg',
            'excluded_areas': '["gweru"]', 'currency': 'US$',
            'packages': '[]', 'faq_facts': '{}', 'scripts': '{}',
            'email_from_name': 'Takudzwa', 'email_sender': '',
            'form-TOTAL_FORMS': '0', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
        }
        response = self.client.post(
            reverse('platform_tenant_config', args=['homebase']), data)
        self.assertEqual(response.status_code, 302)
        profile = TenantProfile.objects.get(tenant=self.homebase)
        self.assertEqual(profile.excluded_areas, ['gweru'])


class TenantIntakeTests(TestCase):
    """Phase 3.3: owner intake — token form → draft → admin approve applies
    to the live config; nothing goes live unreviewed."""

    def setUp(self):
        from .models import TenantIntake
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.intake = TenantIntake.objects.create(tenant=self.acme)
        self.root = get_user_model().objects.create_superuser(
            username='root', password='pass12345', email='root@example.com')

    def _submit(self, extra=None):
        payload = {
            'plumber_name': 'Blessing', 'plumber_contact': '+263711111111',
            'location_area': 'Kwekwe', 'location_city': 'Kwekwe',
            'email_from_name': 'Blessing',
            'days': ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday'],
            'hours_open': '07:00', 'hours_close': '17:00',
            'excluded_areas': 'Harare',
            'payment': ['Cash (USD)', 'EcoCash'],
            'services': ['leak repairs', 'geyser install & repair'],
            'duration_small': 'under an hour', 'duration_big': 'a full day',
            'faq_free_quote': 'Yes — free visit, fixed price on the spot.',
            'price_label': ['Geyser supply & install', ''],
            'price_family': ['geyser', ''],
            'price_variant': ['', ''],
            'price_supply': ['90', ''], 'price_labour': ['60', ''],
            'price_allin': ['150', ''],
            'photos_meta': '[]',
        }
        payload.update(extra or {})
        return self.client.post(f'/intake/{self.intake.token}/', payload)

    def test_public_form_renders_by_token_and_404s_otherwise(self):
        response = self.client.get(f'/intake/{self.intake.token}/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/intake/not-a-token/').status_code, 404)

    def test_submission_is_draft_not_live(self):
        response = self._submit()
        self.assertEqual(response.status_code, 200)
        self.intake.refresh_from_db()
        self.assertEqual(self.intake.status, 'submitted')
        self.assertEqual(self.intake.data['profile']['plumber_name'], 'Blessing')
        # NOT applied to live config yet.
        profile = TenantProfile.objects.filter(tenant=self.acme).first()
        self.assertTrue(profile is None or profile.plumber_name == '')

    def test_approve_applies_everything(self):
        self._submit()
        self.client.login(username='root', password='pass12345')
        response = self.client.post(
            reverse('platform_review_intake', args=[self.intake.pk]),
            {'decision': 'approve'})
        self.assertEqual(response.status_code, 302)
        profile = TenantProfile.objects.get(tenant=self.acme)
        self.assertEqual(profile.plumber_name, 'Blessing')
        # Day chips + pickers composed into the canonical hours shape…
        self.assertEqual(profile.business_hours['days'], 'Monday-Saturday')
        self.assertEqual(profile.business_hours['open'], '07:00')
        self.assertEqual(profile.business_hours['closed'], ['sun'])
        # …and it renders through the bot's hour formatters.
        from .tenant_config import get_config
        cfg = get_config(self.acme)
        self.assertEqual(cfg.hours_sentence(), 'Monday to Saturday, 7:00 AM – 5:00 PM')
        self.assertEqual(profile.excluded_areas, ['harare'])
        # Structured answers composed into fact sentences.
        self.assertIn('Cash (USD)', profile.faq_facts['payment'])
        self.assertIn('EcoCash', profile.faq_facts['payment'])
        self.assertIn('leak repairs', profile.faq_facts['services'])
        self.assertIn('under an hour', profile.faq_facts['job_duration'])
        self.assertEqual(profile.faq_facts['free_quote'],
                         'Yes — free visit, fixed price on the spot.')
        from .models import TenantPriceItem
        item = TenantPriceItem.objects.get(tenant=self.acme, family='geyser', variant='')
        self.assertEqual(int(item.supply), 90)
        self.assertEqual(int(item.allin), 150)
        self.assertEqual(cfg.price_components().get('geyser'), (90, 60))

    def test_photo_upload_and_pairing(self):
        # Upload two photos via the endpoint, submit as a before/after pair,
        # approve → ONE portfolio item with pair_filename + tag keyword.
        import json as _json
        png = (b'\x89PNG\r\n\x1a\n' + b'0' * 64)
        paths = []
        for name in ('before.png', 'after.png'):
            response = self.client.post(
                f'/intake/{self.intake.token}/photo/',
                {'photo': SimpleUploadedFile(name, png, content_type='image/png')})
            body = response.json()
            self.assertTrue(body['ok'], body)
            paths.append(body['path'])
        self._submit({'photos_meta': _json.dumps([
            {'path': paths[0], 'tag': 'geyser', 'caption': '', 'pair_with_prev': False},
            {'path': paths[1], 'tag': 'geyser', 'caption': 'Geyser swap in Kwekwe',
             'pair_with_prev': True},
        ])})
        self.client.login(username='root', password='pass12345')
        self.client.post(reverse('platform_review_intake', args=[self.intake.pk]),
                         {'decision': 'approve'})
        from .models import TenantPortfolioItem
        items = list(TenantPortfolioItem.objects.filter(tenant=self.acme))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].filename, paths[1])       # after
        self.assertEqual(items[0].pair_filename, paths[0])  # before
        self.assertEqual(items[0].keywords, ['geyser'])
        self.assertEqual(items[0].title, 'Geyser swap in Kwekwe')

    def test_autosave_merges_draft_for_resume(self):
        response = self.client.post(f'/intake/{self.intake.token}/autosave/', {
            'plumber_name': 'Draft Guy', 'days': ['monday'],
            'hours_open': '08:00', 'hours_close': '17:00', 'photos_meta': '[]',
        })
        self.assertTrue(response.json()['ok'])
        self.intake.refresh_from_db()
        self.assertEqual(self.intake.status, 'pending')  # still a draft
        self.assertEqual(self.intake.data['profile']['plumber_name'], 'Draft Guy')
        # The form GET embeds the draft for resume.
        response = self.client.get(f'/intake/{self.intake.token}/')
        self.assertIn('Draft Guy', response.content.decode())

    def test_reject_applies_nothing(self):
        self._submit()
        self.client.login(username='root', password='pass12345')
        self.client.post(reverse('platform_review_intake', args=[self.intake.pk]),
                         {'decision': 'reject', 'review_note': 'numbers look off'})
        self.intake.refresh_from_db()
        self.assertEqual(self.intake.status, 'rejected')
        profile = TenantProfile.objects.filter(tenant=self.acme).first()
        self.assertTrue(profile is None or profile.plumber_name == '')

    def test_non_superuser_cannot_review(self):
        self._submit()
        get_user_model().objects.create_user(
            username='staff3', password='pass12345', is_staff=True)
        self.client.login(username='staff3', password='pass12345')
        response = self.client.post(
            reverse('platform_review_intake', args=[self.intake.pk]),
            {'decision': 'approve'})
        self.assertIn(response.status_code, (302, 403))
        self.intake.refresh_from_db()
        self.assertEqual(self.intake.status, 'submitted')  # untouched

    def test_closed_intake_shows_done_page(self):
        self._submit()
        self.intake.refresh_from_db()
        self.intake.status = 'approved'
        self.intake.save()
        response = self.client.get(f'/intake/{self.intake.token}/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('approved', response.content.decode().lower())


class ScenarioLabTenantTests(TestCase):
    """Phase 5: per-tenant Scenario Lab + golden-pack cloning."""

    def setUp(self):
        from .models import TestScenario
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        for i in range(3):
            TestScenario.objects.create(
                tenant=self.homebase, name=f'golden {i}', category='Pricing',
                content='> how much is a geyser\nexpect: US$',
            )
        self.root = get_user_model().objects.create_superuser(
            username='root', password='pass12345', email='root@example.com')
        self.client.login(username='root', password='pass12345')

    def test_create_tenant_clones_golden_pack(self):
        from .models import TestScenario
        self.client.post(reverse('platform_create_tenant'), {'name': 'Acme Plumbing'})
        acme = Tenant.objects.get(slug='acme-plumbing')
        cloned = TestScenario.objects.filter(tenant=acme)
        self.assertEqual(cloned.count(), 3)
        self.assertEqual(
            set(cloned.values_list('name', flat=True)),
            {'golden 0', 'golden 1', 'golden 2'})
        # Same names across tenants — per-tenant uniqueness holds.
        self.assertEqual(TestScenario.objects.filter(name='golden 0').count(), 2)

    def test_lab_shows_only_current_tenants_scenarios(self):
        from .models import TenantMembership, TestScenario
        acme = Tenant.objects.create(name='Acme', slug='acme')
        TestScenario.objects.create(
            tenant=acme, name='acme only', content='> hi\nexpect: hello')
        staff = get_user_model().objects.create_user(
            username='acmestaff5', password='pass12345', is_staff=True)
        TenantMembership.objects.create(user=staff, tenant=acme, role='staff')
        self.client.login(username='acmestaff5', password='pass12345')
        response = self.client.get(reverse('scenario_lab'))
        body = response.content.decode()
        self.assertIn('acme only', body)
        self.assertNotIn('golden 0', body)


class LeadSourceTests(TestCase):
    """Channel attribution: ad referrals are deterministic; everything else
    is inferred from the customer's own words and can upgrade later."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})

    def test_inference_patterns(self):
        cases = {
            'Hi, saw your post on Facebook about geysers': 'facebook',
            'found you on fb page': 'facebook',
            'I saw you on instagram': 'instagram',
            'googled plumbers in harare': 'google_search',
            'found you on google': 'google_search',
            'my friend told me about you': 'referral',
            'you were recommended to me': 'referral',
            'saw your whatsapp status': 'whatsapp_status',
            'got your flyer at the shops': 'flyer',
            'Hi, how much is a geyser?': '',
        }
        for message, expected in cases.items():
            with self.subTest(message=message):
                self.assertEqual(Appointment.infer_lead_source(message), expected)

    def test_first_message_tags_and_later_upgrades(self):
        lead = make_lead(9950, tenant=self.homebase)
        # First message, no signal → direct.
        lead.update_lead_source('Hi, how much is a geyser?', is_first_message=True)
        self.assertEqual(lead.lead_source, 'direct')
        # Later message reveals the source → upgrades.
        lead.update_lead_source('by the way I saw you on facebook')
        self.assertEqual(lead.lead_source, 'facebook')
        # A different signal later does NOT overwrite a real source.
        lead.update_lead_source('also my friend recommended you')
        self.assertEqual(lead.lead_source, 'facebook')

    def test_ad_referral_is_deterministic_and_wins(self):
        lead = make_lead(9951, tenant=self.homebase)
        lead.record_ctwa_referral({
            'source_type': 'ad', 'source_id': '123',
            'source_url': 'https://fb.me/somead'})
        self.assertEqual(lead.lead_source, 'facebook_ad')
        # Words can never downgrade ad attribution.
        lead.update_lead_source('my friend told me about you')
        self.assertEqual(lead.lead_source, 'facebook_ad')
        # Instagram ads are distinguished by the source URL.
        lead2 = make_lead(9952, tenant=self.homebase)
        lead2.record_ctwa_referral({
            'source_type': 'ad', 'source_id': '456',
            'source_url': 'https://instagram.com/somead'})
        self.assertEqual(lead2.lead_source, 'instagram_ad')


class SelfServiceAccountTests(TestCase):
    """Users manage their own username/password: profile rename (unique,
    logged) and the forgot-password email flow (HTTP transport, no
    enumeration, token round-trip)."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.user = get_user_model().objects.create_user(
            username='renameme', password='oldpass123', is_staff=True,
            email='me@example.test')
        TenantMembership.objects.create(user=self.user, tenant=self.homebase, role='staff')

    def test_username_change_and_uniqueness(self):
        get_user_model().objects.create_user(username='taken', password='x' * 10)
        self.client.login(username='renameme', password='oldpass123')
        # Taken name rejected (case-insensitive), original intact.
        self.client.post(reverse('profile'), {'username': 'TAKEN', 'email': 'me@example.test'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'renameme')
        # Fresh name accepted; next login uses it.
        self.client.post(reverse('profile'), {'username': 'newname', 'email': 'me@example.test'})
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, 'newname')
        self.client.logout()
        self.assertTrue(self.client.login(username='newname', password='oldpass123'))

    @patch('bot.plumber_notifications.send_email_to_recipients')
    def test_password_reset_round_trip(self, mock_send):
        # Request a link (by email this time).
        response = self.client.post(reverse('password_reset_request'),
                                    {'identifier': 'me@example.test'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(mock_send.called)
        plain_body = mock_send.call_args[0][2]
        import re as _re
        link = _re.search(r'(/reset/[^\s]+/)', plain_body).group(1)
        # Django's confirm view redirects to a set-password session URL.
        response = self.client.get(link, follow=True)
        self.assertEqual(response.status_code, 200)
        set_url = response.request['PATH_INFO']
        response = self.client.post(set_url, {
            'new_password1': 'brandNew!234', 'new_password2': 'brandNew!234'},
            follow=True)
        self.assertFalse(self.client.login(username='renameme', password='oldpass123'))
        self.assertTrue(self.client.login(username='renameme', password='brandNew!234'))

    @patch('bot.plumber_notifications.send_email_to_recipients')
    def test_unknown_identifier_reveals_nothing(self, mock_send):
        response = self.client.post(reverse('password_reset_request'),
                                    {'identifier': 'ghost@nowhere.test'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('a reset link is on its way', response.content.decode())
        self.assertFalse(mock_send.called)


class TenantWebhookRoutingTests(TestCase):
    """Phase 1: inbound events route to a tenant by metadata.phone_number_id.
    Route-miss falls back to homebase (single-tenant transition safety) —
    flips to log-and-drop before tenant #2 goes live."""

    def setUp(self):
        from .models import TenantWhatsAppChannel
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        TenantWhatsAppChannel.objects.create(
            tenant=self.homebase, phone_number_id='111000111')
        TenantWhatsAppChannel.objects.create(
            tenant=self.acme, phone_number_id='222000222')

    def _resolve(self, value):
        from .whatsapp_webhook import _resolve_tenant_for_value
        return _resolve_tenant_for_value(value)

    def test_known_phone_number_id_routes_to_owner(self):
        self.assertEqual(
            self._resolve({'metadata': {'phone_number_id': '222000222'}}), self.acme)
        self.assertEqual(
            self._resolve({'metadata': {'phone_number_id': '111000111'}}), self.homebase)

    def test_unknown_id_falls_back_to_homebase(self):
        self.assertEqual(
            self._resolve({'metadata': {'phone_number_id': 'nope-999'}}), self.homebase)

    def test_missing_metadata_falls_back_to_homebase(self):
        self.assertEqual(self._resolve({}), self.homebase)

    def test_inactive_channel_is_not_routable(self):
        from .models import TenantWhatsAppChannel
        TenantWhatsAppChannel.objects.filter(phone_number_id='222000222').update(is_active=False)
        self.assertEqual(
            self._resolve({'metadata': {'phone_number_id': '222000222'}}), self.homebase)

    def test_get_or_create_lead_scopes_by_tenant(self):
        phone = 'whatsapp:+15550007777'
        a, created_a = Appointment.objects.get_or_create_lead(phone, tenant=self.homebase)
        b, created_b = Appointment.objects.get_or_create_lead(phone, tenant=self.acme)
        self.assertTrue(created_a and created_b)
        self.assertNotEqual(a.pk, b.pk)
        # Re-fetch returns each tenant's own lead, never the other's.
        a2, created = Appointment.objects.get_or_create_lead(phone, tenant=self.homebase)
        self.assertFalse(created)
        self.assertEqual(a2.pk, a.pk)

    def test_get_or_create_lead_defaults_to_homebase(self):
        lead, _ = Appointment.objects.get_or_create_lead('whatsapp:+15550008888')
        self.assertEqual(lead.tenant_id, self.homebase.pk)


class TenantCredentialTests(TestCase):
    """Phase 1.2: channel tokens encrypted at rest; outbound client per tenant."""

    def setUp(self):
        from .models import TenantWhatsAppChannel
        from .whatsapp_cloud_api import invalidate_client_cache
        invalidate_client_cache()
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.channel = TenantWhatsAppChannel.objects.create(
            tenant=self.acme, phone_number_id='333000333',
            access_token='plain-secret-token', verify_token='vt',
        )

    def test_token_encrypted_at_rest_and_decryptable(self):
        self.channel.refresh_from_db()
        self.assertTrue(self.channel.access_token.startswith('fernet:'))
        self.assertNotIn('plain-secret-token', self.channel.access_token)
        self.assertEqual(self.channel.decrypted_access_token(), 'plain-secret-token')

    def test_encrypt_is_idempotent_and_legacy_plaintext_passes_through(self):
        from .services.secrets import decrypt_secret, encrypt_secret
        once = encrypt_secret('abc')
        self.assertEqual(encrypt_secret(once), once)
        self.assertEqual(decrypt_secret('legacy-plaintext'), 'legacy-plaintext')
        self.assertEqual(decrypt_secret(''), '')

    def test_client_for_tenant_uses_channel_credentials(self):
        from .whatsapp_cloud_api import get_client_for_tenant
        client = get_client_for_tenant(self.acme)
        self.assertEqual(client.phone_number_id, '333000333')
        self.assertEqual(client.access_token, 'plain-secret-token')

    def test_client_cache_returns_same_instance(self):
        from .whatsapp_cloud_api import get_client_for_tenant
        self.assertIs(get_client_for_tenant(self.acme), get_client_for_tenant(self.acme))

    def test_no_channel_falls_back_to_env_singleton(self):
        from .whatsapp_cloud_api import get_client_for_tenant, whatsapp_api
        bare = Tenant.objects.create(name='Bare Pipes', slug='bare')
        self.assertIs(get_client_for_tenant(bare), whatsapp_api)
        self.assertIs(get_client_for_tenant(None), whatsapp_api)


class GalleryPortalTests(TestCase):
    """Portal Gallery page + shared upload rules: uploads land under
    tenant_portfolios/<slug>/, the 20-file cap holds, videos are accepted
    and routed to send_local_video, and deletes never touch repo files."""

    def setUp(self):
        # The media cap counts files in storage, which outlives each test's
        # DB — start every test with an empty tenant folder.
        import shutil

        from django.conf import settings as dj_settings
        shutil.rmtree(os.path.join(dj_settings.MEDIA_ROOT, 'tenant_portfolios'),
                      ignore_errors=True)
        self.homebase = Tenant.objects.get(slug='homebase')
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.user = get_user_model().objects.create_user(
            username='acme-owner', password='pass12345', is_staff=True)
        TenantMembership.objects.create(user=self.user, tenant=self.acme, role='owner')
        self.client.force_login(self.user)

    def _upload(self, name='job.jpg', content=b'\xff\xd8 fake jpg', **extra):
        from django.core.files.uploadedfile import SimpleUploadedFile
        data = {'media': SimpleUploadedFile(name, content), 'tag': 'geyser',
                'caption': 'Geyser swap in Avondale'}
        data.update(extra)
        return self.client.post(reverse('gallery_add'), data)

    def test_gallery_page_renders(self):
        self.assertEqual(self.client.get(reverse('gallery')).status_code, 200)

    def test_add_lands_in_tenant_folder_and_creates_item(self):
        from .models import TenantPortfolioItem
        self._upload(price_line='geyser install from US$150')
        item = TenantPortfolioItem.objects.get(tenant=self.acme)
        self.assertTrue(item.filename.startswith('tenant_portfolios/acme/'))
        self.assertEqual(item.title, 'Geyser swap in Avondale')
        self.assertEqual(item.keywords, ['geyser'])
        self.assertEqual(item.price_line, 'geyser install from US$150')

    def test_video_accepted_and_routed_to_video_send(self):
        from unittest.mock import MagicMock

        from .media_library import is_video_filename
        from .models import TenantPortfolioItem
        from .whatsapp_webhook import _send_local_media
        self._upload(name='pipes.mp4', content=b'\x00\x00 fake mp4')
        item = TenantPortfolioItem.objects.get(tenant=self.acme)
        self.assertTrue(item.filename.endswith('.mp4'))
        self.assertTrue(is_video_filename(item.filename))
        client = MagicMock()
        _send_local_media(client, '+263771', item.filename, '/tmp/x.mp4', caption='c')
        client.send_local_video.assert_called_once()
        client.send_local_image.assert_not_called()
        _send_local_media(client, '+263771', 'a/b.jpg', '/tmp/y.jpg')
        client.send_local_image.assert_called_once()

    def test_bad_type_and_cap_rejected(self):
        from unittest.mock import patch

        from .models import TenantPortfolioItem
        self._upload(name='malware.exe')
        self.assertEqual(TenantPortfolioItem.objects.filter(tenant=self.acme).count(), 0)
        with patch('bot.media_library.MAX_PORTFOLIO_MEDIA', 1):
            self._upload(name='one.jpg')
            self._upload(name='two.jpg')
        self.assertEqual(TenantPortfolioItem.objects.filter(tenant=self.acme).count(), 1)

    def test_update_delete_and_tenant_pinning(self):
        from django.core.files.storage import default_storage

        from .models import TenantPortfolioItem
        self._upload()
        item = TenantPortfolioItem.objects.get(tenant=self.acme)
        self.client.post(reverse('gallery_update', args=[item.pk]),
                         {'title': 'New title', 'price_line': 'from US$99'})
        item.refresh_from_db()
        self.assertEqual((item.title, item.price_line), ('New title', 'from US$99'))
        # A homebase item is invisible to acme's portal (404 on every action).
        hb_item = TenantPortfolioItem.objects.filter(tenant=self.homebase).first()
        self.assertIsNotNone(hb_item)
        for name in ('gallery_update', 'gallery_delete'):
            self.assertEqual(self.client.post(
                reverse(name, args=[hb_item.pk]), {}).status_code, 404)
        self.assertEqual(self.client.get(
            reverse('gallery_media', args=[hb_item.pk])).status_code, 404)
        # Delete removes the row AND the uploaded file.
        path = item.filename
        self.assertTrue(default_storage.exists(path))
        self.client.post(reverse('gallery_delete', args=[item.pk]))
        self.assertFalse(TenantPortfolioItem.objects.filter(pk=item.pk).exists())
        self.assertFalse(default_storage.exists(path))

    def test_delete_never_unlinks_repo_files(self):
        from .views.gallery import _is_tenant_owned_file
        self.assertFalse(_is_tenant_owned_file(self.acme, 'modern_shower.jpg'))
        self.assertFalse(_is_tenant_owned_file(self.acme, 'tenant_portfolios/homebase/x.jpg'))
        self.assertTrue(_is_tenant_owned_file(self.acme, 'tenant_portfolios/acme/x.jpg'))
        self.assertTrue(_is_tenant_owned_file(self.acme, 'intake_photos/acme/x.jpg'))

    def test_title_is_mandatory_everywhere(self):
        import json

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.db import IntegrityError, transaction

        from .models import TenantIntake, TenantPortfolioItem
        # Portal add without a name: rejected with the message, no row created.
        res = self.client.post(reverse('gallery_add'),
                               {'media': SimpleUploadedFile('job.jpg', b'x'),
                                'tag': 'geyser', 'caption': '  '}, follow=True)
        self.assertContains(res, 'Please provide names of items for the image.')
        self.assertEqual(TenantPortfolioItem.objects.filter(tenant=self.acme).count(), 0)
        # Portal update can't blank an existing title.
        self._upload()
        item = TenantPortfolioItem.objects.get(tenant=self.acme)
        self.client.post(reverse('gallery_update', args=[item.pk]),
                         {'title': '   ', 'price_line': 'x'})
        item.refresh_from_db()
        self.assertEqual(item.title, 'Geyser swap in Avondale')
        # DB-level: an empty title violates the check constraint.
        with self.assertRaises(IntegrityError), transaction.atomic():
            TenantPortfolioItem.objects.create(
                tenant=self.acme, item_id='untitled', filename='x.jpg', title='')
        # Wizard submit with an untitled photo: draft kept, error shown,
        # intake NOT submitted. A 'before' shot of a pair is exempt.
        intake = TenantIntake.objects.create(tenant=self.acme)
        self.client.logout()
        res = self.client.post(reverse('intake_form', args=[intake.token]), {
            'photos_meta': json.dumps([
                {'path': 'tenant_portfolios/acme/a.jpg', 'tag': 'geyser',
                 'caption': '', 'pair_with_prev': False}]),
        })
        self.assertContains(res, 'Please provide names of items for the image.')
        intake.refresh_from_db()
        self.assertEqual(intake.status, 'pending')
        res = self.client.post(reverse('intake_form', args=[intake.token]), {
            'photos_meta': json.dumps([
                {'path': 'tenant_portfolios/acme/a.jpg', 'tag': 'geyser',
                 'caption': '', 'pair_with_prev': False},
                {'path': 'tenant_portfolios/acme/b.jpg', 'tag': 'geyser',
                 'caption': 'Geyser before and after', 'pair_with_prev': True}]),
        })
        intake.refresh_from_db()
        self.assertEqual(intake.status, 'submitted')

    def test_portal_ajax_upload_and_finalize_single_and_pair(self):
        import json as _json

        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import TenantPortfolioItem
        ups = [self.client.post(reverse('gallery_upload'),
                                {'media': SimpleUploadedFile(n, b'x')}).json()
               for n in ('a.jpg', 'b.jpg', 'c.jpg')]
        self.assertTrue(all(u['ok'] and 'url' in u for u in ups))
        res = self.client.post(reverse('gallery_finalize'), data=_json.dumps([
            {'path': ups[0]['path'], 'caption': 'Shower cubicle',
             'tag': 'bathroom install', 'price_line': 'Shower cubicle from US$380'},
            {'path': ups[2]['path'], 'caption': 'Geyser before and after',
             'tag': 'geyser', 'pair_path': ups[1]['path']},
            {'path': 'tenant_portfolios/other/x.jpg', 'caption': 'Sneaky'},
        ]), content_type='application/json')
        self.assertTrue(res.json()['ok'])
        items = TenantPortfolioItem.objects.filter(tenant=self.acme)
        self.assertEqual(items.count(), 2)  # the foreign-folder path is skipped
        pair = items.get(title='Geyser before and after')
        self.assertEqual(pair.pair_filename, ups[1]['path'])
        self.assertEqual(items.get(title='Shower cubicle').price_line,
                         'Shower cubicle from US$380')
        # An unnamed entry rejects the batch with the canonical message.
        res = self.client.post(reverse('gallery_finalize'), data=_json.dumps([
            {'path': ups[0]['path'], 'caption': '  '}]),
            content_type='application/json')
        self.assertEqual(res.status_code, 400)
        self.assertIn('Please provide names of items', res.json()['error'])

    def test_multi_item_finalize_stores_all_tags_and_groups(self):
        import json as _json

        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import TenantPortfolioItem
        up = self.client.post(reverse('gallery_upload'),
                              {'media': SimpleUploadedFile('bath.jpg', b'x')}).json()
        # A bathroom photo showing three jobs → one row, every category kept.
        res = self.client.post(reverse('gallery_finalize'), data=_json.dumps([
            {'path': up['path'], 'caption': 'Shower cubicle · Vanity unit · Basin',
             'tags': ['bathroom install', 'bathroom install', 'general'],
             'price_line': 'Shower cubicle from US$380\nVanity unit from US$120'},
        ]), content_type='application/json')
        self.assertTrue(res.json()['ok'])
        item = TenantPortfolioItem.objects.get(tenant=self.acme)
        # De-duplicated, primary first, both categories preserved.
        self.assertEqual(item.keywords, ['bathroom install', 'general'])
        self.assertIn('Vanity unit from US$120', item.price_line)
        # The gallery page groups it under its primary category.
        groups = self.client.get(reverse('gallery')).context['groups']
        primary = next(g for g in groups if g['key'] == 'bathroom install')
        self.assertIn(item, primary['items'])

    def test_gallery_update_accepts_multi_tags(self):
        import json as _json

        from .models import TenantPortfolioItem
        self._upload()
        item = TenantPortfolioItem.objects.get(tenant=self.acme)
        self.client.post(reverse('gallery_update', args=[item.pk]),
                         {'title': 'Bathroom refit', 'price_line': 'from US$500',
                          'tags': _json.dumps(['bathroom install', 'pipes'])})
        item.refresh_from_db()
        self.assertEqual(item.keywords, ['bathroom install', 'pipes'])
        # A rename with no tags leaves the existing categories untouched.
        self.client.post(reverse('gallery_update', args=[item.pk]),
                         {'title': 'Renamed', 'tags': '[]'})
        item.refresh_from_db()
        self.assertEqual(item.keywords, ['bathroom install', 'pipes'])

    def test_annotator_library_prices_come_from_tenant_rows(self):
        from .media_library import portfolio_library_with_prices
        from .models import TenantPriceItem
        TenantPriceItem.objects.create(tenant=self.acme, family='geyser',
                                       variant='', label='Geyser', allin=150)
        lib = portfolio_library_with_prices(self.acme)
        flat = [it for group in lib for it in group['items']]
        geyser = next(it for it in flat if it['family'] == 'geyser' and it['variant'] == '')
        self.assertEqual(geyser['price'], '150')
        # Everything the tenant hasn't priced stays blank — no cross-tenant leak.
        self.assertTrue(all(it['price'] == '' for it in flat if it is not geyser))

    def test_annotator_price_line_applies_on_approve(self):
        from .models import TenantPortfolioItem
        from .views.platform import _apply_intake_photos
        _apply_intake_photos(self.acme, [
            {'path': 'tenant_portfolios/acme/g.jpg', 'tag': 'geyser',
             'caption': 'Geyser supply & install',
             'price_line': 'Geyser supply & install from US$150'}])
        item = TenantPortfolioItem.objects.get(tenant=self.acme)
        self.assertEqual(item.title, 'Geyser supply & install')
        self.assertEqual(item.price_line, 'Geyser supply & install from US$150')

    def test_customer_media_paths_are_per_tenant(self):
        from .media_library import customer_media_path
        self.assertEqual(customer_media_path(self.acme, 'image', 'p.jpg'),
                         'customer_plans/acme/p.jpg')
        self.assertEqual(customer_media_path(self.acme, 'document', 'p.pdf'),
                         'customer_plans/acme/p.pdf')
        self.assertEqual(customer_media_path(self.acme, 'video', 'v.mp4'),
                         'customer_videos/acme/v.mp4')
        self.assertEqual(customer_media_path(self.acme, 'audio', 'n.ogg'),
                         'customer_audio/acme/n.ogg')
        self.assertEqual(customer_media_path(self.acme, 'sticker', 'x.bin'),
                         'customer_media/acme/x.bin')
        self.assertEqual(customer_media_path(None, 'image', 'p.jpg'),
                         'customer_plans/homebase/p.jpg')

    def test_inbound_plan_and_video_land_in_tenant_folder(self):
        from unittest.mock import MagicMock, patch

        from .models import Appointment
        from .whatsapp_webhook import handle_media_message
        wa = MagicMock()
        wa.download_media.return_value = b'%PDF fake plan'
        with patch('bot.whatsapp_cloud_api.get_client_for_tenant', return_value=wa), \
             patch('bot.whatsapp_webhook._schedule_media_ack'):
            handle_media_message('263771000111',
                                 {'id': 'MID1', 'mime_type': 'application/pdf'},
                                 'document', tenant=self.acme)
            handle_media_message('263771000111',
                                 {'id': 'MID2', 'mime_type': 'video/mp4'},
                                 'video', tenant=self.acme)
        apt = Appointment.objects.get(tenant=self.acme)
        self.assertTrue(str(apt.plan_file).startswith('customer_plans/acme/'),
                        str(apt.plan_file))
        self.assertIn('customer_videos/acme/', apt.internal_notes)

    def test_wizard_upload_endpoint_uses_shared_rules(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import TenantIntake
        intake = TenantIntake.objects.create(tenant=self.acme)
        self.client.logout()  # endpoint is public, token-gated
        res = self.client.post(
            reverse('intake_photo_upload', args=[intake.token]),
            {'photo': SimpleUploadedFile('work.mp4', b'\x00 fake')})
        out = res.json()
        self.assertTrue(out['ok'])
        self.assertTrue(out['path'].startswith('tenant_portfolios/acme/'))
        self.assertIn('url', out)  # the wizard's annotator/preview needs it
        res = self.client.post(
            reverse('intake_photo_upload', args=[intake.token]),
            {'photo': SimpleUploadedFile('bad.exe', b'x')})
        self.assertEqual(res.status_code, 400)


class OfferPageTests(TestCase):
    """Portal 'My Offer': the tenant's own Facebook/social offer — the price
    the bot leads with on vague, no-context 'how much' questions."""

    def setUp(self):
        self.homebase = Tenant.objects.get(slug='homebase')
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.user = get_user_model().objects.create_user(
            username='acme-owner2', password='pass12345', is_staff=True)
        TenantMembership.objects.create(user=self.user, tenant=self.acme, role='owner')
        self.client.force_login(self.user)

    def test_save_edit_and_remove_offer(self):
        from .models import TenantPriceItem
        from .pricing_copy import facebook_package_facts
        from .tenant_config import get_config
        self.assertEqual(self.client.get(reverse('offer')).status_code, 200)
        self.client.post(reverse('offer_save'), {
            'label': 'Bathroom makeover special', 'price': 'US$800',
            'includes': 'freestanding tub\nside chamber\n'})
        row = TenantPriceItem.objects.get(
            tenant=self.acme, family='package', variant='facebook')
        self.assertEqual(int(row.flat), 800)
        facts = facebook_package_facts(get_config(self.acme))
        self.assertEqual((facts['price'], facts['label'], facts['en']),
                         (800, 'Bathroom makeover special',
                          'freestanding tub and side chamber'))
        # Homebase's own offer row is untouched by acme's edits.
        self.assertTrue(TenantPriceItem.objects.filter(
            tenant=self.homebase, family='package', variant='facebook').exists())
        # Clearing the price removes the offer entirely.
        self.client.post(reverse('offer_save'),
                         {'label': 'x', 'price': '', 'includes': ''})
        self.assertFalse(TenantPriceItem.objects.filter(
            tenant=self.acme, family='package', variant='facebook').exists())

    def test_bad_price_rejected(self):
        from .models import TenantPriceItem
        self.client.post(reverse('offer_save'), {'price': 'eight hundred'})
        self.assertFalse(TenantPriceItem.objects.filter(
            tenant=self.acme, family='package').exists())

    def test_vague_how_much_composes_from_offer_alone(self):
        # A tenant whose ONLY price row is the offer still gets the anchored
        # reply; a tenant with no offer gets None (router deflects).
        from .models import TenantPriceItem
        from .tenant_config import get_config
        from .views.plumbot.response_mixin import ResponseMixin
        TenantPriceItem.objects.create(
            tenant=self.acme, family='package', variant='facebook',
            label='winter special', flat=350,
            parts=[{'name': 'geyser'}, {'name': 'thermostat'}])
        acme_cfg = get_config(self.acme)

        class _Fake:
            tenant_cfg = acme_cfg
            def _freestanding_tub_price(self):
                return None
            def _price_components_map(self):
                return {}
            def _product_price_close(self, lang):
                return 'Which area are you in?'
            def _ensure_price_disclaimer(self, intent, reply):
                return reply
        reply = ResponseMixin._compose_pricing_overview(_Fake(), 'english')
        self.assertIn('Our Winter special is US$350 — a geyser and thermostat.', reply)
        self.assertNotIn('tub', reply)
        self.assertTrue(reply.endswith('Which area are you in?'))
        bare = Tenant.objects.create(name='Bare Offer', slug='bare-offer')
        bare_cfg = get_config(bare)

        class _FakeNone(_Fake):
            tenant_cfg = bare_cfg
        self.assertIsNone(
            ResponseMixin._compose_pricing_overview(_FakeNone(), 'english'))


class TenantConfigTests(TestCase):
    """Phase 2 slice 1: FAQ facts + identity via the TenantConfig seam.
    Homebase must be byte-identical to the old hardcoded strings; a tenant
    without facts must get graceful omission, never homebase's values."""

    def setUp(self):
        self.homebase = Tenant.objects.get(slug='homebase')  # test-DB hook seeds it
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')

    def test_homebase_faq_facts_byte_identical_to_legacy_constants(self):
        from .faq import faq_fact
        from .tenant_config import HOMEBASE_FAQ_FACTS, get_config
        cfg = get_config(self.homebase)
        for topic, legacy in HOMEBASE_FAQ_FACTS.items():
            with self.subTest(topic=topic):
                self.assertEqual(cfg.faq_fact(topic), legacy)
                self.assertEqual(faq_fact(topic, tenant=self.homebase), legacy)

    def test_foreign_tenant_never_gets_homebase_facts(self):
        from .faq import faq_fact
        from .tenant_config import HOMEBASE_FAQ_FACTS
        for topic in HOMEBASE_FAQ_FACTS:
            with self.subTest(topic=topic):
                self.assertIsNone(faq_fact(topic, tenant=self.acme))

    def test_foreign_tenant_own_facts_win(self):
        from .faq import faq_fact
        TenantProfile.objects.create(
            tenant=self.acme,
            faq_facts={'location': "We're in Bulawayo CBD."},
            licensed_claim_enabled=False,
        )
        self.assertEqual(faq_fact('location', tenant=self.acme), "We're in Bulawayo CBD.")
        self.assertIsNone(faq_fact('payment', tenant=self.acme))

    def test_licensed_claim_gated_on_certification_flag(self):
        from .tenant_config import get_config
        profile = TenantProfile.objects.create(
            tenant=self.acme,
            faq_facts={'licensed': 'Yes, fully licensed.'},
            licensed_claim_enabled=False,
        )
        self.assertIsNone(get_config(self.acme).faq_fact('licensed'))
        profile.licensed_claim_enabled = True
        profile.save()
        self.assertEqual(get_config(self.acme).faq_fact('licensed'), 'Yes, fully licensed.')

    def test_none_tenant_resolves_to_homebase_seed(self):
        from .faq import faq_fact
        from .tenant_config import HOMEBASE_FAQ_FACTS
        self.assertEqual(faq_fact('location', tenant=None), HOMEBASE_FAQ_FACTS['location'])

    def test_plumber_helpers_per_tenant(self):
        # Homebase lead: profile-driven; per-lead override wins; foreign
        # tenant with no profile: '' + generic name (never homebase's).
        hb_lead = make_lead(9601, tenant=self.homebase)
        self.assertEqual(hb_lead.plumber_contact(), '+263774819901')
        self.assertEqual(hb_lead.plumber_display_name(), 'Takudzwa')
        hb_lead.plumber_contact_number = '+263700000001'
        self.assertEqual(hb_lead.plumber_contact(), '+263700000001')
        acme_lead = make_lead(9602, tenant=self.acme)
        self.assertEqual(acme_lead.plumber_contact(), '')
        self.assertEqual(acme_lead.plumber_display_name(), 'the plumber')

    def test_email_identity_per_tenant(self):
        # Homebase emails carry their own identity; a bare tenant's emails
        # omit contact buttons and use its business name — never homebase's.
        from .customer_emails import (
            _business_name, _call_phone, _contact_buttons, _from_name, _wa_number, _wrap,
        )
        hb_lead = make_lead(9701, tenant=self.homebase)
        self.assertEqual(_call_phone(hb_lead), '263774819901')
        self.assertEqual(_wa_number(hb_lead), '263776255077')
        self.assertEqual(_from_name(hb_lead), 'Takudzwa')
        self.assertIn('263776255077', _contact_buttons(hb_lead))
        self.assertIn('Homebase Plumbers · Zimbabwe', _wrap('<p>x</p>', hb_lead))

        acme_lead = make_lead(9702, tenant=self.acme)
        self.assertEqual(_contact_buttons(acme_lead), '')
        self.assertEqual(_from_name(acme_lead), 'Acme Plumbing')
        self.assertNotIn('263774819901', _wrap('<p>x</p>', acme_lead))
        self.assertIn('Acme Plumbing · Zimbabwe', _wrap('<p>x</p>', acme_lead))

    def test_price_accessors_match_legacy_response_mixin_tables(self):
        # Phase 2.3 parity pins: the cfg price shapes must equal the tables
        # that lived hardcoded in response_mixin until 2.3b (literals below
        # ARE those tables, verbatim). Any drift in the homebase seed or the
        # renderers = a real price change on prod — fail loudly.
        from .tenant_config import get_config
        cfg = get_config(self.homebase)

        legacy_components = {
            'shower': (130, 40), 'tub': (80, 80), 'geyser': (80, 80),
            'vanity': (150, 30), 'toilet': (50, 20), 'chamber': (130, 30),
        }
        components = cfg.price_components()
        for family, pair in legacy_components.items():
            self.assertEqual(components.get(family), pair, family)

        self.assertEqual(cfg.flat_prices().get('basin'), 70)

        legacy_rough = {
            'shower': 'shower cubicle from US$170', 'tub': 'tub from US$160',
            'geyser': 'geyser from US$160', 'vanity': 'vanity from US$180',
            'toilet': 'toilet from US$70', 'chamber': 'side chamber from US$160',
        }
        rough = cfg.rough_price_lines()
        for family, line in legacy_rough.items():
            self.assertEqual(rough.get(family), line, family)

        legacy_breakdown = {
            'shower': 'Shower cubicle: supply from US$130, labour from US$40',
            'tub': 'Tub: supply from US$80, labour from US$80',
            'geyser': 'Geyser: supply from US$80, labour from US$80',
            'vanity': 'Vanity unit: supply from US$150, labour from US$30',
            'toilet': 'Toilet seat: supply from US$50, labour from US$20',
            'chamber': 'Side chamber: supply from US$130, labour from US$30',
        }
        breakdown = cfg.labour_breakdown_lines()
        for family, line in legacy_breakdown.items():
            self.assertEqual(breakdown.get(family), line, family)

        allin, split = cfg.freestanding_tub()
        self.assertEqual(allin, 670)
        self.assertEqual(split, "tub from US$400 + mixer US$150, install from US$120")

    def test_structured_pricing_render_pinned(self):
        # Phase 2.3c: the bilingual per-intent blocks render from price rows.
        # Pin the load-bearing lines byte-for-byte (full parity vs the legacy
        # dict was proven mechanically before the swap — 2026-07-15).
        from .pricing_copy import build_structured_pricing
        from .tenant_config import get_config
        sp = build_structured_pricing(get_config(self.homebase))
        self.assertEqual(
            sorted(sp.keys()),
            sorted(['tub_sales', 'standalone_tub', 'bathtub_installation', 'geyser',
                    'shower_cubicle', 'vanity', 'toilet', 'wall_hung_toilet', 'chamber',
                    'facebook_package', 'drain_unblocking', 'pipe_repair',
                    'geyser_repair', 'toilet_repair']))
        self.assertEqual(
            sp['tub_sales']['breakdown_lines'][0],
            "Freestanding tub: Supply US$400 | Mixer US$150 | Install US$120 → from US$670 all-in")
        self.assertEqual(
            sp['tub_sales']['sn_cheapest_line'],
            "Starting point i standard tub paUS$80 supply + US$80 install.")
        self.assertEqual(
            sp['pipe_repair']['total_line'],
            "Pipe repairs start from US$15–$20 for minor leaks — cost depends on the pipe size, location, and how accessible it is.")
        self.assertEqual(
            sp['toilet_repair']['total_line'],
            "Toilet repairs start from US$20 for labour + parts. A full replacement (supply and fit) starts from US$100.")
        self.assertEqual(
            sp['facebook_package']['total_line'],
            "The Facebook package is US$800 — freestanding tub and side chamber.")
        self.assertEqual(
            sp['geyser_repair']['cheapest_line'],
            "Minor repairs like a valve or thermostat start from US$25–$30.")
        # Bare tenant: no sheet → no blocks → handler deflects.
        self.assertEqual(build_structured_pricing(get_config(self.acme)), {})

    def test_facebook_offer_varies_per_tenant(self):
        # The social-ad offer composes from the tenant's OWN package row:
        # label, price, and contents — never homebase's wording.
        from .models import TenantPriceItem
        from .pricing_copy import facebook_package_facts
        from .tenant_config import get_config
        # Homebase: byte-identical to the legacy copy.
        hb = facebook_package_facts(get_config(self.homebase))
        self.assertEqual(
            (hb['label'], hb['price'], hb['en']),
            ('Facebook package', 800, 'freestanding tub and side chamber'))
        # A tenant with a different special gets their own composition.
        TenantPriceItem.objects.create(
            tenant=self.acme, family='package', variant='facebook',
            label='WhatsApp winter special', flat=350,
            parts=[{'name': 'geyser'}, {'name': 'thermostat'}])
        acme = facebook_package_facts(get_config(self.acme))
        self.assertEqual(
            (acme['label'], acme['price'], acme['en']),
            ('Whatsapp winter special', 350, 'geyser and thermostat'))
        # A tenant with NO package: no offer to pitch.
        bare = Tenant.objects.create(name='Bare Pipes 2', slug='bare2')
        self.assertIsNone(facebook_package_facts(get_config(bare)))

    def test_price_accessors_empty_for_bare_tenant(self):
        from .tenant_config import get_config
        cfg = get_config(self.acme)
        self.assertEqual(cfg.price_components(), {})
        self.assertEqual(cfg.rough_price_lines(), {})
        self.assertEqual(cfg.flat_prices(), {})
        self.assertIsNone(cfg.freestanding_tub())
        self.assertIsNone(cfg.price_item('shower'))

    def test_portfolio_items_per_tenant(self):
        # Phase 2.5: catalogue reads TenantPortfolioItem rows. Homebase's rows
        # must round-trip the legacy PORTFOLIO_ITEMS dicts; a foreign tenant
        # gets nothing — never homebase's photos.
        from . import portfolio_catalog
        from .portfolio_catalog import PORTFOLIO_ITEMS, items_for
        hb_items = items_for(self.homebase)
        self.assertEqual(len(hb_items), len(PORTFOLIO_ITEMS))
        legacy_by_id = {i['id']: i for i in PORTFOLIO_ITEMS}
        for item in hb_items:
            legacy = legacy_by_id[item['id']]
            for key in ('filename', 'title', 'price', 'description', 'story', 'keywords'):
                self.assertEqual(item[key], legacy.get(key, '' if key != 'keywords' else []), f"{item['id']}.{key}")
        self.assertEqual(items_for(self.acme), [])
        self.assertIsNone(portfolio_catalog.catalogue_overview(tenant=self.acme))
        self.assertIsNone(portfolio_catalog.match_portfolio_item(
            'show me the black tub photo', tenant=self.acme))

    def test_uploaded_photo_quotable_via_highlight_chain(self):
        # A tenant's wizard-uploaded photo (storage-backed path) must be:
        # available → in their gallery → described by ITS title (so a customer
        # highlighting it gets the right answer) → priced via get_item_by_title.
        from django.core.files.base import ContentFile
        from django.core.files.storage import default_storage

        from . import portfolio_catalog
        from .models import TenantPortfolioItem
        from .whatsapp_webhook import _describe_work_image, _materialize_image, get_previous_work_images

        path = default_storage.save('intake_photos/acme/testgeyser.png',
                                    ContentFile(b'\x89PNG fake'))
        TenantPortfolioItem.objects.create(
            tenant=self.acme, item_id='geyser-1', filename=path,
            title='Geyser swap in Kwekwe', price_line='geyser install from US$150',
            keywords=['geyser'])
        item = portfolio_catalog.items_for(self.acme)[0]
        self.assertTrue(portfolio_catalog.item_is_available(item))
        self.assertEqual(get_previous_work_images(self.acme), [path])
        # Description (what record_sent_media stores → what quotes resolve to).
        self.assertEqual(_describe_work_image(path, tenant=self.acme),
                         'Geyser swap in Kwekwe')
        # Homebase describing the same path finds nothing of its own.
        self.assertNotEqual(_describe_work_image(path, tenant=self.homebase),
                            'Geyser swap in Kwekwe')
        # Title → item → price guide (the "this one how much?" answer).
        guide = portfolio_catalog.build_item_price_guide(
            'Geyser swap in Kwekwe', tenant=self.acme)
        self.assertIn('US$150', guide)
        # Materialization yields a real local file for the WhatsApp send.
        local, is_temp = _materialize_image(path)
        self.assertTrue(os.path.exists(local))
        if is_temp:
            os.unlink(local)

    def test_foreign_tenant_gallery_never_serves_homebase_photos(self):
        from .whatsapp_webhook import get_catalogue_images, get_previous_work_images
        self.assertEqual(get_previous_work_images(self.acme), [])
        self.assertEqual(get_catalogue_images(self.acme), [])

    def test_service_area_per_tenant(self):
        # Phase 2.6: the decline list comes from the tenant profile.
        from .views.plumbot.state_mixin import StateMixin
        # Homebase: seeded list + the vic-falls alias expansion.
        hb = StateMixin._tenant_excluded_areas(self.homebase)
        self.assertIn('bulawayo', hb)
        self.assertIn('vic falls', hb)
        self.assertEqual(
            StateMixin._is_excluded_city_keywords('Bulawayo', tenant=self.homebase),
            'Bulawayo')
        # Foreign tenant with no list: declines nowhere.
        self.assertEqual(StateMixin._tenant_excluded_areas(self.acme), set())
        self.assertIsNone(StateMixin._is_excluded_city('Bulawayo', tenant=self.acme))
        # Foreign tenant with its own list: only theirs applies.
        TenantProfile.objects.create(tenant=self.acme, excluded_areas=['kariba'])
        self.assertEqual(
            StateMixin._is_excluded_city_keywords('Kariba', tenant=self.acme), 'Kariba')
        self.assertIsNone(
            StateMixin._is_excluded_city_keywords('Bulawayo', tenant=self.acme))

    def test_identity_fields_read_from_profile(self):
        from .tenant_config import get_config
        cfg = get_config(self.homebase)
        self.assertEqual(cfg.plumber_name, 'Takudzwa')
        self.assertEqual(cfg.plumber_contact, '+263774819901')
        self.assertEqual(cfg.business_whatsapp, '+263776255077')
        self.assertIn('gweru', cfg.excluded_areas())
        # Absent profile → graceful empties, never homebase's values.
        bare_cfg = get_config(self.acme)
        self.assertEqual(bare_cfg.plumber_name, '')
        self.assertEqual(bare_cfg.excluded_areas(), [])
