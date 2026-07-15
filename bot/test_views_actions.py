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
    """Logged-in staff client, shared by every test class below."""

    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username='staff-tester', password='pass12345', is_staff=True,
        )
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

    def test_no_membership_falls_back_to_homebase(self):
        get_user_model().objects.create_user(
            username='legacystaff', password='pass12345', is_staff=True)
        self.client.login(username='legacystaff', password='pass12345')
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.wsgi_request.tenant, self.homebase)


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
