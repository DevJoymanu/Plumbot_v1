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

import json
import os
import re
import unittest
from decimal import Decimal
from io import StringIO
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import (
    Appointment,
    ConversationMessage,
    Job,
    Quotation,
    QuotationItem,
    QuotationTemplate,
    ScheduledFollowup,
    ScheduledReminder,
    Tenant,
    TenantMembership,
    TenantProfile,
    TenantSetting,
    WhatsAppSendCost,
    get_default_tenant_id,
)


# Anything a customer reads must be free of these: pictographs, dingbats,
# arrows and the variation selector that turns a glyph into an emoji.
_EMOJI_RE = re.compile('[\U0001F000-\U0001FAFF\u2190-\u21FF\u2300-\u27BF\u2B00-\u2BFF\uFE0F]')


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

    def _become_owner(self):
        """Deleting conversations is restricted to the platform OWNER account,
        so the delete tests run as that account: superuser AND listed in
        settings.PLATFORM_OWNER_ACCOUNTS."""
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        override = override_settings(
            PLATFORM_OWNER_ACCOUNTS=[self.user.get_username()])
        override.enable()
        self.addCleanup(override.disable)

    def test_delete_lead_requires_post_and_cascades(self):
        """The dashboard Delete button. GET must never destroy a lead (a
        prefetched link would be enough), and the delete has to take the child
        records with it rather than leaving orphans behind."""
        self._become_owner()
        Quotation.objects.create(appointment=self.lead)
        ScheduledFollowup.objects.create(
            appointment=self.lead, channel='whatsapp',
            scheduled_for=timezone.now() + timedelta(days=1))
        pk = self.lead.pk

        rejected = self.client.get(reverse('delete_appointment', args=[pk]))
        self.assertEqual(rejected.status_code, 405)
        self.assertTrue(Appointment.objects.filter(pk=pk).exists())

        response = self.client.post(reverse('delete_appointment', args=[pk]))
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Appointment.objects.filter(pk=pk).exists())
        self.assertFalse(Quotation.objects.filter(appointment_id=pk).exists())
        self.assertFalse(ScheduledFollowup.objects.filter(appointment_id=pk).exists())

    def test_only_the_owner_can_delete_past_conversations(self):
        """Tenant staff must not be able to destroy conversation history — the
        POST is refused and the lead (with its transcript) survives."""
        pk = self.lead.pk
        response = self.client.post(reverse('delete_appointment', args=[pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Appointment.objects.filter(pk=pk).exists())

        # ...and the button is not rendered for them either.
        body = self.client.get(self.detail_url()).content.decode()
        self.assertNotIn(reverse('delete_appointment', args=[pk]), body)

        self._become_owner()
        body = self.client.get(self.detail_url()).content.decode()
        self.assertIn(reverse('delete_appointment', args=[pk]), body)

    def test_a_second_admin_account_still_cannot_delete(self):
        """Superuser is deliberately not enough: only the owner account listed in
        PLATFORM_OWNER_ACCOUNTS may destroy a transcript."""
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        pk = self.lead.pk
        with self.settings(PLATFORM_OWNER_ACCOUNTS=['adminJ']):
            response = self.client.post(reverse('delete_appointment', args=[pk]))
            self.assertEqual(response.status_code, 403)
            self.assertTrue(Appointment.objects.filter(pk=pk).exists())
            body = self.client.get(self.detail_url()).content.decode()
            self.assertNotIn(reverse('delete_appointment', args=[pk]), body)

    def test_owner_matches_on_email_too_and_empty_list_never_locks_out(self):
        from .decorators import is_platform_owner

        self.user.is_superuser = True
        self.user.email = 'jones86xi@gmail.com'
        self.user.save(update_fields=['is_superuser', 'email'])
        with self.settings(PLATFORM_OWNER_ACCOUNTS=['JONES86XI@GMAIL.COM']):
            self.assertTrue(is_platform_owner(self.user))
        # A mis-set env var must not lock the owner out of their own platform.
        with self.settings(PLATFORM_OWNER_ACCOUNTS=[]):
            self.assertTrue(is_platform_owner(self.user))
        # Plain staff are never the owner, whatever the list says.
        self.user.is_superuser = False
        self.user.save(update_fields=['is_superuser'])
        with self.settings(PLATFORM_OWNER_ACCOUNTS=[]):
            self.assertFalse(is_platform_owner(self.user))

    def test_delete_lead_honours_only_internal_next(self):
        self._become_owner()
        target = reverse('conversations_list') + '?status_filter=pending'
        response = self.client.post(
            reverse('delete_appointment', args=[self.lead.pk]), {'next': target})
        self.assertEqual(response['Location'], target)

        other = make_lead(9012)
        response = self.client.post(
            reverse('delete_appointment', args=[other.pk]),
            {'next': '//evil.example.com/'})
        self.assertEqual(response['Location'], reverse('conversations_list'))

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

    @patch('bot.views.followups.get_client_for_tenant')
    def test_manual_followup_sends_from_the_leads_tenant(self, mock_get_client):
        # The send is routed through the lead's own tenant channel, not the
        # global env singleton — so a tenant messages from its own number.
        from unittest.mock import MagicMock
        mock_get_client.return_value = MagicMock()
        response = self.client.post(
            reverse('send_followup', args=[self.lead.pk]),
            {'message': 'Hi {name}, checking in.'})
        self.assertEqual(response.status_code, 302)
        mock_get_client.assert_called_once_with(self.lead.tenant)
        mock_get_client.return_value.send_text_message.assert_called_once()

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
# 4b. Quote screens on a phone
#
# The whole quote workflow has to be usable at 320-430px. These pin the
# structural facts that made it unusable before, each of which is silent
# in a normal smoke test because the page still returns 200:
#   * view/edit rendered a whole second <!DOCTYPE html> document inside
#     base.html's content block, so the inner body{background} painted
#     over the shell and the mobile nav was unreachable;
#   * item tables carried min-width: 800px, forcing a sideways scroll;
#   * the icon set was Bootstrap Icons, which head_assets never loads, so
#     icon-only touch targets rendered blank.
# ======================================================================

class QuoteMobileLayoutTests(StaffClientTestCase):
    """Every quote screen ships the shared responsive layer and nothing that
    forces horizontal overflow."""

    def setUp(self):
        super().setUp()
        self.lead = make_lead(4, customer_name='Mobile Quote Lead')
        self.quote = Quotation.objects.create(appointment=self.lead)
        self.template = QuotationTemplate.objects.create(name='Mobile Template')

    def quote_pages(self):
        return {
            'quotations_list': reverse('quotations_list'),
            'view_quotation': reverse('view_quotation', args=[self.quote.pk]),
            'edit_quotation': reverse('edit_quotation', args=[self.quote.pk]),
            'create_quotation': reverse('create_quotation', args=[self.lead.pk]),
            'standalone_quotation': reverse('standalone_quotation'),
            'quotation_templates_list': reverse('quotation_templates_list'),
            'quotation_template_detail': reverse('quotation_template_detail',
                                                 args=[self.template.pk]),
            'create_quotation_template': reverse('create_quotation_template'),
            'edit_quotation_template': reverse('edit_quotation_template',
                                               args=[self.template.pk]),
        }

    def _html(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f'{url} -> {response.status_code}')
        return response.content.decode('utf-8', 'replace')

    def test_pages_are_a_single_document(self):
        """No page nests a second full HTML document inside the shell."""
        for name, url in self.quote_pages().items():
            with self.subTest(page=name):
                html = self._html(url)
                self.assertEqual(html.count('<!DOCTYPE'), 1,
                                 f'{name} renders a nested document')
                self.assertEqual(html.count('<html'), 1, f'{name} nests <html>')
                self.assertEqual(html.count('name="viewport"'), 1,
                                 f'{name} has a duplicate viewport meta')

    def test_pages_reach_the_mobile_nav(self):
        """Every quote screen extends the shell, so the bottom nav is there."""
        for name, url in self.quote_pages().items():
            with self.subTest(page=name):
                self.assertIn('pb-bottomnav', self._html(url),
                              f'{name} has no mobile navigation')

    def test_no_table_forces_horizontal_scroll(self):
        """A min-width wider than a phone means a sideways-scrolling page."""
        for name, url in self.quote_pages().items():
            with self.subTest(page=name):
                html = self._html(url)
                # Lookbehind skips `@media (min-width: …)`, which is a
                # breakpoint, not a declared width.
                wide = [int(px) for px in re.findall(r'(?<!\()min-width:\s*(\d+)px', html)
                        if int(px) >= 600]
                self.assertEqual(wide, [], f'{name} pins a {wide}px minimum width')

    def test_icons_use_the_loaded_icon_set(self):
        """head_assets ships Font Awesome only; `bi bi-*` renders as nothing."""
        for name, url in self.quote_pages().items():
            with self.subTest(page=name):
                self.assertNotIn('bi bi-', self._html(url),
                                 f'{name} uses Bootstrap Icons, which never load')

    def test_shared_responsive_layer_is_present(self):
        """quote_responsive_css.html must actually reach every quote screen —
        matched on its banner, which appears nowhere else."""
        for name, url in self.quote_pages().items():
            with self.subTest(page=name):
                self.assertIn('Quote workflow — shared responsive layer',
                              self._html(url),
                              f'{name} does not include quote_responsive_css.html')

    def test_editable_item_tables_stack_on_mobile(self):
        """The item editors opt into the stacked-card treatment."""
        for name in ('edit_quotation', 'create_quotation_template',
                     'edit_quotation_template'):
            with self.subTest(page=name):
                self.assertIn('pbq-table--edit', self._html(self.quote_pages()[name]),
                              f'{name} keeps a desktop-only item table')

    #: Every screen where the user builds up a list of line items.
    ITEM_EDITORS = ('create_quotation', 'standalone_quotation', 'edit_quotation',
                    'create_quotation_template', 'edit_quotation_template')

    def test_item_editors_keep_added_items_on_screen(self):
        """Adding an item must not hide the ones already added.

        Each item editor wraps its list in a persistent panel: a running
        count/subtotal bar above, the list itself, and Add Item below. The
        list gets its own scrollbar once it is long (`is-capped`, toggled in
        JS) rather than pushing everything else off the page.
        """
        for name in self.ITEM_EDITORS:
            with self.subTest(page=name):
                html = self._html(self.quote_pages()[name])
                for marker in ('pbq-items-panel', 'id="itemsBar"', 'id="itemsCount"',
                               'id="itemsRunningTotal"', 'id="itemsScroll"'):
                    self.assertIn(marker, html, f'{name} is missing {marker}')

    def test_totals_do_not_scroll_away_with_the_item_list(self):
        """The template builders kept their totals in the table's <tfoot>, so
        capping the list into a scroll box would have scrolled the totals out
        of sight along with the rows. They live in a .pbq-totals block below
        the scrollable list instead."""
        for name in ('create_quotation_template', 'edit_quotation_template'):
            with self.subTest(page=name):
                html = self._html(self.quote_pages()[name])
                self.assertNotIn('<tfoot>', html,
                                 f'{name} still holds its totals inside the table')
                self.assertIn('pbq-total-row--grand', html,
                              f'{name} has no totals block below the list')
                # The rows scroll; the totals must sit outside that box.
                after_scroll_box = html.split('id="itemsScroll"', 1)[1]
                self.assertLess(after_scroll_box.index('</table>'),
                                after_scroll_box.index('pbq-total-row--grand'),
                                f'{name} renders its totals inside the scroll box')

    def test_cap_constant_is_declared_before_it_is_used(self):
        """`const` is in the temporal dead zone until its own line runs. The
        template builder calls calculateAllTotals() — which reads the cap — at
        the top of its DOMContentLoaded handler, so a later `const` threw a
        ReferenceError that took the whole handler down: no totals, no delete
        buttons, no auto-add, on a page still returning 200."""
        html = self._html(self.quote_pages()['create_quotation_template'])
        self.assertLess(html.index('const ITEMS_CAP_AT'),
                        html.index('calculateAllTotals();'),
                        'the row cap is declared after the code that reads it')

    def test_new_item_does_not_recentre_the_page(self):
        """`scrollIntoView({block: 'center'})` on the new row pushed every
        item already added off the viewport — on a phone with the keyboard up
        the blank new row was all that was left. Reveal must be 'nearest', or
        a scroll of the list's own box."""
        for name in ('create_quotation', 'standalone_quotation', 'edit_quotation'):
            with self.subTest(page=name):
                html = self._html(self.quote_pages()[name])
                self.assertNotIn("block: 'center'", html,
                                 f'{name} re-centres the page on the new item')
                self.assertIn("block: 'nearest'", html,
                              f'{name} has no minimal-scroll reveal')

    def test_line_item_ids_cannot_collide(self):
        """Line items are looked up by id, and ids came from `Date.now()` —
        two items added inside the same millisecond shared one, so editing
        one edited both and removing one removed both. A monotonic counter
        is the only safe source."""
        for name in ('create_quotation', 'standalone_quotation'):
            with self.subTest(page=name):
                html = self._html(self.quote_pages()[name])
                items_js = html.split('function renderItems')[0]
                self.assertNotIn('id: Date.now()', items_js,
                                 f'{name} mints line-item ids from the clock')
                self.assertIn('nextItemId()', html,
                              f'{name} has no monotonic id source')

    def test_quotations_list_paginates(self):
        """25-per-page with no controls stranded every quote after the first
        page; the list has to be navigable on a phone."""
        for i in range(30):
            Quotation.objects.create(appointment=self.lead)
        html = self._html(reverse('quotations_list'))
        self.assertIn('?page=2', html)


# ======================================================================
# 4c. The sectioned quote document
#
# A tenant whose profile sets letterhead.layout = "sectioned" gets the
# grouped quote sheet — numbered sections with their own subtotals, plus
# discount and VAT. Everyone else keeps the flat layout, and no letterhead
# value may cross between tenants.
# ======================================================================

SECTIONED_LETTERHEAD = {
    'layout': 'sectioned',
    'trading_name': 'ROYAL HARDWARE',
    'phones': ['+263 77 387 1503', '+263 77 324 0167'],
    'public_email': 'info@barmakplumbing.co.zw',
    'website': 'www.barmakplumbing.co.zw',
    'services_blurb': 'For all: supply & new installation water & sewer reticulation.',
    'maintenance_blurb': 'Maintenance: water leaks, no water, low pressure & blockages.',
    'tagline': 'Quality is our mission',
    'signatory': 'Director K. Marange',
    'bank': {
        'account_name': 'Barmak Plumbing Private Limited',
        'bank_name': 'CABS',
        'branch': 'Park street',
        'account_number': '1154714543',
    },
    'terms': ['deposit 75%', 'Balance to be paid on completion of 1st stage'],
}


class SectionedQuoteTests(TestCase):
    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.barmak = Tenant.objects.create(
            name='Barmak Plumbing', slug='barmak-plumbing')
        TenantProfile.objects.create(
            tenant=self.barmak,
            location_line='20398 Budiriro 5B Cabs Harare',
            letterhead=SECTIONED_LETTERHEAD,
        )

        # One staff member per tenant: the active tenant comes from
        # membership, and a shared login would blur exactly the boundary
        # these tests exist to check.
        self.user = self._staff('barmak-staff', self.barmak)
        self.client.force_login(self.user)

        from django.test import Client
        self.hb_client = Client()
        self.hb_client.force_login(self._staff('homebase-staff', self.homebase))

        self.barmak_lead = make_lead(7101, tenant=self.barmak, customer_name='B Client')
        self.homebase_lead = make_lead(7102, tenant=self.homebase, customer_name='H Client')

    @staticmethod
    def _staff(username, tenant):
        user = get_user_model().objects.create_user(
            username=username, password='pass12345', is_staff=True)
        TenantMembership.objects.create(user=user, tenant=tenant, role='staff')
        return user

    def _html(self, url, client=None):
        response = (client or self.client).get(url)
        self.assertEqual(response.status_code, 200, f'{url} -> {response.status_code}')
        return response.content.decode('utf-8', 'replace')

    # ── layout selection ────────────────────────────────────────────────
    def test_barmak_lead_gets_the_sectioned_sheet(self):
        for url in (reverse('create_quotation', args=[self.barmak_lead.pk]),):
            response = self.client.get(url)
            self.assertIn('bot/pages/quote_sectioned_form.html',
                          [t.name for t in response.templates])

    def test_other_tenants_keep_the_flat_layout(self):
        """The switch is one tenant's profile data — another tenant's quote
        must never pick it up."""
        response = self.hb_client.get(
            reverse('create_quotation', args=[self.homebase_lead.pk]))
        names = [t.name for t in response.templates]
        self.assertIn('bot/pages/quote_flat_form.html', names)
        self.assertNotIn('bot/pages/quote_sectioned_form.html', names)

    def test_no_barmak_letterhead_value_reaches_another_tenant(self):
        """The hard rule: nothing on Barmak's letterhead may appear on a
        Homebase quote, whichever screen it is."""
        pages = [
            reverse('create_quotation', args=[self.homebase_lead.pk]),
            reverse('quotations_list'),
        ]
        quote = Quotation.objects.create(appointment=self.homebase_lead)
        QuotationItem.objects.create(
            quotation=quote, description='Tap', quantity=1, unit_price=Decimal('25'))
        pages += [reverse('view_quotation', args=[quote.pk]),
                  reverse('edit_quotation', args=[quote.pk])]

        leaky = ['ROYAL HARDWARE', '1154714543', 'barmakplumbing.co.zw',
                 'K. Marange', 'Budiriro']
        for url in pages:
            html = self._html(url, client=self.hb_client)
            for value in leaky:
                self.assertNotIn(value, html, f'{value} leaked onto {url}')

    def test_letterhead_renders_from_the_tenant_profile(self):
        html = self._html(reverse('create_quotation', args=[self.barmak_lead.pk]))
        for value in ('Barmak Plumbing', 'ROYAL HARDWARE', '20398 Budiriro 5B Cabs Harare',
                      '+263 77 387 1503', 'info@barmakplumbing.co.zw',
                      'CABS', '1154714543', 'Director K. Marange',
                      'Quality is our mission', 'deposit 75%'):
            self.assertIn(value, html, f'{value} missing from the sheet')

    def test_a_tenant_without_letterhead_facts_omits_them(self):
        """Absent means omit, never borrow: layout on, nothing else set."""
        bare = Tenant.objects.create(name='Bare Plumbing', slug='bare')
        TenantProfile.objects.create(tenant=bare, letterhead={'layout': 'sectioned'})
        lead = make_lead(7103, tenant=bare)

        from django.test import Client
        bare_client = Client()
        bare_client.force_login(self._staff('bare-staff', bare))

        html = self._html(reverse('create_quotation', args=[lead.pk]), client=bare_client)
        self.assertIn('Bare Plumbing', html)
        self.assertNotIn('Banking Details', html)
        self.assertNotIn('t / a', html)
        self.assertNotIn('Authorized by', html)

    # ── saving ──────────────────────────────────────────────────────────
    def _save_payload(self):
        return {
            'appointment_id': self.barmak_lead.pk,
            'client_name': 'B Client',
            'client_phone': '+263771111111',
            'client_address': 'Budiriro 5',
            'client_email': 'b@example.com',
            'items': [
                {'name': 'Basin mixer', 'section': 'PLUMBING MATERIALS',
                 'qty_text': '2 pcs', 'qty': 2, 'unit': 40},
                {'name': 'PVC pipe', 'section': 'PLUMBING MATERIALS',
                 'qty_text': '19 length', 'qty': 19, 'unit': 0},
                {'name': 'Angle valve', 'section': 'FITTINGS',
                 'qty_text': '3', 'qty': 3, 'unit': 3},
            ],
            'labour_cost': 100,
            'transport_cost': 20,
            'discount': 9,
            'vat_percent': 15,
            'materials_cost': 0,
            'terms': ['deposit 75%', 'Balance on completion'],
        }

    def test_saving_keeps_sections_quantities_discount_and_vat(self):
        response = self.client.post(
            reverse('create_quotation_api'),
            data=json.dumps(self._save_payload()),
            content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()
        self.assertTrue(result['success'], result)

        quote = Quotation.objects.get(pk=result['quotation_id'])
        items = list(quote.items.all())
        self.assertEqual([i.section for i in items],
                         ['PLUMBING MATERIALS', 'PLUMBING MATERIALS', 'FITTINGS'])
        self.assertEqual([i.quantity_text for i in items], ['2 pcs', '19 length', '3'])
        self.assertEqual(quote.discount, Decimal('9.00'))
        self.assertEqual(quote.vat_percent, Decimal('15.00'))
        # terms live in notes, one per line, on a sectioned quote
        self.assertEqual(quote.notes.splitlines(), ['deposit 75%', 'Balance on completion'])

    def test_the_saved_total_is_what_the_sheet_showed(self):
        """materials 89 + labour 100 + transport 20 = 209, less 9 discount
        = 200, plus 15% VAT = 230."""
        response = self.client.post(
            reverse('create_quotation_api'),
            data=json.dumps(self._save_payload()),
            content_type='application/json')
        quote = Quotation.objects.get(pk=response.json()['quotation_id'])
        self.assertEqual(quote.total_amount, Decimal('230.00'))

    def test_reopening_rebuilds_the_same_sections(self):
        response = self.client.post(
            reverse('create_quotation_api'),
            data=json.dumps(self._save_payload()),
            content_type='application/json')
        quote = Quotation.objects.get(pk=response.json()['quotation_id'])

        from .views.quote_layout import sections_payload
        payload = sections_payload(quote)
        self.assertEqual([g['title'] for g in payload],
                         ['PLUMBING MATERIALS', 'FITTINGS'])
        self.assertEqual([i['qty'] for i in payload[0]['items']], ['2 pcs', '19 length'])

    def test_the_saved_sheet_shows_its_section_subtotals(self):
        response = self.client.post(
            reverse('create_quotation_api'),
            data=json.dumps(self._save_payload()),
            content_type='application/json')
        quote = Quotation.objects.get(pk=response.json()['quotation_id'])

        html = self._html(reverse('view_quotation', args=[quote.pk]))
        self.assertIn('PLUMBING MATERIALS', html)
        self.assertIn('FITTINGS', html)
        self.assertIn('19 length', html)
        self.assertIn('230.00', html)          # grand total
        self.assertIn('SUB-TOTAL', html)

    def test_editing_a_sectioned_quote_uses_the_same_sheet(self):
        quote = Quotation.objects.create(appointment=self.barmak_lead)
        response = self.client.get(reverse('edit_quotation', args=[quote.pk]))
        self.assertIn('bot/pages/quote_sectioned_form.html',
                      [t.name for t in response.templates])

    # ── the Profile page owns the letterhead ────────────────────────────
    def test_letterhead_is_editable_on_the_profile_page(self):
        html = self._html(reverse('profile'))
        self.assertIn('name="lh_trading_name"', html)
        self.assertIn('name="lh_bank_account_number"', html)
        self.assertIn('ROYAL HARDWARE', html)

    def test_saving_the_profile_form_updates_the_quote(self):
        response = self.client.post(reverse('profile'), {
            'letterhead_submit': '1',
            'lh_sectioned': 'on',
            'lh_trading_name': 'ROYAL HARDWARE',
            'lh_address': '1 New Road, Harare',
            'lh_phones': '+263 700 000 001\n+263 700 000 002',
            'lh_public_email': 'hello@barmakplumbing.co.zw',
            'lh_website': 'www.barmakplumbing.co.zw',
            'lh_services_blurb': 'Everything plumbing.',
            'lh_maintenance_blurb': 'Leaks and blockages.',
            'lh_bank_account_name': 'Barmak Plumbing Private Limited',
            'lh_bank_bank_name': 'CABS',
            'lh_bank_branch': 'Park street',
            'lh_bank_account_number': '9999999999',
            'lh_terms': 'deposit 50%',
            'lh_default_vat_percent': '14.5',
            'lh_signatory': 'Director K. Marange',
            'lh_tagline': 'Quality is our mission',
        })
        self.assertEqual(response.status_code, 302)

        profile = TenantProfile.objects.get(tenant=self.barmak)
        self.assertEqual(profile.letterhead['phones'],
                         ['+263 700 000 001', '+263 700 000 002'])
        self.assertEqual(profile.letterhead['bank']['account_number'], '9999999999')
        self.assertEqual(profile.location_line, '1 New Road, Harare')

        # and it reaches the quote sheet
        html = self._html(reverse('create_quotation', args=[self.barmak_lead.pk]))
        self.assertIn('9999999999', html)
        self.assertIn('1 New Road, Harare', html)
        self.assertIn('deposit 50%', html)

    # ── the consultation fee: how a plumber says the visit is not free ──────
    def test_consultation_fee_input_is_on_the_profile_page(self):
        html = self._html(reverse('profile'))
        self.assertIn('name="consultation_fee"', html)

    def test_setting_and_clearing_the_consultation_fee(self):
        from .tenant_config import get_config

        # Default: nothing set, so the visit is free and the copy is untouched.
        profile = self.barmak_lead.tenant.profile
        self.assertIsNone(profile.consultation_fee)
        self.assertTrue(get_config(self.barmak_lead.tenant).visit_is_free())

        # A plumber types a figure on the Profile page.
        self.client.post(reverse('profile'), {
            'consultation_fee_submit': '1',
            'consultation_fee': '25',
        })
        profile.refresh_from_db()
        self.assertEqual(int(profile.consultation_fee), 25)
        cfg = get_config(self.barmak_lead.tenant)
        self.assertFalse(cfg.visit_is_free())
        self.assertIn('25', cfg.visit_cost_sentence())

        # It renders back into the form, so it can be seen and edited.
        self.assertIn('value="25', self._html(reverse('profile')))

        # A currency symbol typed by hand is accepted, not rejected.
        self.client.post(reverse('profile'), {
            'consultation_fee_submit': '1', 'consultation_fee': 'US$40'})
        profile.refresh_from_db()
        self.assertEqual(int(profile.consultation_fee), 40)

        # Nonsense does not raise or wipe the rest of the profile save.
        self.client.post(reverse('profile'), {
            'consultation_fee_submit': '1', 'consultation_fee': 'abc'})
        profile.refresh_from_db()
        self.assertIsNone(profile.consultation_fee)

        # Blank clears it, and the visit is free again.
        self.client.post(reverse('profile'), {
            'consultation_fee_submit': '1', 'consultation_fee': ''})
        profile.refresh_from_db()
        self.assertIsNone(profile.consultation_fee)
        self.assertTrue(get_config(self.barmak_lead.tenant).visit_is_free())

    def test_unticking_the_layout_returns_that_tenant_to_the_flat_quote(self):
        self.client.post(reverse('profile'), {
            'letterhead_submit': '1',
            'lh_trading_name': 'ROYAL HARDWARE',
        })
        response = self.client.get(
            reverse('create_quotation', args=[self.barmak_lead.pk]))
        self.assertIn('bot/pages/quote_flat_form.html',
                      [t.name for t in response.templates])

    # ── the seed migration ──────────────────────────────────────────────
    def test_seed_migration_only_fills_blanks_and_only_for_barmak(self):
        """Migration 0070 runs on the live database. Its job is to set Barmak
        up without trampling anything the operator has since edited, and to
        leave every other tenant alone."""
        import importlib
        module = importlib.import_module('bot.migrations.0070_seed_barmak_letterhead')

        class Apps:
            @staticmethod
            def get_model(app_label, name):
                from django.apps import apps as django_apps
                return django_apps.get_model(app_label, name)

        # Someone has already edited the bank account on the Profile page.
        profile = TenantProfile.objects.get(tenant=self.barmak)
        profile.letterhead = dict(profile.letterhead, bank={'account_number': 'EDITED'})
        profile.save(update_fields=['letterhead'])

        module.seed(Apps, None)

        profile.refresh_from_db()
        self.assertEqual(profile.letterhead['bank']['account_number'], 'EDITED',
                         'the seed overwrote an edit the operator had made')
        self.assertEqual(profile.letterhead['layout'], 'sectioned')

        # And it touches nobody else. (Homebase is seeded with a profile by
        # the test-DB post_migrate hook, mirroring migration 0041.)
        other, _ = TenantProfile.objects.get_or_create(tenant=self.homebase)
        before = dict(other.letterhead or {})
        module.seed(Apps, None)
        other.refresh_from_db()
        self.assertEqual(other.letterhead or {}, before)
        self.assertNotIn('layout', other.letterhead or {})

    def test_flat_quotes_still_total_exactly_as_before(self):
        """discount and vat_percent default to 0, so an untouched quote's
        total is unchanged by their arrival."""
        quote = Quotation.objects.create(
            appointment=self.homebase_lead, labor_cost=Decimal('50'),
            transport_cost=Decimal('10'), materials_cost=Decimal('40'))
        # 25 (item) + 50 + 40 + 10 — the pre-existing sum, untouched.
        QuotationItem.objects.create(
            quotation=quote, description='Tap', quantity=1, unit_price=Decimal('25'))
        quote.refresh_from_db()
        self.assertEqual(quote.total_amount, Decimal('125.00'))


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

    def test_due_followups_never_leak_across_tenants(self):
        # Regression: _due_followup_leads(tenant=…) must scope to that tenant.
        # The cron's _get_eligible_leads spans ALL tenants (it sends for
        # everyone); the dashboard/workspace helper forgot to re-scope it, so
        # one tenant's follow-up list showed every tenant's due leads — and each
        # leaked row then 404'd on click, since the detail views are scoped.
        from unittest.mock import patch
        from datetime import timedelta
        from django.utils import timezone
        from bot.views.dashboard import _due_followup_leads
        from bot.management.commands.send_followups import Command as FCmd

        old = timezone.now() - timedelta(hours=2)
        for lead in (self.hb_lead, self.acme_lead):
            lead.is_lead_active = True
            lead.status = 'pending'
            lead.is_delayed = False
            lead.followup_stage = 'none'
            lead.last_customer_response = old
            lead.save()

        # Isolate the tenant-scoping seam: force readiness + open window so the
        # only variable left is which tenant's leads come back.
        with patch.object(FCmd, '_is_ready_for_followup', return_value=(True, 'ok')), \
             patch.object(Appointment, 'messaging_window_open',
                          property(lambda self: True)):
            hb_due = _due_followup_leads(tenant=self.homebase)
            acme_due = _due_followup_leads(tenant=self.acme)

        self.assertTrue(all(l.tenant_id == self.homebase.pk for l in hb_due))
        self.assertTrue(all(l.tenant_id == self.acme.pk for l in acme_due))
        # The acme lead surfaces under acme and NEVER under homebase.
        self.assertIn(self.acme_lead.pk, [l.pk for l in acme_due])
        self.assertNotIn(self.acme_lead.pk, [l.pk for l in hb_due])


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
        # Tenant staff cannot delete at all now — owner-only action.
        response = self.client.post(reverse('delete_appointment', args=[self.hb_lead.pk]))
        self.assertEqual(response.status_code, 403)
        self.assertTrue(Appointment.objects.filter(pk=self.hb_lead.pk).exists())

    @override_settings(PLATFORM_OWNER_ACCOUNTS=['root-scoped'])
    def test_owner_cannot_delete_outside_the_tenant_they_are_viewing(self):
        """Even for the owner, deleting across tenants is impossible rather than
        merely forbidden — the tenant lens still scopes it."""
        get_user_model().objects.create_superuser(
            username='root-scoped', password='pass12345', email='r@example.com')
        self.client.login(username='root-scoped', password='pass12345')
        self.client.post(reverse('switch_tenant'), {'tenant': self.acme.slug})
        response = self.client.post(
            reverse('delete_appointment', args=[self.hb_lead.pk]))
        self.assertEqual(response.status_code, 404)
        self.assertTrue(Appointment.objects.filter(pk=self.hb_lead.pk).exists())

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
                           ('platform_tenant_config', ['homebase']),
                           ('platform_tenant_config_edit', ['homebase'])]:
            response = self.client.get(reverse(name, args=args))
            self.assertIn(response.status_code, (302, 403), name)

    def test_tenant_config_renders_every_switch_on_for_homebase(self):
        response = self.client.get(
            reverse('platform_tenant_config', args=['homebase']))
        body = response.content.decode()
        for key in ('batch_window_enabled', 'reply_delay_enabled',
                    'email_sending_enabled'):
            self.assertIn(
                reverse('platform_toggle_timer', args=['homebase', key]), body)
        # Timers default ON everywhere; email defaults ON for homebase only.
        import re
        switches = re.findall(r'<input type="checkbox" name="enabled"[^>]*>', body)
        self.assertEqual(len(switches), 3)
        self.assertTrue(all('checked' in s for s in switches), switches)

    def test_email_switch_defaults_off_for_every_other_tenant(self):
        from .platform_flags import email_sending_enabled
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.assertTrue(email_sending_enabled(self.homebase))
        self.assertFalse(email_sending_enabled(acme))
        # ...and the console shows it off, ready to be switched on per tenant.
        body = self.client.get(
            reverse('platform_tenant_config', args=['acme'])).content.decode()
        import re
        email_url = reverse('platform_toggle_timer',
                            args=['acme', 'email_sending_enabled'])
        self.assertIn(email_url, body)
        form = body.split(email_url, 1)[1]
        switch = re.search(r'<input type="checkbox" name="enabled"[^>]*>', form)
        self.assertNotIn('checked', switch.group(0))
        # "unless stated otherwise" — the switch turns it on for that tenant only.
        self.client.post(email_url, {'enabled': '1'})
        self.assertTrue(email_sending_enabled(acme))

    def test_no_email_leaves_the_platform_for_a_disabled_tenant(self):
        """The gate sits at the single choke point every send goes through, so a
        tenant with email off cannot mail anyone — however the send was reached."""
        from .plumber_notifications import send_email_to_recipients
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        with patch('bot.plumber_notifications._send_via_brevo') as brevo, \
             patch('bot.plumber_notifications._send_via_sendgrid') as sendgrid:
            sent = send_email_to_recipients(
                ['lead@example.com'], 'Booking confirmed', 'body', tenant=acme)
            self.assertFalse(sent)
            brevo.assert_not_called()
            sendgrid.assert_not_called()
        # Platform mail (password resets) carries no tenant and still goes out.
        with patch('bot.plumber_notifications._send_via_brevo',
                   return_value=True) as brevo:
            with self.settings(BREVO_API_KEY='x'):
                self.assertTrue(send_email_to_recipients(
                    ['staff@example.com'], 'Reset your password', 'body'))
            brevo.assert_called_once()

    def test_toggle_timer_persists_and_flips_back(self):
        from .platform_flags import batch_window_enabled, reply_delay_enabled
        url = reverse('platform_toggle_timer',
                      args=['homebase', 'reply_delay_enabled'])
        # Unchecked checkbox posts nothing — that's the OFF signal.
        self.assertEqual(self.client.post(url, {}).status_code, 302)
        self.assertFalse(reply_delay_enabled(self.homebase))
        self.assertTrue(batch_window_enabled(self.homebase))   # independent switches
        self.client.post(url, {'enabled': '1'})
        self.assertTrue(reply_delay_enabled(self.homebase))
        self.assertEqual(TenantSetting.objects.filter(
            tenant=self.homebase, key='reply_delay_enabled').count(), 1)  # upsert

    def test_each_tenant_keeps_its_own_switches(self):
        from .platform_flags import reply_delay_enabled
        acme = Tenant.objects.create(name='Acme', slug='acme')
        self.client.post(
            reverse('platform_toggle_timer', args=['acme', 'reply_delay_enabled']), {})
        self.assertFalse(reply_delay_enabled(acme))
        self.assertTrue(reply_delay_enabled(self.homebase))  # untouched neighbour
        # The console flags the exception on acme's row only.
        body = self.client.get(reverse('platform_console')).content.decode()
        self.assertEqual(body.count('Human reply delay off'), 1)

    def test_unknown_timer_key_404s(self):
        response = self.client.post(
            reverse('platform_toggle_timer', args=['homebase', 'not-a-switch']))
        self.assertEqual(response.status_code, 404)

    def test_unknown_tenant_404s(self):
        response = self.client.post(reverse(
            'platform_toggle_timer', args=['ghost-co', 'reply_delay_enabled']))
        self.assertEqual(response.status_code, 404)

    def test_plain_staff_cannot_flip_a_timer(self):
        get_user_model().objects.create_user(
            username='plainstaff3', password='pass12345', is_staff=True)
        self.client.login(username='plainstaff3', password='pass12345')
        response = self.client.post(reverse(
            'platform_toggle_timer', args=['homebase', 'reply_delay_enabled']))
        self.assertIn(response.status_code, (302, 403))
        self.assertFalse(TenantSetting.objects.exists())

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

    def test_config_view_page_renders_readonly(self):
        # Display page: read-only overview with an Edit button, no form POST.
        from .models import TenantPriceItem
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        TenantProfile.objects.create(
            tenant=acme, plumber_name='Blessing', currency='ZAR',
            business_hours={'days': 'Monday-Friday', 'open': '08:00', 'close': '17:00'},
            excluded_areas=['bulawayo'], faq_facts={'free_quote': 'Yes, free.'})
        TenantPriceItem.objects.create(
            tenant=acme, family='shower', variant='',
            label='shower cubicle', supply=130, labour=40, allin=170)
        response = self.client.get(reverse('platform_tenant_config', args=['acme']))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn('shower cubicle', body)
        self.assertIn('all-in', body)                       # rendered price line
        self.assertIn('ZAR', body)                           # tenant currency, not US$
        self.assertIn('Monday to Friday', body)              # hours sentence via cfg
        self.assertIn('Bulawayo', body)                      # declined-area tag
        self.assertIn('Free Quote', body)                    # FAQ topic tag
        self.assertIn(reverse('platform_tenant_config_edit', args=['acme']), body)

    def test_operator_sets_the_call_out_fee_for_a_tenant(self):
        """The tenant can set this on their own Profile page; the operator must
        be able to do it for them during setup, from the console."""
        from .tenant_config import get_config
        body = self.client.get(
            reverse('platform_tenant_config_edit', args=['homebase'])).content.decode()
        self.assertIn('name="consultation_fee"', body)

        base = {
            'plumber_name': 'Takudzwa', 'plumber_contact': '+263774819901',
            'business_whatsapp': '+263776255077',
            'location_line': "We're in Hatfield, Harare.",
            'location_area': 'Hatfield', 'location_city': 'Harare',
            'timezone_name': 'Africa/Johannesburg', 'currency': 'US$',
            'email_from_name': 'Takudzwa', 'email_sender': '',
            'hours_day': ['monday'], 'hours_open': '08:00', 'hours_close': '18:00',
            'form-TOTAL_FORMS': '0', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
        }
        # Default: no fee, so the assistant may keep calling the visit free.
        self.assertTrue(get_config(self.homebase).visit_is_free())

        response = self.client.post(
            reverse('platform_tenant_config_edit', args=['homebase']),
            dict(base, consultation_fee='30'))
        self.assertEqual(response.status_code, 302)
        profile = TenantProfile.objects.get(tenant=self.homebase)
        self.assertEqual(int(profile.consultation_fee), 30)
        cfg = get_config(self.homebase)
        self.assertFalse(cfg.visit_is_free())
        self.assertIn('30', cfg.visit_cost_sentence())

        # And clearing it puts the tenant back to a free visit.
        self.client.post(
            reverse('platform_tenant_config_edit', args=['homebase']),
            dict(base, consultation_fee=''))
        profile.refresh_from_db()
        self.assertIsNone(profile.consultation_fee)
        self.assertTrue(get_config(self.homebase).visit_is_free())

    def test_config_edit_page_renders_and_saves(self):
        response = self.client.get(reverse('platform_tenant_config_edit', args=['homebase']))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # The raw JSON textareas are gone — structured inputs in their place.
        self.assertNotIn('name="business_hours"', body)
        self.assertNotIn('name="faq_facts"', body)
        self.assertIn('name="hours_day"', body)
        self.assertIn('name="excluded_area"', body)
        self.assertIn('name="faq_payment"', body)
        # Structured POST: day chips + times, area chips, per-topic FAQ fields.
        data = {
            'plumber_name': 'Takudzwa', 'plumber_contact': '+263774819901',
            'business_whatsapp': '+263776255077',
            'location_line': "We're in Hatfield, Harare.",
            'location_area': 'Hatfield', 'location_city': 'Harare',
            'timezone_name': 'Africa/Johannesburg', 'currency': 'US$',
            'email_from_name': 'Takudzwa', 'email_sender': '',
            'hours_day': ['sunday', 'monday', 'tuesday', 'wednesday', 'thursday', 'friday'],
            'hours_open': '08:00', 'hours_close': '18:00',
            'excluded_area': ['Gweru', 'Bulawayo'],
            'faq_payment': 'Cash and EcoCash — all good.',
            'form-TOTAL_FORMS': '0', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
        }
        response = self.client.post(
            reverse('platform_tenant_config_edit', args=['homebase']), data)
        self.assertEqual(response.status_code, 302)  # → back to the read-only view
        profile = TenantProfile.objects.get(tenant=self.homebase)
        self.assertEqual(profile.excluded_areas, ['gweru', 'bulawayo'])   # chips → list
        self.assertEqual(profile.business_hours['open'], '08:00')          # times composed
        self.assertEqual(profile.business_hours['closed'], ['sat'])        # unpicked day
        self.assertEqual(profile.faq_facts['payment'], 'Cash and EcoCash — all good.')
        # Not ticked → no 24/7 promise anywhere on the profile.
        self.assertNotIn('emergency_24h', profile.business_hours)

    def test_config_edit_saves_the_24h_emergency_tick(self):
        """A business that answers callouts round the clock ticks it on top of
        its regular week; the flag rides on the same business_hours JSON and
        reaches the bot's copy."""
        response = self.client.get(reverse('platform_tenant_config_edit', args=['homebase']))
        self.assertIn('name="hours_emergency_24h"', response.content.decode())
        data = {
            'plumber_name': 'Takudzwa', 'plumber_contact': '+263774819901',
            'business_whatsapp': '+263776255077',
            'location_line': "We're in Hatfield, Harare.",
            'location_area': 'Hatfield', 'location_city': 'Harare',
            'timezone_name': 'Africa/Johannesburg', 'currency': 'US$',
            'email_from_name': 'Takudzwa', 'email_sender': '',
            'hours_day': ['monday', 'tuesday', 'wednesday', 'thursday', 'friday'],
            'hours_open': '08:00', 'hours_close': '17:00',
            'hours_emergency_24h': 'on',
            'form-TOTAL_FORMS': '0', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
        }
        response = self.client.post(
            reverse('platform_tenant_config_edit', args=['homebase']), data)
        self.assertEqual(response.status_code, 302)
        profile = TenantProfile.objects.get(tenant=self.homebase)
        self.assertTrue(profile.business_hours['emergency_24h'])
        self.assertEqual(profile.business_hours['open'], '08:00')   # week intact
        from .tenant_config import get_config
        cfg = get_config(self.homebase)
        self.assertTrue(cfg.emergency_24h())
        self.assertIn('24/7', cfg.emergency_sentence())
        # And it shows on the read-only page the owner lands back on.
        body = self.client.get(
            reverse('platform_tenant_config', args=['homebase'])).content.decode()
        self.assertIn('On call 24/7', body)

    def test_owner_sets_and_previews_the_clients_customer_sender(self):
        """The platform owner sets a tenant's customer-facing sender from the
        config editor and sees the resolved identity previewed on both pages —
        the same resolution the send path performs, not just the raw field."""
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        TenantProfile.objects.create(tenant=acme)
        # Editor previews the fallback while no own-domain address is set.
        body = self.client.get(
            reverse('platform_tenant_config_edit', args=['acme'])).content.decode()
        self.assertIn('name="customer_from_email"', body)
        self.assertIn('id="preview-customer-sender"', body)
        self.assertIn('acme@notifications.homexmedia.com', body)

        data = {
            'plumber_name': '', 'plumber_contact': '', 'business_whatsapp': '',
            'location_line': '', 'location_area': '', 'location_city': '',
            'timezone_name': '', 'currency': 'US$',
            'email_from_name': 'Acme Plumbing',
            'email_sender': 'alerts@acmeplumbing.co.zw',
            'customer_from_email': 'info@acmeplumbing.co.zw',
            'form-TOTAL_FORMS': '0', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
        }
        response = self.client.post(
            reverse('platform_tenant_config_edit', args=['acme']), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            TenantProfile.objects.get(tenant=acme).customer_from_email,
            'info@acmeplumbing.co.zw')

        body = self.client.get(
            reverse('platform_tenant_config', args=['acme'])).content.decode()
        self.assertIn('Acme Plumbing &lt;info@acmeplumbing.co.zw&gt;', body)
        self.assertIn('acme@notifications.homexmedia.com', body)  # internal sender
        self.assertIn('alerts@acmeplumbing.co.zw', body)          # alerts inbox
        # The platform operator inbox stays a bcc, never a To recipient.
        self.assertIn('(bcc)', body)

    def test_new_tenant_sheet_prefills_catalogue_prices_blank(self):
        from .models import TenantPriceItem
        from .tenant_config import blank_priced_catalog
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        response = self.client.get(reverse('platform_tenant_config_edit', args=['acme']))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Item labels from homebase's catalogue are prefilled…
        self.assertIn('shower cubicle', body)
        self.assertIn('freestanding tub', body)
        # …the freestanding tub's component names ride along as a breakdown…
        self.assertIn('Breakdown', body)
        self.assertIn('mixer', body)
        # …but nothing is persisted and no figure is prefilled.
        self.assertFalse(TenantPriceItem.objects.filter(tenant=acme).exists())
        self.assertTrue(len(blank_priced_catalog()) > 10)
        # Currency is indicated on the money fields; All-in is flagged auto;
        # items are laid out as horizontal grid rows; Flat is gone; Add item is offered.
        self.assertIn('class="cur"', body)
        self.assertIn('US$', body)
        self.assertIn('<em>auto</em>', body)
        self.assertIn('ps-row', body)
        self.assertNotIn('<span>Flat</span>', body)
        self.assertIn('id="ps-add"', body)

    def test_add_custom_item_keys_off_the_name(self):
        from .models import TenantPriceItem
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        data = {
            'plumber_name': '', 'plumber_contact': '', 'business_whatsapp': '',
            'location_line': '', 'location_area': '', 'location_city': '',
            'business_hours': '{}', 'timezone_name': '', 'excluded_areas': '[]',
            'currency': 'US$', 'packages': '[]', 'faq_facts': '{}', 'scripts': '{}',
            'email_from_name': '', 'email_sender': '',
            'form-TOTAL_FORMS': '1', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
            # A custom "Add item" row: no family posted, keyed off the label.
            'form-0-family': '', 'form-0-variant': '', 'form-0-label': 'Underfloor Heating',
            'form-0-short_label': '', 'form-0-supply': '', 'form-0-labour': '',
            'form-0-allin': '300', 'form-0-parts': '[]',
            'form-0-sort_order': '0', 'form-0-is_active': 'on',
        }
        response = self.client.post(
            reverse('platform_tenant_config_edit', args=['acme']), data)
        self.assertEqual(response.status_code, 302)
        item = TenantPriceItem.objects.get(tenant=acme, label='Underfloor Heating')
        self.assertEqual(item.family, 'underfloor-heating')
        self.assertEqual(int(item.allin), 300)

    def test_existing_tenant_gets_missing_catalogue_items_prefilled(self):
        from .models import TenantPriceItem
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        # Acme already priced one item and has a bespoke row not in the catalogue.
        TenantPriceItem.objects.create(
            tenant=acme, family='shower', variant='', label='shower cubicle', allin=170)
        TenantPriceItem.objects.create(
            tenant=acme, family='other', variant='', label='Geyser link', flat=5)
        response = self.client.get(reverse('platform_tenant_config_edit', args=['acme']))
        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        # Missing catalogue items are still offered to fill in…
        self.assertIn('freestanding tub', body)
        self.assertIn('vanity', body)
        # …the already-priced item keeps its figure and isn't re-added blank…
        self.assertEqual(TenantPriceItem.objects.filter(
            tenant=acme, family='shower').count(), 1)
        self.assertEqual(body.count('shower cubicle'), 1)  # not duplicated blank
        self.assertIn('value="170', body)                  # its figure is rendered
        # …and the bespoke non-catalogue row survives untouched.
        self.assertIn('Geyser link', body)

    def test_only_priced_rows_persist_incl_component_amounts(self):
        from .models import TenantPriceItem
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        base = {
            'plumber_name': 'A', 'plumber_contact': '+263700000000',
            'business_whatsapp': '', 'location_line': '', 'location_area': '',
            'location_city': '', 'business_hours': '{}', 'timezone_name': '',
            'excluded_areas': '[]', 'currency': 'US$', 'packages': '[]',
            'faq_facts': '{}', 'scripts': '{}', 'email_from_name': '', 'email_sender': '',
            'form-TOTAL_FORMS': '3', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
        }
        rows = {
            # 0: priced by all-in → persists
            'form-0-family': 'shower', 'form-0-variant': '', 'form-0-label': 'shower cubicle',
            'form-0-short_label': '', 'form-0-supply': '', 'form-0-labour': '',
            'form-0-flat': '', 'form-0-allin': '170', 'form-0-parts': '[]',
            'form-0-sort_order': '0', 'form-0-is_active': 'on',
            # 1: no figure anywhere → dropped
            'form-1-family': 'toilet', 'form-1-variant': '', 'form-1-label': 'toilet seat',
            'form-1-short_label': '', 'form-1-supply': '', 'form-1-labour': '',
            'form-1-flat': '', 'form-1-allin': '', 'form-1-parts': '[]',
            'form-1-sort_order': '1', 'form-1-is_active': 'on',
            # 2: only a component amount → persists (breakdown counts as priced)
            'form-2-family': 'tub', 'form-2-variant': 'freestanding',
            'form-2-label': 'freestanding tub', 'form-2-short_label': '',
            'form-2-supply': '', 'form-2-labour': '', 'form-2-flat': '', 'form-2-allin': '',
            'form-2-parts': '[{"name": "mixer", "amount": 150}]',
            'form-2-sort_order': '2', 'form-2-is_active': 'on',
        }
        base.update(rows)
        response = self.client.post(
            reverse('platform_tenant_config_edit', args=['acme']), base)
        self.assertEqual(response.status_code, 302)
        families = set(TenantPriceItem.objects.filter(
            tenant=acme).values_list('family', flat=True))
        self.assertEqual(families, {'shower', 'tub'})  # toilet dropped
        tub = TenantPriceItem.objects.get(tenant=acme, family='tub', variant='freestanding')
        self.assertEqual(tub.parts, [{'name': 'mixer', 'amount': 150}])


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

    def test_intake_carries_the_24h_emergency_tick_through_approval(self):
        """The wizard's 24/7 chip survives the draft and lands on the live
        profile at approval, on top of the regular week."""
        self._submit({'hours_emergency_24h': '1'})
        self.intake.refresh_from_db()
        self.assertTrue(self.intake.data['hours']['emergency_24h'])
        self.client.login(username='root', password='pass12345')
        self.client.post(reverse('platform_review_intake', args=[self.intake.pk]),
                         {'decision': 'approve'})
        profile = TenantProfile.objects.get(tenant=self.acme)
        self.assertTrue(profile.business_hours['emergency_24h'])
        self.assertEqual(profile.business_hours['open'], '07:00')

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
        # Emergency tick untouched by this business → no borrowed 24/7 promise.
        self.assertNotIn('emergency_24h', profile.business_hours)
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

    def test_price_rows_carry_the_full_breakdown(self):
        # The wizard posts the same columns as the platform price sheet:
        # supply/labour/all-in plus a component breakdown for composed items.
        import json as _json
        self._submit({
            'price_label': ['Geyser supply & install', 'Freestanding tub'],
            'price_family': ['geyser', 'tub'],
            'price_variant': ['', 'freestanding'],
            'price_supply': ['90', ''], 'price_labour': ['60', ''],
            'price_allin': ['150', '670'],
            'price_parts': ['[]', _json.dumps([
                {'name': 'tub', 'amount': 400}, {'name': 'mixer', 'amount': 150},
                {'name': 'install', 'amount': 120},
                {'name': 'unpriced', 'amount': ''}])],
        })
        self.intake.refresh_from_db()
        tub_draft = self.intake.data['prices'][1]
        self.assertEqual(tub_draft['parts'], [
            {'name': 'tub', 'amount': 400}, {'name': 'mixer', 'amount': 150},
            {'name': 'install', 'amount': 120}])   # the amount-less part dropped
        self.assertNotIn('parts', self.intake.data['prices'][0])

        self.client.login(username='root', password='pass12345')
        self.client.post(reverse('platform_review_intake', args=[self.intake.pk]),
                         {'decision': 'approve'})
        from .models import TenantPriceItem
        tub = TenantPriceItem.objects.get(tenant=self.acme, family='tub', variant='freestanding')
        self.assertEqual(int(tub.allin), 670)
        self.assertEqual([p['name'] for p in tub.parts], ['tub', 'mixer', 'install'])
        geyser = TenantPriceItem.objects.get(tenant=self.acme, family='geyser', variant='')
        self.assertEqual((int(geyser.supply), int(geyser.labour)), (90, 60))
        self.assertEqual(geyser.parts, [])

    def test_form_embeds_catalogue_breakdowns(self):
        # The breakdown lines are driven by the platform catalogue, not a copy
        # kept in the template.
        body = self.client.get(f'/intake/{self.intake.token}/').content.decode()
        self.assertIn('tub|freestanding', body)
        self.assertIn('mixer', body)

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


class SendCostCaptureTests(TestCase):
    """Meta's status webhook carries its own billing verdict; we persist it so
    the messaging-window assumption can be checked against real traffic instead
    of guessed at. Nothing here sends anything — the whole point is that the
    evidence is free to collect."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.lead = make_lead(9960, tenant=self.homebase)
        self.lead.last_inbound_at = timezone.now() - timedelta(hours=30)
        self.lead.ctwa_entry_at = timezone.now() - timedelta(hours=40)
        self.lead.save()

    def _status(self, **over):
        payload = {
            'id': 'wamid.COST1',
            'status': 'delivered',
            'recipient_id': self.lead.phone_number.replace('whatsapp:+', ''),
            'timestamp': '1700000000',
            'pricing': {'billable': False, 'pricing_model': 'PMP',
                        'type': 'free_customer_service', 'category': 'service'},
        }
        payload.update(over)
        return payload

    def test_pricing_verdict_is_recorded_with_our_window_state(self):
        from bot.whatsapp_webhook import process_status_updates
        process_status_updates([self._status()], tenant=self.homebase)

        row = WhatsAppSendCost.objects.get(message_id='wamid.COST1')
        self.assertIs(row.billable, False)
        self.assertEqual(row.pricing_type, 'free_customer_service')
        self.assertEqual(row.category, 'service')
        self.assertEqual(row.appointment_id, self.lead.id)
        # Our believed window state is stamped alongside Meta's verdict — that
        # pairing is what makes the CTWA 72h assumption falsifiable.
        self.assertTrue(row.was_ctwa_lead)
        self.assertAlmostEqual(row.hours_since_last_inbound, 30, delta=1)
        self.assertAlmostEqual(row.hours_since_ctwa_entry, 40, delta=1)

    def test_repeat_statuses_upsert_and_never_blank_a_known_verdict(self):
        from bot.whatsapp_webhook import process_status_updates
        # 'sent' usually carries no pricing; 'delivered' does. Order must not
        # matter, and the later pricing-free status must not erase the verdict.
        process_status_updates([self._status()], tenant=self.homebase)
        process_status_updates(
            [self._status(status='read', pricing={})], tenant=self.homebase)

        self.assertEqual(WhatsAppSendCost.objects.count(), 1)
        row = WhatsAppSendCost.objects.get(message_id='wamid.COST1')
        self.assertEqual(row.status, 'read')
        self.assertIs(row.billable, False)
        self.assertEqual(row.pricing_type, 'free_customer_service')

    def test_window_closed_bounce_is_captured_alongside_the_hour_offset(self):
        from bot.whatsapp_webhook import process_status_updates
        process_status_updates([self._status(
            id='wamid.COST2', status='failed', pricing={},
            errors=[{'code': 131047, 'title': 'Re-engagement message'}],
        )], tenant=self.homebase)

        row = WhatsAppSendCost.objects.get(message_id='wamid.COST2')
        self.assertEqual(row.status, 'failed')
        self.assertIn('131047', row.error_codes)
        # A CTWA lead bouncing at 30h is the exact evidence the report looks for.
        self.assertTrue(row.was_ctwa_lead)
        self.assertGreater(row.hours_since_last_inbound, 24)

    def test_report_runs_on_captured_rows(self):
        from bot.whatsapp_webhook import process_status_updates
        process_status_updates([self._status()], tenant=self.homebase)
        out = StringIO()
        call_command('whatsapp_window_report', '--days', '30', stdout=out)
        text = out.getvalue()
        self.assertIn('Verdict coverage', text)
        self.assertIn('free-form sending survive', text)


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
    """Inbound events route to a tenant by metadata.phone_number_id.
    Route-miss is log-and-drop: with more than one live tenant, an unroutable
    event is another business's traffic, not homebase's."""

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

    def test_unknown_id_is_dropped_not_given_to_homebase(self):
        self.assertIsNone(self._resolve({'metadata': {'phone_number_id': 'nope-999'}}))

    def test_missing_metadata_is_dropped(self):
        self.assertIsNone(self._resolve({}))

    def test_inactive_channel_is_not_routable(self):
        from .models import TenantWhatsAppChannel
        TenantWhatsAppChannel.objects.filter(phone_number_id='222000222').update(is_active=False)
        self.assertIsNone(self._resolve({'metadata': {'phone_number_id': '222000222'}}))

    def test_unroutable_event_is_not_processed(self):
        """A route miss must not reach message handling at all — the old
        homebase fallback answered another tenant's lead in homebase's voice."""
        from unittest.mock import patch
        from .whatsapp_webhook import process_message_change
        with patch('bot.whatsapp_webhook.handle_text_message') as handler:
            process_message_change({
                'metadata': {'phone_number_id': 'nope-999'},
                'messages': [{'type': 'text', 'id': 'wamid.X',
                              'from': '27610318200', 'text': {'body': 'Hi'}}],
            })
        self.assertFalse(handler.called)

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

    # ── Delivery statuses are per (tenant, lead), never per phone number ──────
    # One handset can talk to several tenants. Meta's 131047 verdict applies to
    # the business number it bounced on; applying it to whichever row happened to
    # be updated last froze an unrelated tenant's follow-ups (prod, 2026-08-06).

    def _status_payload(self, phone_number_id, recipient):
        return {
            'metadata': {'phone_number_id': phone_number_id},
            'statuses': [{
                'id': 'wamid.STATUS', 'status': 'failed', 'recipient_id': recipient,
                'timestamp': '1754480000',
                'errors': [{'code': 131047, 'title': 'Re-engagement message'}],
            }],
        }

    def test_131047_only_closes_the_window_on_its_own_tenants_lead(self):
        from .whatsapp_webhook import process_message_change
        phone = 'whatsapp:+27610318200'
        hb, _ = Appointment.objects.get_or_create_lead(phone, tenant=self.homebase)
        acme, _ = Appointment.objects.get_or_create_lead(phone, tenant=self.acme)
        # Homebase's row is decisively the most recently touched — the old
        # phone-number-only lookup's `-updated_at` tiebreak would pick it.
        Appointment.objects.filter(pk=hb.pk).update(
            updated_at=timezone.now() + timedelta(hours=1))

        process_message_change(self._status_payload('222000222', '27610318200'))

        hb.refresh_from_db()
        acme.refresh_from_db()
        self.assertTrue(acme.FREEFORM_CLOSED_TAG in (acme.internal_notes or ''))
        self.assertFalse(hb.FREEFORM_CLOSED_TAG in (hb.internal_notes or ''))
        self.assertTrue(hb.messaging_window_open or hb.messaging_window_closes_at is None)

    def test_chatbot_pause_does_not_leak_across_tenants(self):
        """Pausing is per conversation: one tenant taking a lead over by hand
        must not silence another tenant's bot for the same handset."""
        from .whatsapp_webhook import is_chatbot_paused_for_sender
        phone = 'whatsapp:+27610318200'
        hb, _ = Appointment.objects.get_or_create_lead(phone, tenant=self.homebase)
        acme, _ = Appointment.objects.get_or_create_lead(phone, tenant=self.acme)
        hb.pause_chatbot()

        self.assertTrue(is_chatbot_paused_for_sender('27610318200', tenant=self.homebase))
        self.assertFalse(is_chatbot_paused_for_sender('27610318200', tenant=self.acme))

    def test_failure_note_is_not_written_to_another_tenants_lead(self):
        from .whatsapp_webhook import process_message_change
        phone = 'whatsapp:+27610318200'
        hb, _ = Appointment.objects.get_or_create_lead(phone, tenant=self.homebase)
        acme, _ = Appointment.objects.get_or_create_lead(phone, tenant=self.acme)
        # Acme's row is decisively the most recently touched — the old
        # phone-number-only lookup's `-updated_at` tiebreak would pick it.
        Appointment.objects.filter(pk=acme.pk).update(
            updated_at=timezone.now() + timedelta(hours=1))

        process_message_change(self._status_payload('111000111', '27610318200'))

        acme.refresh_from_db()
        self.assertNotIn('WA Delivery Failure', acme.internal_notes or '')
        hb.refresh_from_db()
        self.assertIn('WA Delivery Failure', hb.internal_notes or '')


class ReminderChannelWindowTests(TestCase):
    """Reminders are WhatsApp-first, email-as-fallback, never a paid template.

    The rule: if the free-form window is open, send on WhatsApp and stop. Only
    once it has closed do we look for an email address. We never spend on a
    utility/template message to reach someone outside the window.
    """

    def setUp(self):
        self.tenant, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})

    def _job(self, **kw):
        defaults = dict(
            phone_number='whatsapp:+263771000111',
            customer_name='Test Customer',
            appointment_type='job_appointment',
            job_status='scheduled',
            job_scheduled_datetime=timezone.now() + timedelta(hours=24),
            tenant=self.tenant,
        )
        defaults.update(kw)
        return Appointment.objects.create(**defaults)

    # ── the gate itself ──────────────────────────────────────────────────────

    def test_window_open_within_24h(self):
        from .whatsapp_window import is_window_open
        job = self._job(last_customer_response=timezone.now() - timedelta(hours=2))
        self.assertTrue(is_window_open(job))

    def test_window_closed_after_24h(self):
        from .whatsapp_window import is_window_open
        job = self._job(last_customer_response=timezone.now() - timedelta(hours=30))
        self.assertFalse(is_window_open(job))

    def test_ctwa_lead_still_open_at_30h(self):
        """A CTWA ad lead gets 72h. The old bare-24h check called this closed
        and needlessly dropped to email."""
        from .whatsapp_window import is_window_open
        job = self._job(
            last_customer_response=timezone.now() - timedelta(hours=30),
            ctwa_entry_at=timezone.now() - timedelta(hours=30),
        )
        self.assertTrue(is_window_open(job))

    def test_131047_flag_closes_window_even_inside_24h(self):
        """Meta is authoritative: a bounced send closes the window regardless
        of our own clock, so we must not keep trying WhatsApp."""
        from .whatsapp_window import is_window_open
        job = self._job(last_customer_response=timezone.now() - timedelta(hours=1))
        job.mark_freeform_window_closed()
        self.assertFalse(is_window_open(job))

    def test_safety_buffer_closes_window_just_before_expiry(self):
        from .whatsapp_window import is_window_open
        job = self._job(
            last_customer_response=timezone.now() - timedelta(hours=24) + timedelta(minutes=2))
        self.assertFalse(is_window_open(job))

    # ── channel selection ────────────────────────────────────────────────────

    def _send(self, job, rtype='1_day'):
        from bot.management.commands.send_job_reminders import Command
        cmd = Command()
        cmd.stdout = StringIO()
        with patch.object(Command, '_send_whatsapp', return_value=True) as wa,              patch('bot.management.commands.send_job_reminders.send_email_to_recipients',
                   return_value=True) as mail:
            result = cmd.send_job_reminder(job, rtype, dry_run=False)
        return result, wa, mail

    def test_open_window_sends_whatsapp_and_no_email(self):
        job = self._job(
            last_customer_response=timezone.now() - timedelta(hours=1),
            customer_email='customer@example.com',
        )
        ok, wa, mail = self._send(job)
        self.assertTrue(ok)
        self.assertTrue(wa.called)
        self.assertFalse(mail.called, 'must not also email while the window is open')

    def test_closed_window_falls_back_to_email_only(self):
        job = self._job(
            last_customer_response=timezone.now() - timedelta(hours=48),
            customer_email='customer@example.com',
        )
        ok, wa, mail = self._send(job)
        self.assertTrue(ok)
        self.assertFalse(wa.called, 'must not send WhatsApp outside the window')
        self.assertTrue(mail.called)

    def test_closed_window_without_email_sends_nothing(self):
        """No open window and no address = no reminder. We never pay for a
        template to reach them."""
        job = self._job(last_customer_response=timezone.now() - timedelta(hours=48))
        ok, wa, mail = self._send(job)
        self.assertFalse(ok)
        self.assertFalse(wa.called)
        self.assertFalse(mail.called)

    def test_ctwa_lead_at_30h_still_uses_whatsapp_not_email(self):
        job = self._job(
            last_customer_response=timezone.now() - timedelta(hours=30),
            ctwa_entry_at=timezone.now() - timedelta(hours=30),
            customer_email='customer@example.com',
        )
        ok, wa, mail = self._send(job)
        self.assertTrue(wa.called)
        self.assertFalse(mail.called)


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

    def test_title_optional_on_upload_mandatory_on_rename(self):
        import json

        from django.core.files.uploadedfile import SimpleUploadedFile
        from django.db import IntegrityError, transaction

        from .models import TenantIntake, TenantPortfolioItem
        # Portal add WITHOUT a name is allowed — the tenant can just upload
        # their pictures and vision names the row (PENDING_TITLE until it runs).
        from .media_library import PENDING_TITLE
        res = self.client.post(reverse('gallery_add'),
                               {'media': SimpleUploadedFile('job.jpg', b'x'),
                                'tag': 'geyser', 'caption': '  '}, follow=True)
        # (The literal string still appears in the page's own JS, so assert the
        # DB effect rather than the copy.)
        added = TenantPortfolioItem.objects.filter(tenant=self.acme)
        self.assertEqual(added.count(), 1)
        self.assertEqual(added.first().title, PENDING_TITLE)
        added.delete()
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
        # An unnamed entry is accepted and parked under PENDING_TITLE for
        # vision to name, rather than rejecting the batch.
        from .media_library import PENDING_TITLE
        up4 = self.client.post(reverse('gallery_upload'),
                               {'media': SimpleUploadedFile('d.jpg', b'x')}).json()
        res = self.client.post(reverse('gallery_finalize'), data=_json.dumps([
            {'path': up4['path'], 'caption': '  '}]),
            content_type='application/json')
        self.assertTrue(res.json()['ok'])
        self.assertTrue(TenantPortfolioItem.objects.filter(
            tenant=self.acme, title=PENDING_TITLE).exists())

    def test_finalize_is_idempotent_and_atomic(self):
        """Re-finalizing the same upload must not 500 on the unique constraint.

        Prod (2026-08-27, tenant barmak): gallery_finalize inserted rows one at
        a time with no transaction and no uniqueness check, so a re-submitted
        batch hit `uniq_portfolio_item_per_tenant` and returned a 500 — after
        having already written the entries before the collision, which made
        every retry collide on more rows than the last.
        """
        import json as _json

        from django.core.files.uploadedfile import SimpleUploadedFile

        from .models import TenantPortfolioItem
        up = self.client.post(reverse('gallery_upload'),
                              {'media': SimpleUploadedFile('borehole.jpg', b'x')}).json()
        payload = _json.dumps([
            {'path': up['path'], 'caption': 'Borehole', 'tag': 'general'}])

        first = self.client.post(reverse('gallery_finalize'), data=payload,
                                 content_type='application/json')
        self.assertTrue(first.json()['ok'])
        # The same batch again: 200, no duplicate row, no new item.
        second = self.client.post(reverse('gallery_finalize'), data=payload,
                                  content_type='application/json')
        self.assertTrue(second.json()['ok'])
        self.assertEqual(second.json()['created'], 0)
        self.assertEqual(
            TenantPortfolioItem.objects.filter(tenant=self.acme).count(), 1)

        # Two DIFFERENT uploads whose names share the first 8 characters used to
        # slugify to one id. They must get distinct ids, not a 500.
        a = self.client.post(reverse('gallery_upload'),
                             {'media': SimpleUploadedFile('bathroom-install-aaa.jpg', b'x')}).json()
        b = self.client.post(reverse('gallery_upload'),
                             {'media': SimpleUploadedFile('bathroom-install-bbb.jpg', b'x')}).json()
        res = self.client.post(reverse('gallery_finalize'), data=_json.dumps([
            {'path': a['path'], 'caption': 'Bathroom one', 'tag': 'bathroom install'},
            {'path': b['path'], 'caption': 'Bathroom two', 'tag': 'bathroom install'},
        ]), content_type='application/json')
        self.assertTrue(res.json()['ok'])
        ids = set(TenantPortfolioItem.objects.filter(tenant=self.acme)
                  .values_list('item_id', flat=True))
        self.assertEqual(len(ids), 3)

        # A batch rejected before writing must write NOTHING. A blank caption
        # is no longer a rejection (vision names it), so malformed JSON is the
        # rejection that still has to leave the table untouched.
        before = TenantPortfolioItem.objects.filter(tenant=self.acme).count()
        bad = self.client.post(reverse('gallery_finalize'), data='not json',
                               content_type='application/json')
        self.assertEqual(bad.status_code, 400)
        self.assertEqual(
            TenantPortfolioItem.objects.filter(tenant=self.acme).count(), before)

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
        # The page preview is the bot's FULL vague-'how much' reply — anchor,
        # price disclaimer, and the budget tie-down close. Assert the stable
        # half of the disclaimer, not its adjective: the wording was simplified
        # for customers ("approximate starting prices" -> "starting prices") and
        # pinning the phrasing broke this test for no behavioural reason.
        page = self.client.get(reverse('offer'))
        self.assertContains(
            page, 'Our Bathroom makeover special is US$800 — a freestanding tub and side chamber.')
        self.assertContains(page, 'sees the space')
        self.assertContains(page, 'That sit alright with your budget?')
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

    def test_faq_name_triggers_never_cross_tenants(self):
        """The shared trigger lists carried Homebase's own names ("talk to
        takudzwa", "where is homebase"), so another tenant's customer typing
        their OWN plumber's name matched nothing. Names are generated per lead."""
        from .faq import match_faq_topic, _TRIGGERS
        TenantProfile.objects.create(
            tenant=self.acme, plumber_name='Kudakwashe Marange',
            plumber_contact='+263773871503', licensed_claim_enabled=False,
        )
        # Each tenant's own plumber name is a trigger — and only theirs.
        self.assertEqual(match_faq_topic('talk to Kudakwashe', tenant=self.acme), 'contact')
        self.assertIsNone(match_faq_topic('talk to Kudakwashe', tenant=self.homebase))
        self.assertEqual(match_faq_topic('talk to Takudzwa', tenant=self.homebase), 'contact')
        self.assertIsNone(match_faq_topic('talk to Takudzwa', tenant=self.acme))
        # Business name likewise.
        self.assertEqual(match_faq_topic('where is Acme Plumbing', tenant=self.acme), 'location')
        self.assertIsNone(match_faq_topic('where is Acme Plumbing', tenant=self.homebase))
        # No proper noun is left in the SHARED lists.
        for topic, triggers in _TRIGGERS.items():
            for trigger in triggers:
                with self.subTest(topic=topic, trigger=trigger):
                    self.assertNotIn('takudzwa', trigger)
                    self.assertNotIn('homebase', trigger)

    def test_asking_to_reach_the_plumber_matches_contact(self):
        """A lead asking in the obvious words matched nothing — the list only
        had the plumber's name and "speak to someone"."""
        from .faq import match_faq_topic
        for message in ('I want to get in touch with your plumber today',
                        'can I speak to the plumber',
                        "what is the plumber's number",
                        'can I call your plumber'):
            with self.subTest(message=message):
                self.assertEqual(match_faq_topic(message, tenant=self.acme), 'contact')
        # Still not a contact question.
        self.assertIsNone(match_faq_topic('how much is a tub', tenant=self.acme))
        self.assertIsNone(match_faq_topic('can you come Wednesday', tenant=self.acme))

    def test_contact_fact_composed_from_the_tenants_own_plumber(self):
        """A tenant can hold a plumber and no hand-written 'contact' fact
        (barmak did), and the topic was skipped — so a lead asking to reach
        them got nothing while we held the number. Absent NUMBER still omits."""
        from .faq import faq_fact
        profile = TenantProfile.objects.create(
            tenant=self.acme, plumber_name='Kudakwashe Marange',
            plumber_contact='+263773871503', licensed_claim_enabled=False,
        )
        fact = faq_fact('contact', tenant=self.acme)
        self.assertIn('+263773871503', fact)
        self.assertIn('Kudakwashe Marange', fact)
        # Never another tenant's plumber.
        self.assertNotIn('Takudzwa', fact)
        self.assertNotIn('+263774819901', fact)
        # A written fact still wins over the composed one.
        profile.faq_facts = {'contact': 'Ring the office on 0800 000.'}
        profile.save()
        self.assertEqual(faq_fact('contact', tenant=self.acme), 'Ring the office on 0800 000.')
        # No number on file → no fact at all, never a borrowed one.
        bare = Tenant.objects.create(name='Bare Plumbing', slug='bare-plumbing')
        self.assertIsNone(faq_fact('contact', tenant=bare))

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

    def test_customer_facing_copy_never_signs_another_tenant_as_homebase(self):
        """Every string a customer reads names THEIR plumber. Hardcoded
        "Homebase Plumbers" reached other tenants' leads through the booking
        confirmation sign-off, the reminder email footer and the WhatsApp
        confirmation signature."""
        from .customer_emails import build_booking_confirmation_email
        from .management.commands.send_reminders import _html_email
        from .utils import business_name_for

        acme_lead = make_lead(9703, tenant=self.acme,
                              customer_name='Acme Customer')
        _, html = build_booking_confirmation_email(acme_lead)
        self.assertIn('Acme Plumbing', html)
        self.assertNotIn('HomeBase Plumbers', html)
        self.assertNotIn('Homebase Plumbers', html)

        reminder = _html_email('#1a73e8', 'Reminder', '<p>x</p>', acme_lead)
        self.assertIn('Acme Plumbing · Automated Reminder', reminder)
        self.assertNotIn('HomeBase Plumbers', reminder)

        # The shared resolver reads the lead's own tenant, and degrades to a
        # neutral phrase rather than any business name.
        self.assertEqual(business_name_for(acme_lead), 'Acme Plumbing')
        self.assertEqual(business_name_for(self.homebase), 'Homebase Plumbers')
        self.assertEqual(business_name_for(None), 'the plumbing team')

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
        # Homebase's gallery buckets must be the app's own categories, not the
        # bot's match terms — the seed used to put 'navy' / 'clawfoot' in
        # `keywords`, giving the portal a bucket per photo.
        from .models import TenantPortfolioItem
        from .views.gallery import GALLERY_CATEGORIES
        valid = {key for key, _ in GALLERY_CATEGORIES}
        rows = TenantPortfolioItem.objects.filter(tenant=self.homebase)
        for row in rows:
            self.assertTrue(set(row.keywords) <= valid,
                            f'{row.item_id}: {row.keywords} not gallery categories')
        # ...while the matching synonyms survive the split.
        self.assertIn('clawfoot', dict(
            (r.item_id, r.match_terms) for r in rows)['clawfoot-tub-feature-wall'])
        self.assertIsNotNone(portfolio_catalog.match_portfolio_item(
            'show me the black tub photo', tenant=self.homebase))
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


class PortfolioPriceSyncTests(TestCase):
    """Portfolio images stay in lockstep with the price list: a photo linked to
    a job (family/variant) always shows that job's current price, and a price
    entered/changed in config re-syncs every linked photo."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.tenant = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        TenantProfile.objects.create(tenant=self.tenant, currency='US$')
        self.root = get_user_model().objects.create_superuser(
            username='root', password='pass12345', email='root@example.com')
        self.client.login(username='root', password='pass12345')

    def test_price_line_and_category_pulled_from_the_list(self):
        from .media_library import price_line_and_tags_for_refs
        from .models import TenantPriceItem
        TenantPriceItem.objects.create(
            tenant=self.tenant, family='geyser', variant='', label='geyser', allin=160)
        line, tags = price_line_and_tags_for_refs(
            self.tenant, [{'family': 'geyser', 'variant': ''}])
        self.assertEqual(line, 'Geyser supply & install from US$160')
        self.assertEqual(tags, ['geyser'])   # categorised by the job

    def test_every_library_job_lands_in_a_real_gallery_category(self):
        # _fam_tag must only ever emit keys the gallery can render, and every
        # pickable job must be reachable: a job missing from PORTFOLIO_LIBRARY
        # can be neither categorised nor price-linked (how kitchens ended up
        # invisible despite renovation/kitchen being priced).
        from .media_library import PORTFOLIO_LIBRARY, _fam_tag
        from .views.gallery import GALLERY_CATEGORIES
        valid = {key for key, _ in GALLERY_CATEGORIES}
        for _cat, items in PORTFOLIO_LIBRARY:
            for label, family, variant in items:
                self.assertIn(_fam_tag(family, variant), valid, label)
        self.assertEqual(_fam_tag('renovation', 'kitchen'), 'kitchen')
        self.assertEqual(_fam_tag('renovation', 'bathroom'), 'bathroom install')
        # Every priced job the tenant sells should be pickable in the annotator.
        from .tenant_config import HOMEBASE_PRICE_ITEMS
        pickable = {(f, v) for _c, items in PORTFOLIO_LIBRARY
                    for _l, f, v in items}
        for row in HOMEBASE_PRICE_ITEMS:
            if row['family'] in ('renovation', 'package'):
                self.assertIn((row['family'], row.get('variant', '')), pickable,
                              row['label'])

    def test_kitchen_photos_bucket_under_kitchens(self):
        from .models import TenantPortfolioItem
        rows = {r.item_id: r.keywords for r
                in TenantPortfolioItem.objects.filter(tenant=self.homebase)}
        self.assertEqual(rows['modern-kitchen-island'], ['kitchen'])
        self.assertEqual(rows['navy-shaker-kitchen'], ['kitchen'])

    def test_unpriced_link_is_blank_but_still_categorised(self):
        from .media_library import price_line_and_tags_for_refs
        line, tags = price_line_and_tags_for_refs(
            self.tenant, [{'family': 'shower', 'variant': ''}])
        self.assertEqual(line, '')                      # no price yet
        self.assertEqual(tags, ['bathroom install'])    # category from the item

    def test_price_added_after_the_photo_resyncs_it(self):
        from .media_library import resync_portfolio_prices
        from .models import TenantPortfolioItem, TenantPriceItem
        item = TenantPortfolioItem.objects.create(
            tenant=self.tenant, item_id='p1',
            filename='tenant_portfolios/acme/a.jpg', title='Shower job',
            price_refs=[{'family': 'shower', 'variant': ''}])
        self.assertEqual(item.price_line, '')           # saved before any price
        TenantPriceItem.objects.create(
            tenant=self.tenant, family='shower', variant='',
            label='shower cubicle', allin=170)
        self.assertEqual(resync_portfolio_prices(self.tenant), 1)
        item.refresh_from_db()
        self.assertIn('US$170', item.price_line)
        self.assertEqual(item.keywords, ['bathroom install'])

    def test_legacy_photo_without_refs_is_linked_and_priced(self):
        # An image saved BEFORE the price link existed (price_refs empty) still
        # gets priced: its text ("Geyser supply & install") is matched back to
        # the price list, the link recovered, and the price pulled in.
        from .media_library import resync_portfolio_prices
        from .models import TenantPortfolioItem, TenantPriceItem
        item = TenantPortfolioItem.objects.create(
            tenant=self.tenant, item_id='legacy',
            filename='tenant_portfolios/acme/old.jpg',
            title='Geyser supply & install', keywords=['geyser'])
        self.assertEqual(item.price_refs, [])
        TenantPriceItem.objects.create(
            tenant=self.tenant, family='geyser', variant='', label='geyser', allin=160)
        self.assertEqual(resync_portfolio_prices(self.tenant), 1)
        item.refresh_from_db()
        self.assertEqual(item.price_refs, [{'family': 'geyser', 'variant': ''}])
        self.assertIn('US$160', item.price_line)

    def test_saving_config_prices_resyncs_linked_photos(self):
        from .models import TenantPortfolioItem, TenantPriceItem
        item = TenantPortfolioItem.objects.create(
            tenant=self.tenant, item_id='p1',
            filename='tenant_portfolios/acme/a.jpg', title='Geyser job',
            price_refs=[{'family': 'geyser', 'variant': ''}])
        data = {
            'plumber_name': '', 'plumber_contact': '', 'business_whatsapp': '',
            'location_line': '', 'location_area': '', 'location_city': '',
            'business_hours': '{}', 'timezone_name': '', 'excluded_areas': '[]',
            'currency': 'US$', 'packages': '[]', 'faq_facts': '{}', 'scripts': '{}',
            'email_from_name': '', 'email_sender': '',
            'form-TOTAL_FORMS': '1', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
            'form-0-family': 'geyser', 'form-0-variant': '', 'form-0-label': 'geyser',
            'form-0-short_label': '', 'form-0-supply': '', 'form-0-labour': '',
            'form-0-allin': '160', 'form-0-parts': '[]',
            'form-0-sort_order': '0', 'form-0-is_active': 'on',
        }
        response = self.client.post(
            reverse('platform_tenant_config_edit', args=['acme']), data)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            TenantPriceItem.objects.filter(tenant=self.tenant, family='geyser').count(), 1)
        item.refresh_from_db()
        self.assertIn('US$160', item.price_line)        # photo now shows the new price


class LeadMagnetTests(TestCase):
    """Per-tenant lead-magnet PDF: one design per tenant (rotated), built from
    the tenant's own config + prices, cached in object storage."""

    def setUp(self):
        import shutil
        from django.conf import settings as dj_settings
        shutil.rmtree(os.path.join(dj_settings.MEDIA_ROOT, 'lead_magnets_pdfs'),
                      ignore_errors=True)
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        TenantProfile.objects.create(
            tenant=self.acme, plumber_name='Blessing',
            business_whatsapp='+263700000000', currency='US$')
        from .models import TenantPriceItem
        TenantPriceItem.objects.create(
            tenant=self.acme, family='geyser', variant='', label='geyser', allin=160)
        TenantPriceItem.objects.create(
            tenant=self.acme, family='basin', variant='', label='basin', flat=70)

    def test_design_is_deterministic_and_in_range(self):
        from .lead_magnet import LEAD_MAGNET_DESIGNS, design_index_for
        idx = design_index_for(self.acme)
        self.assertEqual(idx, design_index_for(self.acme))         # stable per tenant
        self.assertTrue(0 <= idx < len(LEAD_MAGNET_DESIGNS))

    def test_storage_path_is_per_tenant(self):
        from .lead_magnet import storage_path
        self.assertEqual(storage_path(self.acme), 'lead_magnets_pdfs/acme/portfolio.pdf')

    def test_build_produces_a_pdf(self):
        from .lead_magnet import build_lead_magnet_pdf
        data = build_lead_magnet_pdf(self.acme)
        self.assertIsNotNone(data)
        self.assertEqual(data[:4], b'%PDF')
        self.assertGreater(len(data), 1000)

    def test_get_or_build_caches_then_invalidates(self):
        from django.core.files.storage import default_storage
        from .lead_magnet import (get_or_build_lead_magnet, invalidate_lead_magnet,
                                  storage_path)
        path = get_or_build_lead_magnet(self.acme)
        self.assertEqual(path, storage_path(self.acme))
        self.assertTrue(default_storage.exists(path))
        self.assertEqual(get_or_build_lead_magnet(self.acme), path)  # reuses cache
        invalidate_lead_magnet(self.acme)
        self.assertFalse(default_storage.exists(path))

    def test_bytes_are_pdf(self):
        from .lead_magnet import lead_magnet_bytes
        data = lead_magnet_bytes(self.acme)
        self.assertIsNotNone(data)
        self.assertEqual(data[:4], b'%PDF')
        from django.core.files.storage import default_storage
        from .lead_magnet import storage_path
        default_storage.delete(storage_path(self.acme))

    def test_portal_shows_and_streams_the_pdf(self):
        from django.core.files.storage import default_storage
        from .lead_magnet import storage_path
        root = get_user_model().objects.create_superuser(
            username='root', password='pass12345', email='root@example.com')
        self.client.login(username='root', password='pass12345')
        # Config overview shows the Lead magnet card with a View PDF link.
        body = self.client.get(
            reverse('platform_tenant_config', args=['acme'])).content.decode()
        self.assertIn('Lead magnet', body)
        self.assertIn(reverse('platform_tenant_lead_magnet', args=['acme']), body)
        # The link streams a PDF (building it on demand).
        resp = self.client.get(reverse('platform_tenant_lead_magnet', args=['acme']))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'application/pdf')
        self.assertEqual(b''.join(resp.streaming_content)[:4], b'%PDF')
        default_storage.delete(storage_path(self.acme))


class TenantChannelEditorTests(TestCase):
    """Superuser can create/update a tenant's WhatsApp channel from the config
    editor — the access token is stored encrypted and never re-rendered."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.root = get_user_model().objects.create_superuser(
            username='root', password='pass12345', email='root@example.com')
        self.client.login(username='root', password='pass12345')

    def _save(self, **extra):
        # The channel rides along on the single config-edit 'Save changes' form.
        data = {
            'plumber_name': '', 'plumber_contact': '', 'business_whatsapp': '',
            'location_line': '', 'location_area': '', 'location_city': '',
            'timezone_name': '', 'currency': 'US$', 'email_from_name': '', 'email_sender': '',
            'form-TOTAL_FORMS': '0', 'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0', 'form-MAX_NUM_FORMS': '1000',
            'phone_number_id': '111222333', 'display_number': '+263771', 'channel_active': 'on',
        }
        data.update(extra)
        return self.client.post(
            reverse('platform_tenant_config_edit', args=['acme']), data)

    def test_create_channel_encrypts_the_token(self):
        from .models import TenantWhatsAppChannel
        resp = self._save(access_token='EAAB-secret-token')
        self.assertEqual(resp.status_code, 302)
        ch = TenantWhatsAppChannel.objects.get(tenant=self.acme)
        self.assertEqual(ch.phone_number_id, '111222333')
        self.assertTrue(ch.is_active)
        self.assertTrue(ch.access_token.startswith('fernet:'))       # encrypted at rest
        self.assertEqual(ch.decrypted_access_token(), 'EAAB-secret-token')

    def test_blank_token_keeps_the_one_on_file(self):
        from .models import TenantWhatsAppChannel
        TenantWhatsAppChannel.objects.create(
            tenant=self.acme, phone_number_id='111222333', access_token='KEEP-ME')
        self._save()  # no access_token field
        ch = TenantWhatsAppChannel.objects.get(tenant=self.acme)
        self.assertEqual(ch.decrypted_access_token(), 'KEEP-ME')     # preserved

    def test_new_token_replaces_the_old(self):
        from .models import TenantWhatsAppChannel
        TenantWhatsAppChannel.objects.create(
            tenant=self.acme, phone_number_id='111222333', access_token='OLD')
        self._save(access_token='NEW')
        ch = TenantWhatsAppChannel.objects.get(tenant=self.acme)
        self.assertEqual(ch.decrypted_access_token(), 'NEW')

    def test_no_phone_number_id_creates_no_channel(self):
        from .models import TenantWhatsAppChannel
        self._save(phone_number_id='')  # config saves; channel is optional
        self.assertFalse(TenantWhatsAppChannel.objects.filter(tenant=self.acme).exists())

    def test_duplicate_phone_number_id_is_rejected(self):
        from .models import TenantWhatsAppChannel
        other = Tenant.objects.create(name='Other', slug='other')
        TenantWhatsAppChannel.objects.create(tenant=other, phone_number_id='999888')
        resp = self._save(phone_number_id='999888')
        self.assertEqual(resp.status_code, 302)   # rest of config still saved
        self.assertFalse(TenantWhatsAppChannel.objects.filter(tenant=self.acme).exists())

    def test_editor_renders_above_price_sheet_and_never_leaks_the_token(self):
        from .models import TenantWhatsAppChannel
        TenantWhatsAppChannel.objects.create(
            tenant=self.acme, phone_number_id='111222333', access_token='TOPSECRET')
        body = self.client.get(
            reverse('platform_tenant_config_edit', args=['acme'])).content.decode()
        self.assertIn('name="phone_number_id"', body)
        # The channel card sits above the price sheet card.
        self.assertLess(body.index('>WhatsApp channel</div>'), body.index('Price sheet ·'))
        self.assertNotIn('TOPSECRET', body)                          # token not rendered
        self.assertNotIn('fernet:', body)
        self.assertIn('leave blank to keep', body.lower())


class BotTimerSwitchTests(TestCase):
    """Each tenant's switches reach the webhook pipeline: OFF makes that stage
    instant for THAT tenant, ON keeps today's behaviour, and one tenant's
    choice never changes another's."""

    def setUp(self):
        self.homebase = Tenant.objects.get(slug='homebase')
        self.acme = Tenant.objects.create(name='Acme', slug='acme')

    def test_reply_delay_switch_controls_the_send_wait_per_tenant(self):
        from .whatsapp_webhook import get_random_delay
        self.assertIn(get_random_delay(self.acme), range(60, 301))  # default: 1-5 min
        TenantSetting.set_flag('reply_delay_enabled', self.acme, False)
        self.assertEqual(get_random_delay(self.acme), 0)
        self.assertIn(get_random_delay(self.homebase), range(60, 301))

    def test_delayed_response_honours_the_switch_whatever_delay_it_was_given(self):
        # The delay is computed before the tenant is resolved at most call
        # sites, so delayed_response re-checks — that's what makes the switch
        # reliable. A tenant with the switch OFF must not sit out a 5-min wait.
        from . import whatsapp_webhook as ww
        TenantSetting.set_flag('reply_delay_enabled', self.acme, False)
        Appointment.objects.create(
            phone_number='whatsapp:+263771000333', tenant=self.acme)
        sleeps = []
        with patch.object(ww.time, 'sleep', side_effect=sleeps.append), \
             patch('bot.whatsapp_cloud_api.get_client_for_tenant') as client:
            client.return_value.send_text_message.return_value = {}
            ww.delayed_response('263771000333', 'hello', 300, tenant=self.acme)
        self.assertEqual(sleeps, [])                       # never waited
        self.assertTrue(client.return_value.send_text_message.called)

    def test_batch_switch_off_flushes_immediately_without_a_timer(self):
        import threading

        from . import whatsapp_webhook as ww
        TenantSetting.set_flag('batch_window_enabled', self.acme, False)
        flushed = threading.Event()
        with patch.object(ww, '_flush_text_batch', side_effect=lambda s: flushed.set()):
            ww._enqueue_for_response('263771000111', 'how much for a geyser?',
                                     'wamid.1', tenant=self.acme)
        self.assertTrue(flushed.wait(timeout=5))
        self.assertNotIn('263771000111', ww._pending_batch_timers)

    def test_batch_switch_off_for_one_tenant_leaves_the_other_batching(self):
        from . import whatsapp_webhook as ww
        TenantSetting.set_flag('batch_window_enabled', self.acme, False)
        with patch.object(ww, '_flush_text_batch') as flush:
            ww._enqueue_for_response('263771000222', 'hi', 'wamid.2',
                                     tenant=self.homebase)
        try:
            flush.assert_not_called()
            self.assertIn('263771000222', ww._pending_batch_timers)
        finally:
            ww._pending_batch_timers.pop('263771000222').cancel()
            ww._pending_batches.pop('263771000222', None)


class SettingsTabAccessTests(StaffClientTestCase):
    """The Settings pages are platform configuration (team numbers, calendar
    credentials, AI keys), not tenant controls — tenant staff neither see the
    tab nor reach the pages."""

    def test_staff_cannot_reach_settings_pages(self):
        for name in ('settings', 'calendar_settings', 'ai_settings'):
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertIn(response.status_code, (302, 403), name)

    def test_settings_tab_hidden_from_staff_and_shown_to_admin(self):
        body = self.client.get(reverse('dashboard')).content.decode()
        self.assertNotIn(reverse('settings'), body)

        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        body = self.client.get(reverse('dashboard')).content.decode()
        self.assertIn(reverse('settings'), body)

    def test_admin_can_still_open_settings(self):
        self.user.is_superuser = True
        self.user.save(update_fields=['is_superuser'])
        for name in ('settings', 'calendar_settings', 'ai_settings'):
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class TenantNotificationEmailTests(TestCase):
    """Each tenant chooses the inbox its own alerts go to (Profile page →
    TenantProfile.email_sender); the platform address is on every list."""

    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.user = get_user_model().objects.create_user(
            username='acme-owner-email', password='pass12345', is_staff=True)
        TenantMembership.objects.create(
            user=self.user, tenant=self.acme, role='staff')
        self.client.force_login(self.user)

    def test_chosen_address_replaces_the_hardcoded_one(self):
        from .plumber_notifications import (
            PLATFORM_NOTIFICATION_EMAIL, get_plumber_notification_emails,
        )
        # No choice yet: a foreign tenant never inherits Homebase's inbox.
        self.assertEqual(
            get_plumber_notification_emails(self.acme),
            [PLATFORM_NOTIFICATION_EMAIL],
        )
        TenantProfile.objects.update_or_create(
            tenant=self.acme, defaults={'email_sender': 'owner@acme.example'})
        self.acme.refresh_from_db()
        self.assertEqual(
            get_plumber_notification_emails(self.acme),
            [PLATFORM_NOTIFICATION_EMAIL, 'owner@acme.example'],
        )
        # The platform address is never duplicated when a tenant picks it.
        TenantProfile.objects.update_or_create(
            tenant=self.acme,
            defaults={'email_sender': PLATFORM_NOTIFICATION_EMAIL})
        self.acme.refresh_from_db()
        self.assertEqual(
            get_plumber_notification_emails(self.acme),
            [PLATFORM_NOTIFICATION_EMAIL],
        )

    def test_platform_and_homebase_keep_the_configured_list(self):
        from .plumber_notifications import get_plumber_notification_emails
        for tenant in (None, self.homebase):
            with self.subTest(tenant=tenant):
                recipients = get_plumber_notification_emails(tenant)
                self.assertIn('jones86xi@gmail.com', recipients)
                self.assertIn('homebsconstruction@gmail.com', recipients)

    def test_profile_page_saves_and_clears_the_address(self):
        response = self.client.post(
            reverse('profile'), {'notification_email': 'alerts@acme.example'})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            TenantProfile.objects.get(tenant=self.acme).email_sender,
            'alerts@acme.example')

        # Blank clears the choice; a malformed address is rejected, not stored.
        self.client.post(reverse('profile'), {'notification_email': 'not-an-email'})
        self.assertEqual(
            TenantProfile.objects.get(tenant=self.acme).email_sender,
            'alerts@acme.example')
        self.client.post(reverse('profile'), {'notification_email': ''})
        self.assertEqual(
            TenantProfile.objects.get(tenant=self.acme).email_sender, '')

    def test_saving_other_profile_fields_leaves_the_address_alone(self):
        TenantProfile.objects.update_or_create(
            tenant=self.acme, defaults={'email_sender': 'alerts@acme.example'})
        self.client.post(reverse('profile'), {'first_name': 'Blessing'})
        self.assertEqual(
            TenantProfile.objects.get(tenant=self.acme).email_sender,
            'alerts@acme.example')

    def test_alerts_go_to_the_tenants_own_inbox(self):
        from .plumber_notifications import send_plumber_notification_email
        TenantProfile.objects.update_or_create(
            tenant=self.acme, defaults={'email_sender': 'owner@acme.example'})
        TenantSetting.set_flag('email_sending_enabled', self.acme, True)
        with patch('bot.plumber_notifications._send_via_brevo',
                   return_value=True) as brevo:
            with self.settings(BREVO_API_KEY='x'):
                send_plumber_notification_email(
                    'New lead', 'body', tenant=self.acme)
        recipients = brevo.call_args[0][1]
        self.assertIn('owner@acme.example', recipients)
        self.assertNotIn('homebsconstruction@gmail.com', recipients)


class PlatformAddressIsHiddenTests(TestCase):
    """The operator is copied on every tenant alert but must never be visible
    to the tenant -- not on the mail, not on the dashboard."""

    def setUp(self):
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        TenantProfile.objects.update_or_create(
            tenant=self.acme, defaults={'email_sender': 'owner@acme.example'})
        TenantSetting.set_flag('email_sending_enabled', self.acme, True)
        self.user = get_user_model().objects.create_user(
            username='acme-owner-hidden', password='pass12345', is_staff=True)
        TenantMembership.objects.create(
            user=self.user, tenant=self.acme, role='staff')
        self.client.force_login(self.user)

    def _alert(self):
        from .plumber_notifications import send_plumber_notification_email
        with patch('bot.plumber_notifications._send_via_brevo',
                   return_value=True) as brevo:
            with self.settings(BREVO_API_KEY='x'):
                send_plumber_notification_email('New lead', 'body', tenant=self.acme)
        return brevo.call_args

    def test_platform_address_is_bcc_not_to(self):
        from .plumber_notifications import PLATFORM_NOTIFICATION_EMAIL
        call = self._alert()
        self.assertEqual(call[0][1], ['owner@acme.example'])
        self.assertEqual(call.kwargs['bcc'], [PLATFORM_NOTIFICATION_EMAIL])

    def test_platform_address_is_absent_from_the_profile_page(self):
        from .plumber_notifications import PLATFORM_NOTIFICATION_EMAIL
        body = self.client.get(reverse('profile')).content.decode()
        self.assertIn('owner@acme.example', body)
        self.assertNotIn(PLATFORM_NOTIFICATION_EMAIL, body)

    def test_operator_still_receives_the_alert(self):
        from .plumber_notifications import PLATFORM_NOTIFICATION_EMAIL
        call = self._alert()
        everyone = list(call[0][1]) + list(call.kwargs['bcc'])
        self.assertIn(PLATFORM_NOTIFICATION_EMAIL, everyone)

    def test_a_tenant_with_no_address_still_gets_a_deliverable_message(self):
        """Nobody to put in To, so the platform address stays visible rather
        than sending a message with no recipient at all."""
        from .plumber_notifications import (
            PLATFORM_NOTIFICATION_EMAIL, split_notification_recipients)
        bare = Tenant.objects.create(name='Bare', slug='bare')
        visible, hidden = split_notification_recipients(bare)
        self.assertEqual(visible, [PLATFORM_NOTIFICATION_EMAIL])
        self.assertEqual(hidden, [])


class SenderIdentityTests(TestCase):
    """Two sending identities per tenant: internal alerts (operator + the
    tenant's own inbox) leave from the platform subdomain
    <slug>@notifications.homexmedia.com, while mail to the tenant's CLIENTS
    leaves from the tenant's own domain address."""

    def setUp(self):
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        TenantSetting.set_flag('email_sending_enabled', self.acme, True)

    def _sent(self, **kwargs):
        from .plumber_notifications import send_email_to_recipients
        with patch('bot.plumber_notifications._send_via_brevo',
                   return_value=True) as brevo:
            with self.settings(BREVO_API_KEY='x'):
                send_email_to_recipients(
                    ['client@example.com'], 'Subject', 'body',
                    tenant=self.acme, **kwargs)
        return brevo.call_args.kwargs

    def test_internal_alerts_send_from_the_platform_subdomain(self):
        from .plumber_notifications import send_plumber_notification_email
        with patch('bot.plumber_notifications._send_via_brevo',
                   return_value=True) as brevo:
            with self.settings(BREVO_API_KEY='x'):
                send_plumber_notification_email(
                    'New lead', 'body', tenant=self.acme)
        self.assertIn(
            'acme@notifications.homexmedia.com',
            brevo.call_args.kwargs['from_email'])

    def test_customer_mail_sends_from_the_tenants_own_domain(self):
        TenantProfile.objects.update_or_create(
            tenant=self.acme,
            defaults={'customer_from_email': 'info@acmeplumbing.co.zw',
                      'email_from_name': 'Acme Plumbing'})
        self.acme.refresh_from_db()
        kwargs = self._sent()
        self.assertEqual(
            kwargs['from_email'], 'Acme Plumbing <info@acmeplumbing.co.zw>')
        # Reply-To follows the From identity — a customer's reply must reach
        # the tenant, never the platform's global inbox.
        self.assertEqual(kwargs['reply_to'], 'info@acmeplumbing.co.zw')

    def test_customer_mail_falls_back_to_the_platform_subdomain(self):
        kwargs = self._sent()
        self.assertIn('acme@notifications.homexmedia.com', kwargs['from_email'])

    def test_customer_mail_never_uses_another_tenants_domain(self):
        TenantProfile.objects.update_or_create(
            tenant=self.acme,
            defaults={'customer_from_email': 'info@acmeplumbing.co.zw'})
        self.acme.refresh_from_db()
        self.assertNotIn(
            'homebaseplumbers', self._sent()['from_email'])


class InboundEmailIntakeTests(TestCase):
    """Email is REPLY-ONLY: the bot answers people already in the system with a
    WhatsApp record, and nobody else. No email ever creates a lead."""

    def _raw(self, sender='jane@example.com', subject='Quote for a geyser',
             body='Hi, how much for a 150L geyser in Avondale?', extra_headers=(),
             to='team@example.com'):
        headers = [
            f'From: {sender}',
            f'Subject: {subject}',
            f'To: {to}',
        ]
        headers.extend(extra_headers)
        return ('\r\n'.join(headers) + '\r\n\r\n' + body).encode()

    def _lead(self, suffix=7200, **kwargs):
        """A WhatsApp lead who has emailed us before — the only kind of sender
        the bot answers."""
        kwargs.setdefault('customer_email', 'jane@example.com')
        kwargs.setdefault('customer_name', 'Jane Moyo')
        return make_lead(suffix, **kwargs)

    def _run(self, raws, polled='team@example.com', **opts):
        """Run the command against a fake IMAP inbox holding `raws`.

        Headers are handed over first (that is all the gates see); the body is
        fetched only for the mail the command decides to answer.
        """
        import email as _email

        from bot.management.commands import process_inbound_emails as mod

        seen = []
        fake_imap = object()
        by_uid = {str(i).encode(): raw for i, raw in enumerate(raws)}
        out = StringIO()
        with patch.object(mod, '_EMAIL_FROM', polled), \
             patch.object(mod, '_IMAP_PASS', 'secret'), \
             patch.object(mod, '_connect', return_value=fake_imap), \
             patch.object(mod, '_fetch_unseen_headers',
                          return_value=[(uid, _email.message_from_bytes(raw))
                                        for uid, raw in by_uid.items()]), \
             patch.object(mod, '_fetch_message',
                          side_effect=lambda imap, uid:
                              _email.message_from_bytes(by_uid[uid])), \
             patch.object(mod, '_mark_seen',
                          side_effect=lambda imap, uid: seen.append(uid)), \
             patch.object(mod, '_classify_intent',
                          return_value={'intent': 'other', 'date': None}), \
             patch.object(mod, '_generate_plumbot_email_reply',
                          return_value='Happy to help — a 150L geyser starts at '
                                       'US$450 supplied and installed.'), \
             patch.object(mod, '_send_reply', return_value=True) as send:
            call_command('process_inbound_emails', stdout=out, **opts)
        return out.getvalue(), send, seen

    # ── The one rule ────────────────────────────────────────────────────────

    def test_a_known_whatsapp_lead_emailing_in_is_answered(self):
        lead = self._lead()
        output, send, seen = self._run([self._raw()])

        lead.refresh_from_db()
        roles = [m['role'] for m in lead.conversation_history]
        self.assertEqual(roles, ['user', 'assistant'])
        # Off-thread the subject often carries the request itself.
        self.assertIn('Quote for a geyser', lead.conversation_history[0]['content'])
        send.assert_called_once()
        # Freshness fields are stamped, so it does not sort as an ancient lead.
        self.assertIsNotNone(lead.last_customer_response)
        self.assertIsNotNone(lead.last_inbound_at)
        self.assertEqual(len(seen), 1)
        self.assertIn('Matched by sender', output)

    def test_a_stranger_is_never_answered_and_never_becomes_a_lead(self):
        """The bug that started this: receipts, support tickets and cold
        strangers all belong to nobody in the CRM."""
        cases = [
            {'sender': 'stranger@example.com'},
            {'sender': 'invoice+statements@stripe.com',
             'subject': 'Your receipt from Anthropic, PBC #2946-1044-8181'},
            {'sender': 'support@somevendor.example',
             'subject': 'Support Ticket #9616645 Closed'},
        ]
        for case in cases:
            with self.subTest(**case):
                output, send, seen = self._run([self._raw(**case)])
                send.assert_not_called()
                self.assertEqual(seen, [])      # left unread for a human
                self.assertFalse(Appointment.objects.exists())

    def test_a_lead_with_no_whatsapp_record_is_left_for_a_human(self):
        """Synthetic keys — old `email_…` rows, `quotation_only_…` stubs — are
        CRM rows, not people the bot has talked to."""
        for phone in ('email_e62068d37db3', 'quotation_only_44'):
            with self.subTest(phone=phone):
                Appointment.objects.all().delete()
                lead = Appointment.objects.create(
                    phone_number=phone, customer_email='jane@example.com')
                _, send, seen = self._run([self._raw()])
                lead.refresh_from_db()
                send.assert_not_called()
                self.assertEqual(seen, [])
                self.assertEqual(lead.conversation_history or [], [])

    def test_a_thread_reply_from_a_synthetic_lead_is_not_answered_either(self):
        """Even with our own [APT-id] on it: the rule is about the lead, not
        about how the mail was matched."""
        lead = Appointment.objects.create(
            phone_number='email_e62068d37db3', customer_email='jane@example.com')
        _, send, seen = self._run(
            [self._raw(subject=f'Re: [APT-{lead.pk}] Your visit')])

        send.assert_not_called()
        self.assertEqual(seen, [])

    def test_a_thread_reply_from_a_whatsapp_lead_is_answered(self):
        lead = self._lead(customer_email='')
        self._run([self._raw(subject=f'Re: [APT-{lead.pk}] Your visit')])

        lead.refresh_from_db()
        self.assertEqual(len(lead.conversation_history), 2)
        # The address is captured from the sender so we can reply again later.
        self.assertEqual(lead.customer_email, 'jane@example.com')

    def test_the_reply_goes_to_the_leads_own_tenant(self):
        """No routing needed: the matched lead carries its own tenant."""
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        lead = self._lead(tenant=acme)
        self._run([self._raw()])

        lead.refresh_from_db()
        self.assertEqual(lead.tenant_id, acme.pk)
        self.assertEqual(len(lead.conversation_history), 2)

    def test_the_most_recently_touched_lead_wins(self):
        """The same address on two tenants' books answers as the live one."""
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        old = self._lead(7201)
        recent = self._lead(7202, tenant=acme)
        Appointment.objects.filter(pk=old.pk).update(
            updated_at=timezone.now() - timedelta(days=30))

        self._run([self._raw()])

        old.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(old.conversation_history or [], [])
        self.assertEqual(len(recent.conversation_history), 2)

    # ── The gates in front of the rule ──────────────────────────────────────

    def test_automated_senders_are_never_answered(self):
        self._lead()
        cases = [
            {'sender': 'no-reply@bank.example'},
            {'sender': 'mailer-daemon@example.com',
             'subject': 'Undelivered Mail Returned to Sender'},
            {'sender': 'jane@example.com', 'subject': 'Automatic reply: Out of office'},
            {'sender': 'jane@example.com',
             'extra_headers': ('Auto-Submitted: auto-replied',)},
            {'sender': 'jane@example.com',
             'extra_headers': ('List-Id: <news.example.com>',)},
        ]
        for case in cases:
            with self.subTest(**case):
                _, send, _ = self._run([self._raw(**case)])
                send.assert_not_called()

    def test_our_own_mail_is_never_answered_and_is_left_unread(self):
        """Alerts and Bcc copies of our own sends land in this mailbox. The bot
        must not answer them, and must not mark the operator's mail as read."""
        lead = self._lead()
        cases = [
            {'sender': 'team@example.com'},                       # the polled box
            {'sender': 'homebase@notifications.homexmedia.com'},   # platform sender
            {'sender': 'jones86xi@gmail.com'},                     # operator inbox
            # A Bcc copy carries the same thread tag the customer's reply does.
            {'sender': 'homebase@notifications.homexmedia.com',
             'subject': f'[APT-{lead.pk}] Your booking'},
        ]
        for case in cases:
            with self.subTest(**case):
                _, send, seen = self._run([self._raw(**case)])
                send.assert_not_called()
                self.assertEqual(seen, [])

    def test_a_dry_run_neither_replies_nor_touches_the_mailbox(self):
        lead = self._lead()
        _, send, seen = self._run([self._raw()], dry_run=True)

        lead.refresh_from_db()
        send.assert_not_called()
        self.assertEqual(seen, [])
        self.assertEqual(lead.conversation_history or [], [])

    def test_email_only_leads_are_never_whatsapped(self):
        """Legacy `email_` rows have no phone number, so a proactive WhatsApp
        send would 400."""
        from bot.management.commands.send_followups import Command as Followups
        from bot.management.commands.send_reminders import _send_wa

        apt = Appointment.objects.create(
            phone_number='email_e62068d37db3',
            customer_email='jane@example.com',
            status='pending', is_lead_active=True,
        )

        eligible = Followups()._get_eligible_leads(timezone.now(), force=False)
        self.assertNotIn(apt.pk, [lead.pk for lead in eligible])

        with patch('bot.whatsapp_cloud_api.get_client_for_tenant') as client:
            self.assertFalse(_send_wa(apt.phone_number, 'hello'))
            client.assert_not_called()


    # ── Emailed reschedules ──────────────────────────────────────────────
    def _resched_lead(self, suffix):
        return self._lead(suffix, status='confirmed',
                          scheduled_datetime=timezone.now() + timedelta(days=2))

    def test_an_emailed_reschedule_runs_the_full_pipeline(self):
        """It used to just assign scheduled_datetime: the customer was told the
        move was done while the plumber, the calendar and the lead's own record
        stayed on the old time."""
        from .management.commands.process_inbound_emails import _apply_email_reschedule
        from .views.plumbot.base import Plumbot

        lead = self._resched_lead(7290)
        new = timezone.now() + timedelta(days=5)
        with patch.object(Plumbot, 'update_google_calendar_appointment') as cal, \
             patch.object(Plumbot, 'notify_team_about_reschedule') as notify, \
             patch.object(Plumbot, '_reschedule_availability', return_value=(True, None)):
            note = _apply_email_reschedule(lead, new, dry_run=False, out=lambda *a: None)

        lead.refresh_from_db()
        self.assertAlmostEqual(lead.scheduled_datetime, new, delta=timedelta(seconds=2))
        self.assertTrue(notify.called)
        self.assertTrue(cal.called)
        self.assertIn('rescheduled', note.lower())
        self.assertIn('Rescheduled by customer', lead.admin_notes or '')

    def test_an_emailed_reschedule_into_a_taken_slot_moves_nothing(self):
        from .management.commands.process_inbound_emails import _apply_email_reschedule
        from .views.plumbot.base import Plumbot

        lead = self._resched_lead(7291)
        was = lead.scheduled_datetime
        with patch.object(Plumbot, 'notify_team_about_reschedule') as notify, \
             patch.object(Plumbot, '_reschedule_availability',
                          return_value=(False, 'conflict')):
            note = _apply_email_reschedule(lead, timezone.now() + timedelta(days=5),
                                           dry_run=False, out=lambda *a: None)

        lead.refresh_from_db()
        self.assertAlmostEqual(lead.scheduled_datetime, was, delta=timedelta(seconds=2))
        notify.assert_not_called()
        self.assertIn('another day', note)

    def test_a_dry_run_reschedule_writes_nothing(self):
        from .management.commands.process_inbound_emails import _apply_email_reschedule
        from .views.plumbot.base import Plumbot

        lead = self._resched_lead(7292)
        was = lead.scheduled_datetime
        with patch.object(Plumbot, 'notify_team_about_reschedule') as notify, \
             patch.object(Plumbot, '_reschedule_availability', return_value=(True, None)):
            _apply_email_reschedule(lead, timezone.now() + timedelta(days=5),
                                    dry_run=True, out=lambda *a: None)

        lead.refresh_from_db()
        self.assertAlmostEqual(lead.scheduled_datetime, was, delta=timedelta(seconds=2))
        notify.assert_not_called()

class JobSchedulingTests(StaffClientTestCase):
    """schedule_job used to write with `Appointment.objects.update(...)` — a
    manager-level update with no filter, so one job's datetime, name and area
    landed on every lead of every tenant and the whole board went VERY HOT
    (a `scheduled_datetime` scores 100). The page had only ever been
    GET-smoke-tested, which is why nothing caught it."""

    def setUp(self):
        super().setUp()
        self.site_visit = make_lead(
            700,
            customer_name='Site Visit Lead',
            customer_area='Avondale',
            status='confirmed',
            appointment_type='site_visit',
            site_visit_completed=True,
            job_status='pending_schedule',
        )
        self.bystander = make_lead(
            701, customer_name='Untouched Lead', customer_area='Borrowdale')

    @staticmethod
    def _job_slot():
        """The next Wednesday at 10:00 — inside every default business week."""
        import pytz
        sa = pytz.timezone('Africa/Johannesburg')
        moment = timezone.now().astimezone(sa) + timedelta(days=1)
        while moment.weekday() != 2:
            moment += timedelta(days=1)
        return moment.strftime('%Y-%m-%d'), '10:00'

    def _post_job(self):
        job_date, job_time = self._job_slot()
        with patch('bot.views.jobs.get_client_for_tenant'), \
             patch('bot.views.jobs.send_plumber_notification_email'):
            return self.client.post(
                reverse('schedule_job', args=[self.site_visit.pk]),
                {'job_date': job_date, 'job_time': job_time,
                 'duration_hours': '4', 'job_description': 'Install the tub',
                 'materials_needed': 'Tub, waste kit'},
            )

    def test_scheduling_a_job_converts_only_that_lead(self):
        response = self._post_job()
        self.assertEqual(response.status_code, 302)

        self.site_visit.refresh_from_db()
        self.assertEqual(self.site_visit.appointment_type, 'job_appointment')
        self.assertEqual(self.site_visit.job_status, 'scheduled')
        self.assertIsNotNone(self.site_visit.job_scheduled_datetime)
        self.assertEqual(self.site_visit.job_duration_hours, 4)
        self.assertEqual(self.site_visit.job_description, 'Install the tub')
        # Its own identity survives — the job is the same customer.
        self.assertEqual(self.site_visit.customer_name, 'Site Visit Lead')

    def test_scheduling_a_job_leaves_every_other_lead_alone(self):
        """The actual production symptom: every lead in VERY HOT."""
        self._post_job()

        self.bystander.refresh_from_db()
        self.assertEqual(self.bystander.customer_name, 'Untouched Lead')
        self.assertEqual(self.bystander.customer_area, 'Borrowdale')
        self.assertEqual(self.bystander.appointment_type, 'site_visit')
        self.assertIsNone(self.bystander.scheduled_datetime)

        from bot.services.lead_scoring import calculate_lead_score
        score, status = calculate_lead_score(self.bystander)
        self.assertNotEqual(status, 'very_hot')
        self.assertNotEqual(score, 100)

    def test_manager_level_update_is_refused(self):
        """The footgun itself: Django copies update() onto the manager, where
        it carries no filter. Appointment.objects.update() must never run."""
        with self.assertRaises(TypeError):
            Appointment.objects.update(scheduled_datetime=timezone.now())

        self.bystander.refresh_from_db()
        self.assertIsNone(self.bystander.scheduled_datetime)

        # A filtered update still works — only the unfiltered call is blocked.
        Appointment.objects.filter(pk=self.bystander.pk).update(
            customer_area='Mount Pleasant')
        self.bystander.refresh_from_db()
        self.assertEqual(self.bystander.customer_area, 'Mount Pleasant')

    def test_incomplete_site_visit_cannot_be_scheduled(self):
        self.site_visit.site_visit_completed = False
        self.site_visit.job_status = 'not_applicable'
        self.site_visit.save(update_fields=['site_visit_completed', 'job_status'])

        self._post_job()
        self.site_visit.refresh_from_db()
        self.assertEqual(self.site_visit.appointment_type, 'site_visit')
        self.assertIsNone(self.site_visit.job_scheduled_datetime)

    def test_jobs_list_lists_job_appointments(self):
        """The list filtered on appointment_type='job' — a value no correct
        path writes — so it was always empty (and showed the whole table once
        the broken update had stamped 'job' everywhere)."""
        self._post_job()
        response = self.client.get(reverse('job_appointments_list'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Site Visit Lead')
        self.assertNotContains(response, 'Untouched Lead')


class MassJobUpdateRepairTests(StaffClientTestCase):
    """The repair command for tenants already hit in production."""

    def _damaged(self, suffix, **kwargs):
        lead = make_lead(suffix, **kwargs)
        # Reproduce the smear exactly: a manager-level update bypasses save(),
        # so end_datetime and booked_at keep their pre-incident values.
        Appointment.objects.filter(pk=lead.pk).update(
            customer_name='Smeared Name',
            customer_area='Smeared Area',
            scheduled_datetime=timezone.now() + timedelta(days=3),
            appointment_type='job',
            status='scheduled',
        )
        lead.refresh_from_db()
        return lead

    def test_repair_restores_booked_and_unbooked_leads(self):
        booked_time = timezone.now() + timedelta(days=2)
        booked = self._damaged(
            710, status='confirmed', scheduled_datetime=booked_time)
        never_booked = self._damaged(711)

        call_command('repair_mass_job_update', '--apply', stdout=StringIO())

        booked.refresh_from_db()
        self.assertEqual(booked.appointment_type, 'site_visit')
        self.assertEqual(booked.status, 'confirmed')
        # end_datetime survived the smear, so the original time comes back.
        self.assertAlmostEqual(
            booked.scheduled_datetime, booked_time, delta=timedelta(seconds=2))

        never_booked.refresh_from_db()
        self.assertEqual(never_booked.appointment_type, 'site_visit')
        self.assertEqual(never_booked.status, 'pending')
        self.assertIsNone(never_booked.scheduled_datetime)
        self.assertNotEqual(never_booked.lead_status, 'very_hot')

    def test_dry_run_writes_nothing(self):
        lead = self._damaged(712)
        call_command('repair_mass_job_update', stdout=StringIO())
        lead.refresh_from_db()
        self.assertEqual(lead.appointment_type, 'job')
        self.assertEqual(lead.status, 'scheduled')


class BookingConfirmationMessageTests(TestCase):
    """The WhatsApp booking confirmation: written like a person, no emojis,
    held back a beat instead of firing the instant the booking saved, and
    never sent twice for the same slot."""

    EMOJI = _EMOJI_RE

    def setUp(self):
        self.lead = make_lead(9901, customer_area='Borrowdale',
                              customer_name='Tinashe')
        from .views.plumbot.base import Plumbot
        self.bot = Plumbot(self.lead.phone_number)
        self.assertEqual(self.bot.appointment.pk, self.lead.pk)
        self.when = timezone.now() + timedelta(days=2)
        self.info = {'project_type': 'bathroom_renovation', 'area': 'Borrowdale'}

    def test_confirmation_reads_like_a_person(self):
        text = self.bot._build_confirmation_message(self.info, self.when)
        # The old copy was a labelled card with an emoji on every line.
        self.assertNotIn('APPOINTMENT CONFIRMED', text)
        for label in ('Date:', 'Time:', 'Area:', 'Service:'):
            self.assertNotIn(label, text)
        self.assertIsNone(self.EMOJI.search(text), text)
        self.assertIn("in writing", text)
        self.assertIn("you're booked for", text)
        self.assertIn('Borrowdale', text)
        self.assertIn('bathroom renovation', text)
        self.assertIn('just message me here', text)

    def test_absent_details_are_omitted_not_invented(self):
        text = self.bot._build_confirmation_message(
            {'project_type': 'geyser_installation'}, self.when)
        self.assertNotIn('Your area', text)
        self.assertIn('geyser installation', text)
        self.assertIsNone(self.EMOJI.search(text), text)

    def test_shona_lead_gets_a_shona_confirmation(self):
        self.lead.add_conversation_message('user', 'Ndinoda kugadzirisa bhavhu, marii?')
        self.bot.appointment.refresh_from_db()
        text = self.bot._build_confirmation_message(self.info, self.when)
        self.assertIn('Tichakufonerai', text)
        self.assertIsNone(self.EMOJI.search(text), text)

    def test_confirmation_is_queued_behind_a_short_delay_and_only_once(self):
        from .views.plumbot import notification_mixin as nm
        with patch.object(nm.threading, 'Thread') as thread:
            self.bot.send_confirmation_message(self.info, self.when)
            # Same booking triggered again (repeated "yes", Confirm pressed
            # twice) — one confirmation is enough.
            self.bot.send_confirmation_message(self.info, self.when)
            self.assertEqual(thread.call_count, 1)
            self.assertTrue(thread.return_value.start.called)

            delay = thread.call_args.kwargs['args'][2]
            self.assertGreaterEqual(delay, self.bot.CONFIRMATION_DELAY_MIN_SECONDS)
            self.assertLessEqual(delay, self.bot.CONFIRMATION_DELAY_MAX_SECONDS)

            # A genuine re-book at a NEW time still confirms.
            self.bot.send_confirmation_message(self.info, self.when + timedelta(days=1))
            self.assertEqual(thread.call_count, 2)

    def test_the_claim_survives_a_later_full_save_of_the_lead(self):
        # The claim is written with a conditional UPDATE; the in-memory copy
        # has to learn about it or a full save() off the same instance writes
        # the pre-claim notes back and a second confirmation slips through.
        from .views.plumbot import notification_mixin as nm
        with patch.object(nm.threading, 'Thread') as thread:
            self.bot.send_confirmation_message(self.info, self.when)
            self.bot.appointment.save()
            self.bot.send_confirmation_message(self.info, self.when)
        self.assertEqual(thread.call_count, 1)


class ReschedulePipelineTests(TestCase):
    """Customer-initiated reschedules over WhatsApp.

    Three of the methods this path called did not exist anywhere in the repo —
    the plumber alert, the calendar move and the keyword fallback — and every
    failure was swallowed by a bare except, so the customer was told their new
    time was confirmed while nothing else moved.
    """

    def setUp(self):
        self.homebase = Tenant.objects.get(slug='homebase')
        self.acme = Tenant.objects.create(name='Acme Plumbing', slug='acme')
        self.old = timezone.now() + timedelta(days=2)
        self.new = timezone.now() + timedelta(days=4)
        self.lead = make_lead(9801, tenant=self.homebase, status='confirmed',
                              customer_name='Tinashe', customer_area='Borrowdale',
                              project_type='bathroom_renovation',
                              scheduled_datetime=self.old)
        self.bot = self._bot(self.lead)

    def _bot(self, lead):
        from .views.plumbot.base import Plumbot
        return Plumbot(lead.phone_number, tenant=lead.tenant)

    def _job_lead(self):
        lead = make_lead(9802, tenant=self.homebase, status='confirmed',
                         customer_name='Rudo', customer_area='Avondale',
                         appointment_type='job_appointment',
                         scheduled_datetime=timezone.now() - timedelta(days=7),
                         job_scheduled_datetime=self.old,
                         job_duration_hours=4, job_status='scheduled')
        return lead, self._bot(lead)

    # ── detection ────────────────────────────────────────────────────────
    def test_keyword_fallback_detects_a_reschedule_without_the_api(self):
        for message in ("Something came up, can we move it?",
                        "I need to reschedule",
                        "can't make Thursday",
                        "ndinoda kuchinja zuva"):
            with self.subTest(message=message):
                self.assertTrue(self.bot.detect_reschedule_request(message))

        for message in ("Thanks for confirming", "How much will it cost?",
                        "Do you need directions?"):
            with self.subTest(message=message):
                self.assertFalse(self.bot.detect_reschedule_request(message))

    def test_keyword_fallback_stays_quiet_without_a_confirmed_slot(self):
        self.lead.status = 'pending'
        self.lead.save(update_fields=['status'])
        self.bot.appointment.refresh_from_db()
        self.assertFalse(self.bot.detect_reschedule_request('can we reschedule?'))

    def test_ai_detector_degrades_to_keywords_when_deepseek_is_down(self):
        # The except branch called detect_reschedule_request, which did not
        # exist — an API blip raised AttributeError out of the detector.
        from .views.plumbot import reschedule_mixin as rm
        with patch.object(rm.deepseek_client.chat.completions, 'create',
                          side_effect=RuntimeError('deepseek down')):
            self.assertTrue(
                self.bot.detect_reschedule_request_with_ai('something came up, can we move it?'))
            self.assertFalse(
                self.bot.detect_reschedule_request_with_ai('thanks for confirming'))

    # ── which slot moves ─────────────────────────────────────────────────
    def test_site_visit_move_writes_the_visit_slot(self):
        from .views.plumbot.base import Plumbot
        with patch.object(Plumbot, 'update_google_calendar_appointment'), \
             patch.object(Plumbot, 'notify_team_about_reschedule'):
            reply = self.bot.process_successful_reschedule(self.old, self.new)
        self.lead.refresh_from_db()
        self.assertAlmostEqual(self.lead.scheduled_datetime, self.new,
                               delta=timedelta(seconds=2))
        self.assertIn('moved you to', reply)

    def test_job_move_writes_the_job_slot_not_the_site_visit(self):
        # schedule_job_appointment keeps one row, so a job customer still has
        # the old site-visit datetime on scheduled_datetime. The bot used to
        # move THAT and leave the jobs board on the old job time.
        from .views.plumbot.base import Plumbot
        lead, bot = self._job_lead()
        visit = lead.scheduled_datetime
        with patch.object(Plumbot, 'update_google_calendar_appointment'), \
             patch.object(Plumbot, 'notify_team_about_reschedule'):
            bot.process_successful_reschedule(self.old, self.new)
        lead.refresh_from_db()
        self.assertAlmostEqual(lead.job_scheduled_datetime, self.new,
                               delta=timedelta(seconds=2))
        self.assertAlmostEqual(lead.scheduled_datetime, visit,
                               delta=timedelta(seconds=2))

    def test_the_job_slot_is_the_one_quoted_back_to_the_customer(self):
        lead, bot = self._job_lead()
        field, current = bot._reschedule_slot()
        self.assertEqual(field, 'job_scheduled_datetime')
        self.assertAlmostEqual(current, self.old, delta=timedelta(seconds=2))

    # ── who gets told ────────────────────────────────────────────────────
    def test_plumber_is_told_about_the_move(self):
        with patch('bot.whatsapp_cloud_api.get_client_for_tenant') as client, \
             patch('bot.plumber_notifications.send_plumber_notification_email') as mail:
            self.bot.notify_team_about_reschedule(self.old, self.new)

        self.assertTrue(client.return_value.send_text_message.called)
        number, text = client.return_value.send_text_message.call_args.args
        self.assertEqual(number, '263774819901')          # the tenant's own line
        self.assertIn('RESCHEDULED', text)
        self.assertIn('Tinashe', text)
        self.assertTrue(mail.called)

    def test_a_tenant_with_no_plumber_line_still_gets_the_email(self):
        lead = make_lead(9803, tenant=self.acme, status='confirmed',
                         scheduled_datetime=self.old)
        bot = self._bot(lead)
        with patch('bot.whatsapp_cloud_api.get_client_for_tenant') as client, \
             patch('bot.plumber_notifications.send_plumber_notification_email') as mail:
            bot.notify_team_about_reschedule(self.old, self.new)
        client.return_value.send_text_message.assert_not_called()
        self.assertTrue(mail.called)

    def test_move_is_recorded_on_the_lead(self):
        from .views.plumbot.base import Plumbot
        with patch.object(Plumbot, 'update_google_calendar_appointment'), \
             patch.object(Plumbot, 'notify_team_about_reschedule'):
            self.bot.process_successful_reschedule(self.old, self.new)
        self.lead.refresh_from_db()
        self.assertIn('Rescheduled by customer', self.lead.admin_notes or '')

    # ── calendar ─────────────────────────────────────────────────────────
    def test_calendar_event_is_patched_not_duplicated(self):
        from .views.plumbot import notification_mixin as nm
        self.lead.google_calendar_event_id = 'evt-123'
        self.lead.save(update_fields=['google_calendar_event_id'])
        self.bot.appointment.refresh_from_db()

        with patch.object(nm, 'GOOGLE_CALENDAR_CREDENTIALS', {'type': 'service_account'}), \
             patch.object(nm, 'service_account'), \
             patch.object(nm, 'build') as build:
            self.bot.update_google_calendar_appointment(self.old, self.new)

        events = build.return_value.events.return_value
        self.assertTrue(events.patch.called)
        self.assertFalse(events.insert.called)
        self.assertEqual(events.patch.call_args.kwargs['eventId'], 'evt-123')

    def test_calendar_creates_an_event_when_none_is_on_file(self):
        from .views.plumbot import notification_mixin as nm
        from .views.plumbot.base import Plumbot
        with patch.object(nm, 'GOOGLE_CALENDAR_CREDENTIALS', {'type': 'service_account'}), \
             patch.object(Plumbot, 'add_to_google_calendar') as add:
            self.bot.update_google_calendar_appointment(self.old, self.new)
        self.assertTrue(add.called)

    def test_booking_stores_the_calendar_event_id(self):
        from .views.plumbot import notification_mixin as nm
        with patch.object(nm, 'GOOGLE_CALENDAR_CREDENTIALS', {'type': 'service_account'}), \
             patch.object(nm, 'service_account'), \
             patch.object(nm, 'build') as build:
            build.return_value.events.return_value.insert.return_value.execute.return_value = {
                'id': 'evt-new'}
            self.bot.add_to_google_calendar({'name': 'Tinashe'}, self.new)
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.google_calendar_event_id, 'evt-new')

    # ── copy ─────────────────────────────────────────────────────────────
    def test_reschedule_copy_is_human_and_tenant_safe(self):
        texts = [
            self.bot._build_reschedule_confirmation(self.old, self.new),
            self.bot._build_reschedule_clarification('Monday, June 01 at 10:00 AM'),
            self.bot._build_reschedule_unavailable_reply([]),
            self.bot._build_reschedule_unavailable_reply(
                [{'display': 'Monday at 10:00 AM'}, {'display': 'Tuesday at 2:00 PM'}]),
            self.bot._reschedule_breakdown_reply(),
        ]
        for text in texts:
            with self.subTest(text=text[:40]):
                self.assertIsNone(_EMOJI_RE.search(text), text)
                self.assertNotIn('555', text)              # (555) PLUMBING
                self.assertNotIn('PLUMBING', text)
                self.assertNotIn('Monday to Friday', text)  # hardcoded week
        # The breakdown reply offers the tenant's OWN line, never a placeholder.
        self.assertIn('263774819901', texts[-1])

    def test_breakdown_reply_omits_a_number_the_tenant_does_not_have(self):
        lead = make_lead(9804, tenant=self.acme, status='confirmed',
                         scheduled_datetime=self.old)
        text = self._bot(lead)._reschedule_breakdown_reply()
        self.assertNotIn('263774819901', text)             # never homebase's
        self.assertNotIn('555', text)
        self.assertIn('day and time', text)

    def test_unavailable_reply_survives_an_alternatives_lookup_failure(self):
        # The old except branch read `alternatives` before it was assigned.
        from .views.plumbot.base import Plumbot
        with patch.object(Plumbot, 'get_alternative_time_suggestions',
                          side_effect=RuntimeError('diary unavailable')):
            reply = self.bot.handle_unavailable_reschedule_with_ai(self.new, 'monday at 2pm')
        self.assertIn('already taken', reply)

    def test_a_failed_save_never_claims_the_move_happened(self):
        from .views.plumbot.base import Plumbot
        with patch.object(type(self.bot.appointment), 'save',
                          side_effect=RuntimeError('db down')), \
             patch.object(Plumbot, 'notify_team_about_reschedule') as notify:
            reply = self.bot.process_successful_reschedule(self.old, self.new)
        self.assertNotIn('moved you to', reply)
        self.assertIn('day and time', reply)
        notify.assert_not_called()


class DeferredImportTests(TestCase):
    """
    Every ``from bot.<module> import <name>`` in the app must resolve.

    Most of these live INSIDE a function body — deferred on purpose, to dodge
    circular imports — so a name the module no longer exports raises nothing at
    boot and nothing in a normal request. It only detonates the first time that
    branch is actually reached, which for a cron branch can be days later:
    ``send_reminders`` crashed in production on 2026-08-31 with
    ``ImportError: cannot import name '_WA_NUMBER' from 'bot.customer_emails'``
    — the multi-tenancy refactor had replaced that module constant with the
    per-lead ``_wa_number(apt)`` helper months earlier, but the delayed-lead
    EMAIL branch (only reached when the WhatsApp window is shut AND the lead
    has an email on file) still imported the old name, so the whole reminder
    dispatcher died mid-run.

    Static review does not see these; this test does.
    """

    def _bot_modules(self):
        import ast
        bot_dir = os.path.dirname(os.path.abspath(__file__))
        for root, dirs, files in os.walk(bot_dir):
            dirs[:] = [d for d in dirs if d not in ('__pycache__', 'migrations')]
            for fname in sorted(files):
                if not fname.endswith('.py'):
                    continue
                path = os.path.join(root, fname)
                with open(path, encoding='utf-8-sig') as fh:
                    yield path, ast.parse(fh.read())

    def test_every_deferred_import_name_still_exists(self):
        import ast
        import importlib

        missing = []
        for path, tree in self._bot_modules():
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                if node.level or not (node.module or '').startswith('bot'):
                    continue
                try:
                    module = importlib.import_module(node.module)
                except ImportError:
                    continue          # optional/env-gated module, not our business
                for alias in node.names:
                    if alias.name == '*' or hasattr(module, alias.name):
                        continue
                    try:
                        importlib.import_module(f'{node.module}.{alias.name}')
                    except ImportError:
                        missing.append(
                            f'{os.path.basename(path)}:{node.lineno} '
                            f'from {node.module} import {alias.name}'
                        )

        self.assertEqual(
            missing, [],
            'Import(s) naming something that no longer exists:\n  '
            + '\n  '.join(missing)
        )


class DashboardScheduleTests(StaffClientTestCase):
    """The dashboard is the plumber's diary, so what it shows has to be what is
    actually in the diary. Three bugs pinned here:

    1. The schedule was filtered on ``last_customer_response`` (default: 7
       days), so a customer who booked further out than that and then went
       quiet vanished from the dashboard while the visit was still on.
    2. The "This week" figure counted only the days AFTER tomorrow, so one
       visit today rendered as "Today's Appts 1 / This week: 0".
    3. "Jobs This Week" read the ``bot.models.Job`` table, which nothing in the
       app writes (jobs live on the Appointment row — appointment_type=
       'job_appointment' + job_scheduled_datetime), so it showed 0 forever;
       the query was also unscoped, i.e. every tenant's rows.
    """

    def _local_9am(self, days=0):
        return timezone.localtime(timezone.now()).replace(
            hour=9, minute=0, second=0, microsecond=0) + timedelta(days=days)

    def _dashboard(self):
        response = self.client.get(reverse('dashboard'))
        self.assertEqual(response.status_code, 200)
        return response

    def test_booking_shows_even_when_the_lead_went_quiet(self):
        lead = make_lead(
            7101, customer_name='Quiet Booker', status='confirmed',
            scheduled_datetime=self._local_9am(),
            last_customer_response=timezone.now() - timedelta(days=30),
        )
        response = self._dashboard()
        self.assertIn(lead, list(response.context['todays_confirmed_appointments']))
        self.assertIn('Quiet Booker', response.content.decode())

    def test_week_count_covers_the_whole_week_including_today(self):
        today = timezone.localdate()
        week_end = today + timedelta(days=(6 - today.weekday()))
        make_lead(7102, customer_name='Today Visit', status='confirmed',
                  scheduled_datetime=self._local_9am(),
                  last_customer_response=timezone.now())
        expected = 1
        if week_end > today:
            make_lead(7103, customer_name='Later Visit', status='confirmed',
                      scheduled_datetime=self._local_9am((week_end - today).days),
                      last_customer_response=timezone.now())
            expected = 2
        self.assertEqual(self._dashboard().context['week_appointment_count'], expected)

    def test_unconfirmed_slot_is_not_counted_as_a_booking(self):
        # Every other surface counts a booking as status='confirmed'; the week
        # list used to also admit 'pending', so a proposed slot showed there
        # and nowhere else.
        make_lead(7104, customer_name='Proposed Only', status='pending',
                  scheduled_datetime=self._local_9am(),
                  last_customer_response=timezone.now())
        response = self._dashboard()
        self.assertEqual(list(response.context['todays_confirmed_appointments']), [])
        self.assertEqual(response.context['week_appointment_count'], 0)

    def test_jobs_this_week_reads_the_appointment_row(self):
        job = make_lead(
            7105, customer_name='Job Customer', customer_area='Madokero',
            status='confirmed', appointment_type='job_appointment',
            job_status='scheduled', job_scheduled_datetime=self._local_9am(),
            scheduled_datetime=self._local_9am(-7),
            last_customer_response=timezone.now() - timedelta(days=30),
        )
        response = self._dashboard()
        self.assertIn(job, list(response.context['week_jobs']))
        self.assertIn('Job Customer', response.content.decode())

    def test_cancelled_job_is_not_on_the_week(self):
        make_lead(7106, customer_name='Called Off', status='confirmed',
                  appointment_type='job_appointment', job_status='cancelled',
                  job_scheduled_datetime=self._local_9am(),
                  last_customer_response=timezone.now())
        self.assertEqual(list(self._dashboard().context['week_jobs']), [])

    def test_jobs_are_tenant_scoped(self):
        acme = Tenant.objects.create(name='Acme Plumbing', slug='acme-jobs')
        make_lead(7107, tenant=acme, customer_name='Acme Job', status='confirmed',
                  appointment_type='job_appointment', job_status='scheduled',
                  job_scheduled_datetime=self._local_9am(),
                  last_customer_response=timezone.now())
        response = self._dashboard()
        self.assertEqual(list(response.context['week_jobs']), [])
        self.assertNotIn('Acme Job', response.content.decode())


# ======================================================================
# Post-visit debrief form + quote follow-up automation
# ======================================================================

class PostVisitFormTests(StaffClientTestCase):
    """The two entry points, the gate, and the single-use rule."""

    def setUp(self):
        super().setUp()
        self.lead = make_lead(
            8100, customer_name='Rudo Moyo', customer_area='Borrowdale',
            project_type='bathroom_renovation', status='confirmed',
            scheduled_datetime=timezone.now() - timedelta(hours=3),
            customer_email='rudo@example.com',
        )

    def _report(self):
        from bot.post_visit import ensure_report
        return ensure_report(self.lead)

    # -- the in-app entry point -------------------------------------------

    def _banner(self, lead=None):
        response = self.client.get(
            reverse('appointment_detail', args=[(lead or self.lead).pk]))
        return response.context['site_visit_banner'], response.content.decode()

    def test_banner_shows_on_a_finished_visit(self):
        banner, body = self._banner()
        self.assertEqual(banner['state'], 'open')
        self.assertIn('Is the site visit complete?', body)

    def test_banner_shows_even_before_the_visit_has_happened(self):
        """It is the only way to log a visit, so it is never conditional on the
        row looking tidy: real visits happen without the bot pinning a slot."""
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=1)
        self.lead.save()
        banner, body = self._banner()
        self.assertEqual(banner['state'], 'open')
        self.assertIn('Is the site visit complete?', body)

    def test_banner_shows_on_a_lead_with_no_booking_at_all(self):
        bare = make_lead(8150, customer_name='Never Booked', status='pending')
        banner, body = self._banner(bare)
        self.assertEqual(banner['state'], 'open')
        self.assertIn('Is the site visit complete?', body)

    def test_the_old_complete_site_visit_route_is_gone(self):
        """One way to log a visit, one place it can be done."""
        from django.urls import NoReverseMatch
        with self.assertRaises(NoReverseMatch):
            reverse('complete_site_visit', args=[self.lead.pk])
        self.assertEqual(
            self.client.get(f'/appointments/{self.lead.pk}/complete-site-visit/').status_code,
            404)
        self.assertNotIn('Complete Site Visit', self._banner()[1])

    def test_rendering_the_page_does_not_create_a_report(self):
        """A page view is not an action - creating the row here would start the
        fallback-email clock on every render."""
        from bot.models import SiteVisitReport
        self.client.get(reverse('appointment_detail', args=[self.lead.pk]))
        self.assertFalse(SiteVisitReport.objects.filter(appointment=self.lead).exists())

    def test_in_app_button_opens_the_same_tokenized_form(self):
        from bot.models import SiteVisitReport
        response = self.client.get(reverse('site_visit_start', args=[self.lead.pk]))
        report = SiteVisitReport.objects.get(appointment=self.lead)
        self.assertRedirects(
            response, reverse('site_visit_form', kwargs={'token': report.token}))

    def test_banner_reports_the_outcome_once_logged(self):
        """Resolved, not gone: the banner must never become a dead control, and
        the plumber should be able to see what their answer set running."""
        from bot.post_visit import apply_submission
        apply_submission(self._report(), outcome='went_ahead', expectation='unknown',
                         email='rudo@example.com')
        banner, body = self._banner()
        self.assertEqual(banner['state'], 'resolved')
        self.assertIn('Site visit logged', body)
        self.assertNotIn('Is the site visit complete?', body)
        self.assertIn('Chasing a firmer date by email', body)

    def test_a_logged_visit_with_a_date_says_when_we_confirm(self):
        from bot.post_visit import apply_submission
        target = timezone.localdate() + timedelta(days=9)
        apply_submission(self._report(), outcome='went_ahead',
                         expectation='specific_date', expected_date=target,
                         email='rudo@example.com')
        _, body = self._banner()
        self.assertIn('we confirm two days before', body)
        self.assertIn(target.strftime('%B'), body)

    def test_a_closed_out_visit_says_nothing_is_going_out(self):
        from bot.post_visit import ensure_report
        lead = make_lead(8160, status='confirmed',
                         scheduled_datetime=timezone.now() - timedelta(hours=3))
        report = ensure_report(lead)
        self.client.post(reverse('site_visit_form', kwargs={'token': report.token}),
                         {'outcome': 'not_proceeding'})
        banner, body = self._banner(lead)
        self.assertEqual(banner['state'], 'resolved')
        self.assertIn('No quote and no follow-ups will go out', body)

    # -- the form itself ---------------------------------------------------

    def test_form_is_public_and_token_gated(self):
        report = self._report()
        self.client.logout()
        ok = self.client.get(reverse('site_visit_form', kwargs={'token': report.token}))
        self.assertEqual(ok.status_code, 200)
        missing = self.client.get(
            reverse('site_visit_form', kwargs={'token': 'not-a-real-token'}))
        self.assertEqual(missing.status_code, 404)

    def test_form_prefills_what_we_already_know(self):
        report = self._report()
        body = self.client.get(
            reverse('site_visit_form', kwargs={'token': report.token})).content.decode()
        self.assertIn('Rudo Moyo', body)
        self.assertIn('rudo@example.com', body)

    def test_went_ahead_with_a_date_arms_the_confirmation(self):
        report = self._report()
        target = timezone.localdate() + timedelta(days=10)
        response = self.client.post(
            reverse('site_visit_form', kwargs={'token': report.token}),
            {'outcome': 'went_ahead', 'lead_email': 'rudo@example.com',
             'expectation': 'specific_date', 'expected_date': target.isoformat(),
             'job_notes': 'Retile and move the shower feed'})
        self.assertRedirects(response, reverse('create_quotation', args=[self.lead.pk]))
        report.refresh_from_db()
        self.assertEqual(report.outcome, 'went_ahead')
        self.assertEqual(report.expected_date, target)
        self.assertEqual(report.sequence, 'confirm')
        self.assertEqual(report.job_notes, 'Retile and move the shower feed')

    def test_went_ahead_with_a_timeframe_arms_the_ask_sequence(self):
        report = self._report()
        self.client.post(
            reverse('site_visit_form', kwargs={'token': report.token}),
            {'outcome': 'went_ahead', 'lead_email': 'rudo@example.com',
             'expectation': 'timeframe', 'expected_timeframe': 'two_weeks'})
        report.refresh_from_db()
        self.assertEqual(report.sequence, 'asks')
        self.assertEqual(report.expected_timeframe, 'two_weeks')
        self.assertIsNotNone(report.next_action_at)

    def test_the_form_sets_a_missing_lead_email(self):
        self.lead.customer_email = None
        self.lead.save()
        report = self._report()
        self.client.post(
            reverse('site_visit_form', kwargs={'token': report.token}),
            {'outcome': 'went_ahead', 'lead_email': 'new@example.com',
             'expectation': 'unknown'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.customer_email, 'new@example.com')

    def test_went_ahead_without_an_email_is_refused(self):
        self.lead.customer_email = None
        self.lead.save()
        report = self._report()
        response = self.client.post(
            reverse('site_visit_form', kwargs={'token': report.token}),
            {'outcome': 'went_ahead', 'expectation': 'unknown'})
        self.assertEqual(response.status_code, 200)
        report.refresh_from_db()
        self.assertIsNone(report.submitted_at)

    # -- the gate ----------------------------------------------------------

    def test_other_outcomes_produce_no_quote_and_no_sequence(self):
        for suffix, outcome in enumerate(('no_show', 'rescheduled', 'not_proceeding')):
            lead = make_lead(8200 + suffix, status='confirmed',
                             customer_email='x@example.com',
                             scheduled_datetime=timezone.now() - timedelta(hours=3))
            from bot.post_visit import ensure_report
            report = ensure_report(lead)
            response = self.client.post(
                reverse('site_visit_form', kwargs={'token': report.token}),
                {'outcome': outcome})
            self.assertEqual(response.status_code, 200, outcome)
            report.refresh_from_db()
            self.assertEqual(report.sequence, 'stopped', outcome)
            self.assertIsNone(report.next_action_at, outcome)

    def test_not_proceeding_deactivates_the_lead(self):
        report = self._report()
        self.client.post(reverse('site_visit_form', kwargs={'token': report.token}),
                         {'outcome': 'not_proceeding'})
        self.lead.refresh_from_db()
        self.assertFalse(self.lead.is_lead_active)

    def test_no_show_marks_the_appointment(self):
        report = self._report()
        self.client.post(reverse('site_visit_form', kwargs={'token': report.token}),
                         {'outcome': 'no_show'})
        self.lead.refresh_from_db()
        self.assertEqual(self.lead.status, 'no_show')

    # -- single use --------------------------------------------------------

    def test_a_second_submit_changes_nothing(self):
        report = self._report()
        url = reverse('site_visit_form', kwargs={'token': report.token})
        self.client.post(url, {'outcome': 'went_ahead', 'lead_email': 'rudo@example.com',
                               'expectation': 'timeframe', 'expected_timeframe': 'asap'})
        report.refresh_from_db()
        first_submitted, first_sequence = report.submitted_at, report.sequence

        response = self.client.post(url, {'outcome': 'not_proceeding'})
        self.assertEqual(response.status_code, 200)
        report.refresh_from_db()
        self.assertEqual(report.submitted_at, first_submitted)
        self.assertEqual(report.sequence, first_sequence)
        self.assertEqual(report.outcome, 'went_ahead')

    def test_a_used_link_says_so_instead_of_showing_the_form(self):
        report = self._report()
        url = reverse('site_visit_form', kwargs={'token': report.token})
        self.client.post(url, {'outcome': 'no_show'})
        body = self.client.get(url).content.decode()
        self.assertIn('already been logged', body)
        self.assertNotIn('name="outcome"', body)

    # -- job notes carry into the quote ------------------------------------

    def test_job_notes_prefill_the_quote_screen(self):
        report = self._report()
        self.client.post(
            reverse('site_visit_form', kwargs={'token': report.token}),
            {'outcome': 'went_ahead', 'lead_email': 'rudo@example.com',
             'expectation': 'unknown', 'job_notes': 'Second bathroom, geyser move'})
        body = self.client.get(
            reverse('create_quotation', args=[self.lead.pk])).content.decode()
        self.assertIn('Second bathroom, geyser move', body)


class PostVisitSchedulerTests(TestCase):
    """The cron: the fallback email, Cases A / B / C, and the guards."""

    def setUp(self):
        self.lead = make_lead(
            8300, customer_name='Tendai', status='confirmed',
            customer_email='tendai@example.com',
            scheduled_datetime=timezone.now() - timedelta(hours=4),
        )

    def _report(self):
        from bot.post_visit import ensure_report
        return ensure_report(self.lead)

    def _tick(self, now=None, **kw):
        from bot.post_visit import run_post_visit_tick
        return run_post_visit_tick(now=now, **kw)

    # -- the 35-minute fallback -------------------------------------------

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    def test_fallback_email_goes_out_35_minutes_after_the_visit(self, send):
        from bot.post_visit import visit_end
        just_after = visit_end(self.lead) + timedelta(minutes=36)
        stats = self._tick(now=just_after)
        self.assertEqual(stats['form_emails'], 1)
        self.assertTrue(send.called)
        self.assertIsNotNone(self._report().fallback_email_sent_at)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    def test_fallback_email_is_not_sent_early(self, send):
        from bot.post_visit import visit_end
        stats = self._tick(now=visit_end(self.lead) + timedelta(minutes=5))
        self.assertEqual(stats['form_emails'], 0)
        self.assertFalse(send.called)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    def test_the_in_app_submit_voids_the_fallback_email(self, send):
        """The primary path resolves the appointment: the fallback must not fire."""
        from bot.post_visit import apply_submission, visit_end
        apply_submission(self._report(), outcome='went_ahead', expectation='unknown',
                         email='tendai@example.com')
        stats = self._tick(now=visit_end(self.lead) + timedelta(minutes=90))
        self.assertEqual(stats['form_emails'], 0)
        self.assertFalse(send.called)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    def test_the_fallback_email_is_sent_only_once(self, send):
        from bot.post_visit import visit_end
        when = visit_end(self.lead) + timedelta(minutes=40)
        self._tick(now=when)
        self._tick(now=when + timedelta(minutes=5))
        self.assertEqual(send.call_count, 1)

    # -- Case A ------------------------------------------------------------

    @patch('bot.customer_emails._send', return_value=True)
    def test_case_a_sends_one_confirmation_two_days_out(self, _send):
        from bot.post_visit import apply_submission
        target = timezone.localdate() + timedelta(days=10)
        report = self._report()
        apply_submission(report, outcome='went_ahead', expectation='specific_date',
                         expected_date=target, email='tendai@example.com')

        # The armed moment is 09:00 local, two days before the date they gave.
        # Asserted rather than recomputed here: deriving the tick time from
        # now() + an offset made this test depend on the hour it ran at.
        report.refresh_from_db()
        due = timezone.localtime(report.next_action_at)
        self.assertEqual(due.date(), target - timedelta(days=2))
        self.assertEqual(due.hour, 9)

        # A minute early: nothing yet.
        self.assertEqual(
            self._tick(now=report.next_action_at - timedelta(minutes=1))['confirmations'], 0)

        # Due: exactly one confirmation, and then never again.
        self.assertEqual(self._tick(now=report.next_action_at)['confirmations'], 1)
        self.assertEqual(
            self._tick(now=report.next_action_at + timedelta(hours=2))['confirmations'], 0)

        report.refresh_from_db()
        self.assertEqual(report.sequence, 'done')
        self.assertIsNotNone(report.confirmation_sent_at)

    @patch('bot.customer_emails._send', return_value=True)
    def test_case_a_never_renders_a_null_date(self, _send):
        """A confirm-branch report with no date must not send a date-shaped
        email; it falls back to the ask sequence instead."""
        from bot.customer_emails import build_post_visit_confirmation_email
        with self.assertRaises(ValueError):
            build_post_visit_confirmation_email(self.lead, None)

        report = self._report()
        report.submitted_at = timezone.now()
        report.outcome = 'went_ahead'
        report.sequence = 'confirm'
        report.expected_date = None
        report.next_action_at = timezone.now() - timedelta(minutes=1)
        report.save()
        self._tick()
        report.refresh_from_db()
        self.assertEqual(report.sequence, 'asks')

    # -- Case B ------------------------------------------------------------

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_case_b_runs_three_asks_then_goes_cold(self, _send, alert):
        from bot.post_visit import apply_submission
        report = self._report()
        apply_submission(report, outcome='went_ahead', expectation='timeframe',
                         expected_timeframe='this_month', email='tendai@example.com')
        report.refresh_from_db()

        # Ask 1 — next day at noon, and ask 2 is queued three days out.
        self.assertEqual(self._tick(now=report.next_action_at)['asks'], 1)
        report.refresh_from_db()
        self.assertEqual(report.ask_count, 1)
        self.assertEqual((report.next_action_at - report.last_ask_at).days, 3)

        # Ask 2 — and ask 3 is queued seven days after it.
        self.assertEqual(self._tick(now=report.next_action_at)['asks'], 1)
        report.refresh_from_db()
        self.assertEqual(report.ask_count, 2)
        self.assertEqual((report.next_action_at - report.last_ask_at).days, 7)

        # Ask 3 — the last one; the lead gets the same week to answer it.
        self.assertEqual(self._tick(now=report.next_action_at)['asks'], 1)
        report.refresh_from_db()
        self.assertEqual(report.ask_count, 3)
        self.assertEqual((report.next_action_at - report.last_ask_at).days, 7)

        # Then cold, and back to the plumber.
        self.assertEqual(self._tick(now=report.next_action_at)['cold'], 1)
        report.refresh_from_db()
        self.lead.refresh_from_db()
        self.assertEqual(report.sequence, 'cold')
        self.assertEqual(self.lead.lead_status, 'cold')
        self.assertTrue(alert.called)

    @patch('bot.customer_emails._send', return_value=True)
    def test_ask_one_lands_at_noon_the_next_day(self, _send):
        from bot.post_visit import apply_submission
        report = self._report()
        apply_submission(report, outcome='went_ahead', expectation='unknown',
                         email='tendai@example.com')
        report.refresh_from_db()
        due = timezone.localtime(report.next_action_at)
        self.assertEqual(due.hour, 12)
        self.assertEqual(due.date(),
                         timezone.localtime(report.submitted_at).date() + timedelta(days=1))

    @patch('bot.customer_emails._send', return_value=True)
    def test_a_date_from_the_lead_switches_case_b_to_case_a(self, _send):
        from bot.post_visit import apply_submission, note_inbound_reply
        report = self._report()
        apply_submission(report, outcome='went_ahead', expectation='timeframe',
                         expected_timeframe='exploring', email='tendai@example.com')
        self.assertEqual(report.sequence, 'asks')

        target = timezone.localdate() + timedelta(days=12)
        self.lead.refresh_from_db()
        switched = note_inbound_reply(
            self.lead, 'we want it done on {}'.format(target.strftime('%d/%m/%Y')))
        self.assertTrue(switched)
        report.refresh_from_db()
        self.assertEqual(report.sequence, 'confirm')
        self.assertEqual(report.expected_date, target)

    # -- Case C ------------------------------------------------------------

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_case_c_starts_the_sequence_when_the_form_never_comes_back(self, _send, alert):
        from bot.post_visit import next_day_noon, visit_end
        report = self._report()
        deadline = next_day_noon(visit_end(self.lead))

        self.assertEqual(self._tick(now=deadline - timedelta(hours=1))['asks'], 0)
        self.assertEqual(self._tick(now=deadline + timedelta(minutes=1))['asks'], 1)

        report.refresh_from_db()
        self.assertEqual(report.sequence, 'asks')
        self.assertEqual(report.ask_count, 1)
        # The form stays open — the plumber may still get to it.
        self.assertTrue(report.is_open)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_case_c_does_not_re_fire_on_every_later_tick(self, _send, alert):
        """Case C leaves the form open, so this branch is re-entered every tick.
        Starting the sequence twice would fire ask 2 minutes after ask 1."""
        from bot.post_visit import next_day_noon, visit_end
        report = self._report()
        deadline = next_day_noon(visit_end(self.lead))
        self._tick(now=deadline + timedelta(minutes=1))
        self.assertEqual(self._tick(now=deadline + timedelta(minutes=6))['asks'], 0)
        report.refresh_from_db()
        self.assertEqual(report.ask_count, 1)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_case_c_still_paces_the_later_asks(self, _send, alert):
        """The cadence after a Case C start is the same 3-then-7 as any other."""
        from bot.post_visit import next_day_noon, visit_end
        report = self._report()
        self._tick(now=next_day_noon(visit_end(self.lead)) + timedelta(minutes=1))
        report.refresh_from_db()
        self.assertEqual(self._tick(now=report.next_action_at)['asks'], 1)
        report.refresh_from_db()
        self.assertEqual(report.ask_count, 2)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_case_c_with_no_email_hands_the_lead_back_instead(self, _send, alert):
        from bot.post_visit import next_day_noon, visit_end
        self.lead.customer_email = None
        self.lead.save()
        report = self._report()

        stats = self._tick(now=next_day_noon(visit_end(self.lead)) + timedelta(minutes=1))
        self.assertEqual(stats['no_email'], 1)
        self.assertEqual(stats['asks'], 0)
        self.assertFalse(_send.called)
        report.refresh_from_db()
        self.assertIsNotNone(report.no_email_notified_at)
        self.assertEqual(report.sequence, 'stopped')

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_the_no_email_handback_is_sent_only_once(self, _send, alert):
        from bot.post_visit import next_day_noon, visit_end
        self.lead.customer_email = None
        self.lead.save()
        self._report()
        when = next_day_noon(visit_end(self.lead)) + timedelta(minutes=1)
        self._tick(now=when)
        before = alert.call_count
        self._tick(now=when + timedelta(hours=1))
        self.assertEqual(alert.call_count, before)

    # -- guards ------------------------------------------------------------

    @patch('bot.customer_emails._send', return_value=True)
    def test_a_parked_lead_is_never_chased(self, _send):
        from bot.post_visit import apply_submission
        report = self._report()
        apply_submission(report, outcome='went_ahead', expectation='unknown',
                         email='tendai@example.com')
        self.lead.refresh_from_db()
        self.lead.mark_parked()
        report.refresh_from_db()

        stats = self._tick(now=report.next_action_at)
        self.assertEqual(stats['asks'], 0)
        self.assertEqual(stats['skipped'], 1)
        self.assertFalse(_send.called)

    @patch('bot.customer_emails._send', return_value=True)
    def test_a_handed_off_lead_is_never_chased(self, _send):
        from bot.post_visit import apply_submission
        report = self._report()
        apply_submission(report, outcome='went_ahead', expectation='unknown',
                         email='tendai@example.com')
        self.lead.refresh_from_db()
        self.lead.mark_handed_off()
        report.refresh_from_db()
        self.assertEqual(self._tick(now=report.next_action_at)['asks'], 0)
        self.assertFalse(_send.called)

    @patch('bot.customer_emails._send', return_value=True)
    def test_a_lead_whose_job_is_booked_is_never_chased(self, _send):
        from bot.post_visit import apply_submission
        report = self._report()
        apply_submission(report, outcome='went_ahead', expectation='unknown',
                         email='tendai@example.com')
        self.lead.refresh_from_db()
        self.lead.job_scheduled_datetime = timezone.now() + timedelta(days=3)
        self.lead.job_status = 'scheduled'
        self.lead.save()
        report.refresh_from_db()
        self.assertEqual(self._tick(now=report.next_action_at)['asks'], 0)
        self.assertFalse(_send.called)

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_a_future_visit_is_left_alone(self, _send, alert):
        self.lead.scheduled_datetime = timezone.now() + timedelta(days=2)
        self.lead.save()
        stats = self._tick()
        self.assertEqual(stats, {'form_emails': 0, 'asks': 0, 'confirmations': 0,
                                 'cold': 0, 'no_email': 0, 'skipped': 0})

    @patch('bot.plumber_notifications.send_plumber_notification_email', return_value=True)
    @patch('bot.customer_emails._send', return_value=True)
    def test_dry_run_writes_nothing(self, _send, alert):
        from bot.post_visit import visit_end
        self._tick(now=visit_end(self.lead) + timedelta(days=2), dry_run=True)
        self.assertFalse(_send.called)
        self.assertFalse(alert.called)
        report = self._report()
        self.assertIsNone(report.fallback_email_sent_at)
        self.assertEqual(report.ask_count, 0)

    def test_the_command_runs(self):
        out = StringIO()
        call_command('send_post_visit_followups', '--dry-run', stdout=out)
        self.assertIn('Post-visit', out.getvalue())


class PostVisitDateParsingTests(TestCase):
    """extract_expected_date: a real date switches branches, a vague one must not."""

    def _parse(self, text):
        from bot.post_visit import extract_expected_date
        return extract_expected_date(text, today=timezone.localdate())

    def test_explicit_dates_are_read(self):
        today = timezone.localdate()
        target = today + timedelta(days=20)
        for text in (target.strftime('%d/%m/%Y'),
                     target.strftime('%Y-%m-%d'),
                     target.strftime('%d %B'),
                     target.strftime('%B %d')):
            self.assertEqual(self._parse('we can do ' + text), target, text)

    def test_tomorrow_and_named_days_are_dates(self):
        today = timezone.localdate()
        self.assertEqual(self._parse('tomorrow works'), today + timedelta(days=1))
        self.assertIsNotNone(self._parse('next monday please'))

    def test_a_vague_timeframe_is_not_a_date(self):
        """This is the whole point of Case B - a rough answer must keep chasing,
        never trigger a confirmation for a day the lead never named."""
        for text in ('in a few weeks', 'sometime next month', 'not sure yet',
                     'asap', 'when we have the money', ''):
            self.assertIsNone(self._parse(text), text)

    def test_a_past_date_does_not_switch_the_branch(self):
        from bot.post_visit import record_lead_expected_date
        lead = make_lead(8400, status='confirmed',
                         scheduled_datetime=timezone.now() - timedelta(hours=3))
        from bot.post_visit import ensure_report
        ensure_report(lead)
        lead.refresh_from_db()
        self.assertFalse(record_lead_expected_date(
            lead, timezone.localdate() - timedelta(days=2)))

    def test_a_lead_with_no_report_is_untouched(self):
        from bot.post_visit import note_inbound_reply
        lead = make_lead(8401)
        self.assertFalse(note_inbound_reply(lead, 'tomorrow works'))


class PostVisitCopyTests(TestCase):
    """What the lead reads. The house rules: no emojis, no dash punctuation, and
    never a re-pitch of the visit that has already happened."""

    def setUp(self):
        self.lead = make_lead(8500, customer_name='Chipo',
                              customer_email='chipo@example.com',
                              project_type='bathroom_renovation')

    def _bodies(self):
        from bot.customer_emails import (build_post_visit_ask_email,
                                         build_post_visit_confirmation_email)
        out = [build_post_visit_ask_email(self.lead, n) for n in (1, 2, 3)]
        out.append(build_post_visit_confirmation_email(
            self.lead, timezone.localdate() + timedelta(days=5)))
        return out

    def test_no_emojis_anywhere(self):
        for subject, html in self._bodies():
            self.assertIsNone(_EMOJI_RE.search(subject), subject)
            self.assertIsNone(_EMOJI_RE.search(html), subject)

    def test_no_dash_punctuation(self):
        import re as _re
        text = _re.compile(r'<[^>]+>')
        for subject, html in self._bodies():
            visible = text.sub(' ', html)
            self.assertNotIn('—', visible, subject)
            self.assertNotIn('–', visible, subject)
            # A clause dash - like this one - is the shape being banned; hyphens
            # inside words (on-site, call-out) are fine.
            self.assertIsNone(_re.search(r'\s-\s', visible), subject)

    def test_the_visit_is_never_re_pitched(self):
        """The plumber has already been. Offering the visit again, free or
        otherwise, is the repeat-pitch bug in a new channel."""
        for subject, html in self._bodies():
            low = html.lower()
            self.assertNotIn('free', low, subject)
            self.assertNotIn('site visit', low, subject)
            self.assertNotIn('no cost', low, subject)

    def test_the_confirmation_names_the_lead_s_own_date(self):
        from bot.customer_emails import build_post_visit_confirmation_email
        target = timezone.localdate() + timedelta(days=5)
        subject, html = build_post_visit_confirmation_email(self.lead, target)
        self.assertIn(target.strftime('%d %B'), subject + html)
        self.assertNotIn('None', html)


class QuoteSendChannelTests(StaffClientTestCase):
    """The two quote-send buttons are independent, and neither touches the
    follow-up channel."""

    def setUp(self):
        super().setUp()
        self.lead = make_lead(8600, customer_name='Farai', status='confirmed',
                              customer_email='farai@example.com',
                              scheduled_datetime=timezone.now() - timedelta(hours=3))
        self.quotation = Quotation.objects.create(appointment=self.lead, labor_cost=Decimal('120'))

    @patch('bot.customer_emails.send_quotation_email_to_customer', return_value=True)
    @patch('bot.views.quotations.build_quotation_pdf_file')
    def test_email_send_marks_only_the_email_flag(self, build_pdf, send):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(b'%PDF-1.4 test')
            build_pdf.return_value = tmp.name
        self.client.post(reverse('send_quotation_email', args=[self.quotation.pk]))
        self.quotation.refresh_from_db()
        self.assertTrue(self.quotation.sent_via_email)
        self.assertFalse(self.quotation.sent_via_whatsapp)
        self.assertTrue(send.called)

    def test_email_send_refuses_without_an_address(self):
        self.lead.customer_email = None
        self.lead.save()
        self.client.post(reverse('send_quotation_email', args=[self.quotation.pk]))
        self.quotation.refresh_from_db()
        self.assertFalse(self.quotation.sent_via_email)

    @patch('bot.customer_emails._send', return_value=True)
    def test_a_whatsapp_only_quote_send_does_not_disable_email_followups(self, _send):
        """Send channel and follow-up channel are decoupled."""
        from bot.post_visit import apply_submission, ensure_report, run_post_visit_tick
        self.quotation.sent_via_whatsapp = True
        self.quotation.save()
        report = ensure_report(self.lead)
        apply_submission(report, outcome='went_ahead', expectation='unknown',
                         email='farai@example.com')
        report.refresh_from_db()
        self.assertEqual(run_post_visit_tick(now=report.next_action_at)['asks'], 1)


class TemplateCommentTests(TestCase):
    """Django's {# #} is SINGLE-LINE ONLY.

    From the Django docs: "A {# #} comment cannot span multiple lines. This
    limitation improves template parsing performance." One that spans lines is
    never stripped — it renders to the page as literal text, in front of
    whoever is looking at it. Two shipped that way and showed up on the
    appointment detail screen and the dashboard (prod 2026-09-03); one of them
    sat INSIDE a <button> tag, so it became bogus attributes rather than just
    stray prose.

    Multi-line commentary belongs in {% comment %}...{% endcomment %}.
    """

    def test_no_multiline_hash_comments_in_templates(self):
        import pathlib
        from django.conf import settings

        offenders = []
        root = pathlib.Path(settings.BASE_DIR) / 'bot' / 'templates'
        for path in sorted(root.rglob('*.html')):
            src = path.read_text(encoding='utf-8')
            for lineno, line in enumerate(src.splitlines(), 1):
                for match in re.finditer(r'\{#', line):
                    if '#}' not in line[match.end():]:
                        offenders.append(
                            f'{path.relative_to(root).as_posix()}:{lineno} '
                            f'-> {line.strip()[:70]}')

        self.assertEqual(offenders, [], (
            'These {# #} comments span multiple lines, so Django renders them '
            'as visible text. Use {% comment %}...{% endcomment %} instead:\n  '
            + '\n  '.join(offenders)))

    def test_the_detail_page_renders_no_raw_template_syntax(self):
        """End to end: whatever the page contains, none of it reaches the
        browser as template source."""
        user = get_user_model().objects.create_user(
            username='tmpl-tester', password='pass12345', is_staff=True)
        TenantMembership.objects.create(
            user=user, tenant=Tenant.objects.get(slug='homebase'), role='staff')
        self.client.force_login(user)

        lead = make_lead(9300, customer_name='Comment Check', status='confirmed',
                         scheduled_datetime=timezone.now() - timedelta(hours=3))
        for url in (reverse('appointment_detail', args=[lead.pk]),
                    reverse('dashboard')):
            body = self.client.get(url).content.decode()
            self.assertNotIn('{#', body, url)
            self.assertNotIn('#}', body, url)
            self.assertNotIn('{%', body, url)


# ======================================================================
# Quotes: branding, history and templates
# ======================================================================

def _png(name='logo.png'):
    """A tiny real PNG, so uploads exercise the same path a browser takes."""
    import base64
    raw = base64.b64decode(
        b'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM'
        b'IQAAAABJRU5ErkJggg==')
    return SimpleUploadedFile(name, raw, content_type='image/png')


class TenantLogoTests(TestCase):
    """One logo per business, one reader, and a fallback that never borrows."""

    def setUp(self):
        self.tenant = Tenant.objects.get(slug='homebase')
        self.profile, _ = TenantProfile.objects.get_or_create(tenant=self.tenant)

    # -- validation --------------------------------------------------------

    def test_accepted_formats(self):
        from bot.branding import validate_logo
        for name, ctype in (('a.png', 'image/png'), ('a.jpg', 'image/jpeg'),
                            ('a.jpeg', 'image/jpeg'), ('a.svg', 'image/svg+xml')):
            validate_logo(SimpleUploadedFile(name, b'x', content_type=ctype))

    def test_other_formats_are_refused(self):
        from bot.branding import LogoRejected, validate_logo
        for name, ctype in (('a.pdf', 'application/pdf'), ('a.gif', 'image/gif'),
                            ('a.exe', 'application/octet-stream'),
                            ('a.png', 'application/pdf')):
            with self.assertRaises(LogoRejected, msg=name):
                validate_logo(SimpleUploadedFile(name, b'x', content_type=ctype))

    def test_oversized_files_are_refused(self):
        from bot.branding import MAX_LOGO_BYTES, LogoRejected, validate_logo
        big = SimpleUploadedFile('big.png', b'x' * (MAX_LOGO_BYTES + 1),
                                 content_type='image/png')
        with self.assertRaises(LogoRejected) as caught:
            validate_logo(big)
        # The message has to say what to do, not just that something failed.
        self.assertIn('2 MB', str(caught.exception))

    def test_the_limit_is_two_megabytes(self):
        from bot.branding import MAX_LOGO_BYTES, RECOMMENDED_LOGO_WIDTH
        self.assertEqual(MAX_LOGO_BYTES, 2 * 1024 * 1024)
        self.assertEqual(RECOMMENDED_LOGO_WIDTH, 400)

    # -- storage and reading back -----------------------------------------

    def test_a_saved_logo_is_readable_every_way(self):
        from bot import branding
        branding.save_logo(self.tenant, _png())
        self.assertTrue(branding.has_logo(self.tenant))
        self.assertTrue(branding.logo_url(self.tenant))
        self.assertTrue(branding.logo_data_uri(self.tenant).startswith('data:image/png;base64,'))
        raw, ctype = branding.logo_bytes(self.tenant)
        self.assertTrue(raw)
        self.assertEqual(ctype, 'image/png')

    def test_no_logo_falls_back_to_the_business_name(self):
        from bot import branding
        ctx = branding.branding_context(self.tenant)
        self.assertFalse(ctx['has_logo'])
        self.assertEqual(ctx['logo_url'], '')
        self.assertEqual(ctx['brand_name'], self.tenant.name)

    def test_the_letterhead_trading_name_wins_as_the_fallback(self):
        from bot import branding
        self.profile.letterhead = {'business_name': 'Homebase Trading Co'}
        self.profile.save(update_fields=['letterhead'])
        self.assertEqual(branding.brand_name(self.tenant), 'Homebase Trading Co')

    def test_a_logo_is_never_borrowed_from_another_tenant(self):
        """The whole point: absent means fall back to your own name, never to
        somebody else's mark."""
        from bot import branding
        other = Tenant.objects.create(name='Acme Plumbing', slug='acme-logo')
        TenantProfile.objects.create(tenant=other)
        branding.save_logo(self.tenant, _png())

        self.assertTrue(branding.has_logo(self.tenant))
        self.assertFalse(branding.has_logo(other))
        self.assertEqual(branding.logo_url(other), '')
        self.assertEqual(branding.logo_data_uri(other), '')
        self.assertEqual(branding.brand_name(other), 'Acme Plumbing')

    def test_clearing_a_logo_returns_to_the_name(self):
        from bot import branding
        branding.save_logo(self.tenant, _png())
        branding.clear_logo(self.tenant)
        self.assertFalse(branding.has_logo(self.tenant))
        self.assertEqual(branding.branding_context(self.tenant)['brand_name'],
                         self.tenant.name)

    def test_no_tenant_yields_nothing_rather_than_a_default(self):
        from bot import branding
        ctx = branding.branding_context(None)
        self.assertEqual(ctx, {'logo_url': '', 'logo_data_uri': '',
                               'brand_name': '', 'has_logo': False})


class LogoSurfaceTests(StaffClientTestCase):
    """The four places the spec says the logo appears."""

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.get(slug='homebase')
        self.profile, _ = TenantProfile.objects.get_or_create(tenant=self.tenant)
        from bot import branding
        branding.save_logo(self.tenant, _png())
        self.lead = make_lead(9400, customer_name='Brand Check',
                              customer_email='brand@example.com')

    def test_dashboard_shows_it(self):
        response = self.client.get(reverse('dashboard'))
        self.assertTrue(response.context['has_logo'])
        self.assertIn('brand-mark__img', response.content.decode())

    def test_quote_screen_shows_it(self):
        response = self.client.get(reverse('create_quotation', args=[self.lead.pk]))
        self.assertTrue(response.context['has_logo'])

    def test_customer_email_carries_it_inline(self):
        """Inlined, not linked: mail clients block remote images."""
        from bot.customer_emails import build_post_visit_ask_email
        _, html = build_post_visit_ask_email(self.lead, 1)
        self.assertIn('data:image/png;base64,', html)

    def test_the_email_falls_back_to_the_name(self):
        from bot import branding
        from bot.customer_emails import build_post_visit_ask_email
        branding.clear_logo(self.tenant)
        _, html = build_post_visit_ask_email(self.lead, 1)
        self.assertNotIn('data:image', html)
        self.assertIn(self.tenant.name, html)

    def test_booking_form_shows_it(self):
        """The intake wizard - the 'booking form' of the spec."""
        from bot.models import TenantIntake
        intake = TenantIntake.objects.create(tenant=self.tenant)
        self.client.logout()
        body = self.client.get(
            reverse('intake_form', kwargs={'token': intake.token})).content.decode()
        self.assertIn('intake-brand', body)
        self.assertIn('data:image/png;base64,', body)

    def test_the_quote_pdf_uses_this_tenant_and_not_a_static_file(self):
        from bot.views.quotations import build_quotation_pdf_file
        quotation = Quotation.objects.create(appointment=self.lead, labor_cost=Decimal('50'))
        path = build_quotation_pdf_file(quotation)
        try:
            self.assertTrue(os.path.getsize(path) > 500)
        finally:
            os.remove(path)

    def test_a_tenant_with_no_logo_still_renders_a_pdf(self):
        """Absent means omit, not crash."""
        from bot import branding
        from bot.views.quotations import build_quotation_pdf_file
        branding.clear_logo(self.tenant)
        quotation = Quotation.objects.create(appointment=self.lead, labor_cost=Decimal('50'))
        path = build_quotation_pdf_file(quotation)
        try:
            self.assertTrue(os.path.getsize(path) > 500)
        finally:
            os.remove(path)


class LogoUploadUITests(StaffClientTestCase):
    """Both editors: the client's own, and the operator on their behalf."""

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.get(slug='homebase')
        TenantProfile.objects.get_or_create(tenant=self.tenant)

    def test_client_uploads_their_own_from_the_profile_page(self):
        from bot import branding
        self.client.post(reverse('profile'), {'logo_submit': '1', 'logo': _png()})
        self.assertTrue(branding.has_logo(self.tenant))

    def test_a_bad_upload_is_refused_with_a_reason(self):
        from bot import branding
        response = self.client.post(reverse('profile'), {
            'logo_submit': '1',
            'logo': SimpleUploadedFile('x.pdf', b'x', content_type='application/pdf'),
        }, follow=True)
        self.assertFalse(branding.has_logo(self.tenant))
        self.assertIn('PNG, JPG or SVG', response.content.decode())

    def test_client_can_remove_their_logo(self):
        from bot import branding
        branding.save_logo(self.tenant, _png())
        self.client.post(reverse('profile'), {'logo_submit': '1', 'remove_logo': '1'})
        self.assertFalse(branding.has_logo(self.tenant))

    def test_operator_uploads_on_behalf_of_a_client(self):
        from bot import branding
        other = Tenant.objects.create(name='Acme Plumbing', slug='acme-upload')
        TenantProfile.objects.create(tenant=other)
        owner = get_user_model().objects.create_superuser(
            username='adminJ', password='pass12345', email='jones86xi@gmail.com')
        self.client.force_login(owner)

        self.client.post(
            reverse('platform_tenant_config_edit', kwargs={'slug': other.slug}),
            {'logo_submit': '1', 'logo': _png()})
        self.assertTrue(branding.has_logo(other))
        # ...and only for that client.
        self.assertFalse(branding.has_logo(self.tenant))


class MyQuotesHistoryTests(StaffClientTestCase):
    """The quotes list: scoped, searchable, paged, and honest when empty."""

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.get(slug='homebase')
        self.other = Tenant.objects.create(name='Acme Plumbing', slug='acme-quotes')

        self.lead = make_lead(9500, customer_name='Rudo Moyo',
                              customer_email='rudo@example.com')
        self.quote = Quotation.objects.create(appointment=self.lead, labor_cost=Decimal('120'))

        self.foreign_lead = make_lead(9501, tenant=self.other, customer_name='Acme Client')
        self.foreign_quote = Quotation.objects.create(
            appointment=self.foreign_lead, labor_cost=Decimal('999'))

    def _list(self, **params):
        return self.client.get(reverse('quotations_list'), params)

    # -- scoping (the leak this fixed) -------------------------------------

    def test_a_client_sees_only_their_own_quotes(self):
        """The view had no get_queryset at all, so ListView fell back to
        Quotation.objects.all() and every client saw every other client's
        quotes, lead names and figures."""
        response = self._list()
        rows = list(response.context['quotations'])
        self.assertIn(self.quote, rows)
        self.assertNotIn(self.foreign_quote, rows)
        body = response.content.decode()
        self.assertIn('Rudo Moyo', body)
        self.assertNotIn('Acme Client', body)
        # The figure, not the bare digits: '999' also appears in the pill CSS.
        self.assertNotIn('US$999', body)
        self.assertNotIn(self.foreign_quote.quotation_number, body)

    def test_scoping_survives_a_search(self):
        """Search must narrow within the tenant, never widen past it."""
        rows = list(self._list(q='Acme').context['quotations'])
        self.assertEqual(rows, [])

    def _as_owner(self, tenant_slug=None):
        """Log in as the platform operator, optionally lensed into a tenant."""
        from bot.middleware import TENANT_SESSION_KEY
        owner = get_user_model().objects.create_superuser(
            username='adminJ', password='pass12345', email='jones86xi@gmail.com')
        self.client.force_login(owner)
        if tenant_slug:
            session = self.client.session
            session[TENANT_SESSION_KEY] = tenant_slug
            session.save()
        return owner

    def test_the_operator_lensed_into_a_client_sees_only_that_client(self):
        """The tenant switcher is impersonation - viewing Barmak shows Barmak's
        world. Letting the operator bypass it put Homebase's quote in Barmak's
        section (prod 2026-09-03)."""
        self._as_owner(tenant_slug=self.other.slug)
        response = self._list()
        rows = list(response.context['quotations'])
        self.assertIn(self.foreign_quote, rows)
        self.assertNotIn(self.quote, rows)
        self.assertFalse(response.context['sees_all_tenants'])
        self.assertNotIn('Rudo Moyo', response.content.decode())

    def test_the_operator_sees_across_clients_only_when_they_ask(self):
        self._as_owner(tenant_slug=self.other.slug)
        response = self._list(all='1')
        rows = list(response.context['quotations'])
        self.assertIn(self.quote, rows)
        self.assertIn(self.foreign_quote, rows)
        self.assertTrue(response.context['sees_all_tenants'])

    def test_a_client_cannot_widen_the_view_with_the_flag(self):
        """?all=1 is an operator control, not a query parameter anyone can set."""
        response = self._list(all='1')
        self.assertFalse(response.context['sees_all_tenants'])
        self.assertNotIn(self.foreign_quote, list(response.context['quotations']))

    def test_a_per_quote_action_never_widens_past_the_workspace(self):
        """Even for the operator: ?all=1 on a download must not reach into
        another workspace than the one they are lensed into."""
        self._as_owner(tenant_slug=self.other.slug)
        self.assertEqual(
            self.client.get(
                reverse('download_quotation_pdf', args=[self.quote.pk])).status_code,
            404)

    # -- the row -----------------------------------------------------------

    def test_the_row_carries_every_column_the_spec_asks_for(self):
        self.quote.sent_via_email = True
        self.quote.status = 'sent'
        self.quote.save()
        body = self._list().content.decode()
        self.assertIn(self.quote.quotation_number, body)   # quote number
        self.assertIn('Rudo Moyo', body)                   # lead name
        self.assertIn(str(self.quote.total_amount), body)  # amount
        self.assertIn('Sent', body)                        # status
        self.assertIn('Email', body)                       # channel sent

    def test_the_channel_cell_reports_both_when_both_were_used(self):
        self.quote.sent_via_email = True
        self.quote.sent_via_whatsapp = True
        self.quote.save()
        self.assertEqual(self.quote.sent_channel_label(), 'Email + WhatsApp')
        self.assertIn('Email + WhatsApp', self._list().content.decode())

    def test_an_unsent_quote_says_so(self):
        self.assertEqual(self.quote.sent_channel_label(), 'Not sent')
        self.assertIn('Not sent', self._list().content.decode())

    def test_a_nameless_lead_still_shows_something_findable(self):
        bare = make_lead(9502, customer_name='')
        quote = Quotation.objects.create(appointment=bare)
        self.assertTrue(quote.lead_name())
        self.assertNotIn('None', self._list().content.decode())

    # -- sort, search, filter, paging -------------------------------------

    def test_newest_first(self):
        newer = Quotation.objects.create(appointment=self.lead, labor_cost=Decimal('1'))
        rows = list(self._list().context['quotations'])
        self.assertEqual(rows[0], newer)

    def test_search_by_lead_name_and_by_quote_number(self):
        self.assertIn(self.quote, list(self._list(q='Rudo').context['quotations']))
        self.assertIn(self.quote,
                      list(self._list(q=self.quote.quotation_number).context['quotations']))
        self.assertEqual(list(self._list(q='nobody-by-that-name').context['quotations']), [])

    def test_filter_by_status(self):
        self.quote.status = 'accepted'
        self.quote.save()
        self.assertIn(self.quote, list(self._list(status='accepted').context['quotations']))
        self.assertEqual(list(self._list(status='draft').context['quotations']), [])

    def test_pagination_keeps_the_search(self):
        for i in range(30):
            Quotation.objects.create(appointment=self.lead, labor_cost=Decimal(i))
        body = self._list(q='Rudo', page=1).content.decode()
        self.assertIn('q=Rudo', body)
        self.assertEqual(len(self._list(q='Rudo').context['quotations']), 25)

    # -- empty states ------------------------------------------------------

    def test_the_two_empty_states_say_different_things(self):
        Quotation.objects.all().delete()
        fresh = self._list().content.decode()
        self.assertIn('No quotes yet', fresh)

        searched = self._list(q='zzz').content.decode()
        self.assertIn('No quotes match that search', searched)
        self.assertNotIn('No quotes yet', searched)

    # -- row actions -------------------------------------------------------

    def test_download_returns_a_pdf(self):
        response = self.client.get(reverse('download_quotation_pdf', args=[self.quote.pk]))
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertIn('attachment', response['Content-Disposition'])
        self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF'))

    def test_download_is_tenant_scoped(self):
        self.assertEqual(
            self.client.get(
                reverse('download_quotation_pdf', args=[self.foreign_quote.pk])).status_code,
            404)

    def test_duplicate_from_the_list(self):
        before = Quotation.objects.filter(tenant=self.tenant).count()
        self.client.post(reverse('duplicate_quotation', args=[self.quote.pk]))
        self.assertEqual(Quotation.objects.filter(tenant=self.tenant).count(), before + 1)

    def test_status_vocabulary_matches_the_scheduler(self):
        """'cold' replaced 'rejected' so the quote can say what the post-visit
        scheduler says when it gives up on a lead."""
        values = [v for v, _ in Quotation.STATUS_CHOICES]
        self.assertEqual(values, ['draft', 'sent', 'accepted', 'cold'])
        self.assertNotIn('rejected', values)


class GlobalTemplateTests(StaffClientTestCase):
    """Client templates, global templates, and who may change which."""

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.get(slug='homebase')
        self.other = Tenant.objects.create(name='Acme Plumbing', slug='acme-tmpl')

        self.mine = QuotationTemplate.objects.create(
            tenant=self.tenant, name='My Bathroom Template')
        self.theirs = QuotationTemplate.objects.create(
            tenant=self.other, name='Acme Private Template')
        self.shared = QuotationTemplate.objects.create(
            tenant=self.other, name='Platform Standard Bathroom', is_global=True)

    def _owner(self):
        owner = get_user_model().objects.create_superuser(
            username='adminJ', password='pass12345', email='jones86xi@gmail.com')
        self.client.force_login(owner)
        return owner

    # -- visibility --------------------------------------------------------

    def test_a_client_sees_their_own_plus_global(self):
        rows = list(self.client.get(
            reverse('quotation_templates_list')).context['templates'])
        self.assertIn(self.mine, rows)
        self.assertIn(self.shared, rows)
        self.assertNotIn(self.theirs, rows)

    def test_the_global_row_is_labelled(self):
        body = self.client.get(reverse('quotation_templates_list')).content.decode()
        self.assertIn('Platform Standard Bathroom', body)
        self.assertIn('Global', body)

    def test_the_counts_describe_the_list_underneath_them(self):
        """Counting by tenant alone excluded every global template, so the
        totals disagreed with the rows."""
        context = self.client.get(reverse('quotation_templates_list')).context
        self.assertEqual(context['total_templates'], 2)
        self.assertEqual(context['global_templates'], 1)

    # -- permission --------------------------------------------------------

    def test_a_client_may_edit_their_own(self):
        self.assertTrue(self.mine.editable_by(self.user, self.tenant))
        self.assertEqual(
            self.client.get(reverse('edit_quotation_template', args=[self.mine.pk])).status_code,
            200)

    def test_a_global_template_is_read_only_to_a_client(self):
        self.assertFalse(self.shared.editable_by(self.user, self.tenant))
        self.assertEqual(
            self.client.get(reverse('edit_quotation_template', args=[self.shared.pk])).status_code,
            404)

    def test_a_client_cannot_delete_a_global_template(self):
        self.client.post(reverse('delete_template', args=[self.shared.pk]))
        self.assertTrue(QuotationTemplate.objects.filter(pk=self.shared.pk).exists())

    def test_a_client_cannot_touch_another_clients_template(self):
        """delete/duplicate/toggle fetched by bare pk with no scoping at all,
        so any staff user could delete another tenant's template."""
        for name, url in (
            ('edit', reverse('edit_quotation_template', args=[self.theirs.pk])),
            ('delete', reverse('delete_template', args=[self.theirs.pk])),
            ('duplicate', reverse('duplicate_template', args=[self.theirs.pk])),
        ):
            with self.subTest(action=name):
                self.assertEqual(self.client.get(url).status_code, 404)
        self.client.post(reverse('delete_template', args=[self.theirs.pk]))
        self.assertTrue(QuotationTemplate.objects.filter(pk=self.theirs.pk).exists())

    def test_toggling_another_clients_template_is_refused(self):
        was = self.theirs.is_active
        self.client.post(reverse('toggle_template_status', args=[self.theirs.pk]))
        self.theirs.refresh_from_db()
        self.assertEqual(self.theirs.is_active, was)

    # -- duplicate is how a client takes a global one -----------------------

    def test_duplicating_a_global_lands_an_editable_copy_in_my_workspace(self):
        self.client.post(reverse('duplicate_template', args=[self.shared.pk]),
                         {'new_name': 'My Copy'})
        copy = QuotationTemplate.objects.get(name='My Copy')
        self.assertEqual(copy.tenant, self.tenant)
        self.assertFalse(copy.is_global)
        self.assertTrue(copy.editable_by(self.user, self.tenant))

    def test_the_original_global_is_untouched_by_the_copy(self):
        self.client.post(reverse('duplicate_template', args=[self.shared.pk]),
                         {'new_name': 'My Copy'})
        self.shared.refresh_from_db()
        self.assertTrue(self.shared.is_global)
        self.assertEqual(self.shared.tenant, self.other)

    # -- the operator ------------------------------------------------------

    def test_the_operator_lensed_into_a_client_sees_only_that_client(self):
        """Same lens rule as the quotes list: an operator viewing Barmak sees
        Barmak's templates plus the global set, not every client's private
        ones."""
        from bot.middleware import TENANT_SESSION_KEY
        self._owner()
        session = self.client.session
        session[TENANT_SESSION_KEY] = self.other.slug
        session.save()

        rows = list(self.client.get(
            reverse('quotation_templates_list')).context['templates'])
        self.assertIn(self.theirs, rows)
        self.assertIn(self.shared, rows)
        self.assertNotIn(self.mine, rows)

    def test_the_operator_sees_every_template_only_when_they_ask(self):
        self._owner()
        rows = list(self.client.get(
            reverse('quotation_templates_list'), {'all': '1'}).context['templates'])
        for template in (self.mine, self.theirs, self.shared):
            self.assertIn(template, rows)

    def test_a_client_cannot_widen_the_template_view(self):
        rows = list(self.client.get(
            reverse('quotation_templates_list'), {'all': '1'}).context['templates'])
        self.assertNotIn(self.theirs, rows)

    def test_the_operator_may_edit_both_kinds(self):
        owner = self._owner()
        self.assertTrue(self.shared.editable_by(owner, self.tenant))
        self.assertTrue(self.theirs.editable_by(owner, self.tenant))

    def test_only_the_operator_is_offered_the_global_checkbox(self):
        client_view = self.client.get(reverse('create_quotation_template'))
        self.assertFalse(client_view.context['can_create_global'])
        self.assertNotIn('name="is_global"', client_view.content.decode())

        self._owner()
        owner_view = self.client.get(reverse('create_quotation_template'))
        self.assertTrue(owner_view.context['can_create_global'])
        self.assertIn('name="is_global"', owner_view.content.decode())

    def test_a_client_posting_is_global_is_not_honoured(self):
        """Hiding a checkbox is presentation, not permission."""
        self.client.post(reverse('create_quotation_template'), {
            'name': 'Sneaky Global', 'project_type': 'general',
            'default_labor_cost': '0', 'default_transport_cost': '0',
            'is_active': 'on', 'is_global': 'on',
            'items-TOTAL_FORMS': '0', 'items-INITIAL_FORMS': '0',
            'items-MIN_NUM_FORMS': '0', 'items-MAX_NUM_FORMS': '1000',
        })
        created = QuotationTemplate.objects.filter(name='Sneaky Global').first()
        if created is not None:
            self.assertFalse(created.is_global)


class OneQuoteLayoutTests(StaffClientTestCase):
    """Every flat quote screen renders from ONE template, per tenant.

    Create-from-lead, standalone-new and edit used to be three separate
    750-950 line templates that had drifted: different headers, two different
    preview blocks, and no preview at all on edit. The same quote looked like
    three products depending on how you reached it.
    """

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.get(slug='homebase')
        self.profile, _ = TenantProfile.objects.get_or_create(tenant=self.tenant)
        self.lead = make_lead(9600, customer_name='Layout Lead',
                              project_type='bathroom_renovation')
        self.quote = Quotation.objects.create(appointment=self.lead,
                                              labor_cost=Decimal('80'))

    def _editors(self):
        return {
            'standalone': reverse('standalone_quotation'),
            'create': reverse('create_quotation', args=[self.lead.pk]),
            'edit': reverse('edit_quotation', args=[self.quote.pk]),
        }

    def test_all_three_editors_use_the_one_template(self):
        for name, url in self._editors().items():
            with self.subTest(screen=name):
                response = self.client.get(url)
                self.assertEqual(response.status_code, 200)
                self.assertIn('bot/pages/quote_flat_form.html',
                              [t.name for t in response.templates])

    def test_each_editor_knows_its_own_mode(self):
        expected = {'standalone': 'new', 'create': 'create', 'edit': 'edit'}
        for name, url in self._editors().items():
            with self.subTest(screen=name):
                self.assertEqual(
                    self.client.get(url).context['quote_mode'], expected[name])

    def test_every_editor_carries_the_document_preview(self):
        """Edit had no preview at all, so a plumber could not see the document
        they were changing."""
        for name, url in self._editors().items():
            with self.subTest(screen=name):
                body = self.client.get(url).content.decode()
                self.assertIn('id="previewSection"', body)
                self.assertIn('id="previewItems"', body)

    def test_the_editor_and_the_client_copy_share_one_letterhead(self):
        pages = list(self._editors().values()) + [
            reverse('view_quotation', args=[self.quote.pk])]
        for url in pages:
            with self.subTest(url=url):
                self.assertIn('bot/includes/quote_flat_letterhead.html',
                              [t.name for t in self.client.get(url).templates])

    def test_no_hardcoded_homebase_identity_on_any_quote_screen(self):
        """The preview and the client copy printed 'HOMEBASE CONSTRUCTION', a
        Johannesburg address and Homebase's phone and email on EVERY tenant's
        quote."""
        other = Tenant.objects.create(name='Acme Plumbing', slug='acme-layout')
        TenantProfile.objects.create(
            tenant=other, letterhead={'business_name': 'Acme Plumbing'})
        lead = make_lead(9601, tenant=other, customer_name='Acme Lead')
        quote = Quotation.objects.create(appointment=lead)

        user = get_user_model().objects.create_user(
            username='acme-layout-staff', password='pw', is_staff=True)
        TenantMembership.objects.create(user=user, tenant=other, role='staff')
        self.client.force_login(user)

        for url in (reverse('standalone_quotation'),
                    reverse('create_quotation', args=[lead.pk]),
                    reverse('edit_quotation', args=[quote.pk]),
                    reverse('view_quotation', args=[quote.pk])):
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertNotIn('HOMEBASE CONSTRUCTION', body)
                self.assertNotIn('Pritchard', body)
                self.assertNotIn('homebaseplumbers.co.zw', body)
                self.assertIn('Acme Plumbing', body)

    def test_a_tenant_with_no_letterhead_omits_the_lines_rather_than_borrowing(self):
        bare = Tenant.objects.create(name='Bare Plumbing', slug='bare-layout')
        TenantProfile.objects.create(tenant=bare)
        lead = make_lead(9602, tenant=bare)
        user = get_user_model().objects.create_user(
            username='bare-staff', password='pw', is_staff=True)
        TenantMembership.objects.create(user=user, tenant=bare, role='staff')
        self.client.force_login(user)

        body = self.client.get(reverse('create_quotation', args=[lead.pk])).content.decode()
        self.assertIn('Bare Plumbing', body)          # falls back to its own name
        self.assertNotIn('HOMEBASE', body.upper())

    def test_a_sectioned_tenant_still_gets_its_own_sheet(self):
        """The per-tenant switch survives the unification."""
        self.profile.letterhead = {'layout': 'sectioned', 'business_name': 'Homebase'}
        self.profile.save(update_fields=['letterhead'])
        for url in self._editors().values():
            with self.subTest(url=url):
                names = [t.name for t in self.client.get(url).templates]
                self.assertIn('bot/pages/quote_sectioned_form.html', names)
                self.assertNotIn('bot/pages/quote_flat_form.html', names)

    def test_the_superseded_templates_are_gone(self):
        """One layout means one file; a leftover copy is one edit away from
        drifting again."""
        from django.template import TemplateDoesNotExist
        from django.template.loader import get_template
        for name in ('bot/pages/create_quotation.html',
                     'bot/pages/edit_quotation.html',
                     'bot/pages/standalone_quotation.html'):
            with self.subTest(template=name):
                with self.assertRaises(TemplateDoesNotExist):
                    get_template(name)


class QuoteEditorSendTests(StaffClientTestCase):
    """Send from the editor is independent of Save.

    Before this the plumber had to Save, land on the view page and send from
    there — and a quote sent from a screen they had already left is a quote
    nobody checked. The send buttons are now on the editor, never disabled, and
    each one saves first.
    """

    def setUp(self):
        super().setUp()
        self.lead = make_lead(9700, customer_name='Send Lead',
                              customer_email='send@example.com',
                              project_type='bathroom_renovation')
        self.quote = Quotation.objects.create(appointment=self.lead,
                                              labor_cost=Decimal('60'))

    # -- the buttons are there, and never disabled --------------------------

    def test_both_send_buttons_are_on_every_editor(self):
        urls = [reverse('standalone_quotation'),
                reverse('create_quotation', args=[self.lead.pk]),
                reverse('edit_quotation', args=[self.quote.pk])]
        for url in urls:
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertIn('id="sendEmailBtn"', body)
                self.assertIn('id="sendWhatsappBtn"', body)

    def test_the_send_buttons_are_not_gated_on_save(self):
        """A disabled attribute on either would put Save back in front of Send."""
        import re as _re
        body = self.client.get(reverse('create_quotation', args=[self.lead.pk])).content.decode()
        for btn_id in ('sendEmailBtn', 'sendWhatsappBtn'):
            with self.subTest(button=btn_id):
                tag = _re.search(r'<button[^>]*id="' + btn_id + r'"[^>]*>', body).group(0)
                self.assertNotIn('disabled', tag)

    def test_every_send_saves_first(self):
        """persist() is the single step in front of both channels."""
        body = self.client.get(reverse('create_quotation', args=[self.lead.pk])).content.decode()
        self.assertIn('const id = await persist();', body)
        self.assertIn("window.sendQuote = sendQuote;", body)

    # -- the endpoints the editor calls -------------------------------------

    @patch('bot.customer_emails.send_quotation_email_to_customer', return_value=True)
    @patch('bot.views.quotations.build_quotation_pdf_file')
    def test_the_email_endpoint_answers_json(self, build_pdf, send):
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(b'%PDF-1.4 test')
            build_pdf.return_value = tmp.name
        response = self.client.post(
            reverse('send_quotation_email', args=[self.quote.pk]),
            data='{}', content_type='application/json', HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['sent_to'], 'send@example.com')
        self.quote.refresh_from_db()
        self.assertTrue(self.quote.sent_via_email)

    def test_the_email_endpoint_reports_a_missing_address_as_json(self):
        """Not a redirect: the editor is waiting on a fetch, and a redirect
        would look like success."""
        self.lead.customer_email = None
        self.lead.save()
        response = self.client.post(
            reverse('send_quotation_email', args=[self.quote.pk]),
            data='{}', content_type='application/json', HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()['success'])
        self.assertIn('email address', response.json()['error'])

    @patch('bot.customer_emails.send_quotation_email_to_customer', return_value=True)
    @patch('bot.views.quotations.build_quotation_pdf_file')
    def test_the_form_post_still_redirects(self, build_pdf, send):
        """The quotes list and the view page post a plain form and want a
        redirect — one handler, two shapes."""
        import tempfile
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            tmp.write(b'%PDF-1.4 test')
            build_pdf.return_value = tmp.name
        response = self.client.post(reverse('send_quotation_email', args=[self.quote.pk]))
        self.assertEqual(response.status_code, 302)

    def test_sending_is_still_tenant_scoped(self):
        other = Tenant.objects.create(name='Acme Plumbing', slug='acme-send')
        foreign = make_lead(9701, tenant=other, customer_email='x@example.com')
        foreign_quote = Quotation.objects.create(appointment=foreign)
        response = self.client.post(
            reverse('send_quotation_email', args=[foreign_quote.pk]),
            data='{}', content_type='application/json', HTTP_ACCEPT='application/json')
        self.assertEqual(response.status_code, 404)


class QuoteHandoffTests(StaffClientTestCase):
    """Sending from the plumber's OWN apps.

    A wa.me link and a mailto: draft carry text only - neither can carry an
    attachment. So both routes download the PDF first and open the conversation
    beside it, and the server never sees the message that goes out.
    """

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.get(slug='homebase')
        self.profile, _ = TenantProfile.objects.get_or_create(tenant=self.tenant)
        self.lead = make_lead(9800, customer_name='Handoff Lead',
                              customer_email='handoff@example.com',
                              project_type='bathroom_renovation')
        self.quote = Quotation.objects.create(appointment=self.lead)

    def _editor(self):
        return self.client.get(
            reverse('create_quotation', args=[self.lead.pk])).content.decode()

    # -- WhatsApp: the lead's chat, from the tenant's own number ------------

    def test_the_editor_routes_whatsapp_through_the_one_handoff_page(self):
        """ONE WhatsApp behaviour in the app: the editor navigates to the same
        page every other send control links to, rather than keeping a second
        copy of the handoff that could drift from it."""
        body = self._editor()
        self.assertIn("'/quotations/' + id + '/whatsapp/'", body)
        self.assertNotIn('https://wa.me/', body)

    def test_the_digits_are_clean_enough_for_a_wa_me_link(self):
        """wa.me takes digits only: no +, no 'whatsapp:' prefix."""
        response = self.client.get(reverse('create_quotation', args=[self.lead.pk]))
        digits = response.context['lead_wa_digits']
        self.assertTrue(digits.isdigit(), digits)

    def test_a_quote_with_no_lead_offers_no_whatsapp_target(self):
        response = self.client.get(reverse('standalone_quotation'))
        self.assertEqual(response.context['lead_wa_digits'], '')

    def test_the_handoff_page_downloads_the_pdf_and_opens_the_chat(self):
        """A wa.me link carries text only, so the PDF has to arrive separately."""
        body = self.client.get(
            reverse('quotation_whatsapp_handoff', args=[self.quote.pk])).content.decode()
        self.assertIn(reverse('download_quotation_pdf', args=[self.quote.pk]), body)
        self.assertIn('https://wa.me/15550009800', body)
        self.assertIn('Open WhatsApp', body)

    def test_the_handoff_page_says_the_bot_is_not_sending_it(self):
        body = self.client.get(
            reverse('quotation_whatsapp_handoff', args=[self.quote.pk])).content.decode()
        self.assertIn('Nothing is sent until you send it', body)

    def test_no_screen_still_asks_the_bot_to_send_the_quote(self):
        """Every WhatsApp control hands off; none posts to the Cloud API send."""
        pages = [reverse('quotations_list'),
                 reverse('view_quotation', args=[self.quote.pk]),
                 reverse('appointment_detail', args=[self.lead.pk])]
        bot_send = reverse('send_quotation', args=[self.quote.pk])
        for url in pages:
            with self.subTest(url=url):
                body = self.client.get(url).content.decode()
                self.assertNotIn('"' + bot_send + '"', body)
                self.assertIn(reverse('quotation_whatsapp_handoff', args=[self.quote.pk]), body)

    def test_the_handoff_page_is_tenant_scoped(self):
        other = Tenant.objects.create(name='Acme Plumbing', slug='acme-handoff')
        foreign = Quotation.objects.create(appointment=make_lead(9802, tenant=other))
        self.assertEqual(
            self.client.get(
                reverse('quotation_whatsapp_handoff', args=[foreign.pk])).status_code,
            404)

    # -- Email: a choice, with the tenant's default pre-marked -------------

    def test_the_email_button_offers_both_routes(self):
        body = self._editor()
        self.assertIn('id="emailChoiceModal"', body)
        self.assertIn("sendQuote('email', 'platform')", body)
        self.assertIn("sendQuote('email', 'manual')", body)

    def test_the_configured_sender_is_named_on_the_platform_route(self):
        response = self.client.get(reverse('create_quotation', args=[self.lead.pk]))
        sender = response.context['configured_sender']
        self.assertTrue(sender)
        self.assertIn(sender, response.content.decode())

    def test_the_tenants_default_is_the_one_marked(self):
        response = self.client.get(reverse('create_quotation', args=[self.lead.pk]))
        self.assertEqual(response.context['quote_email_mode'], 'platform')

        self.profile.quote_email_mode = 'manual'
        self.profile.save(update_fields=['quote_email_mode'])
        response = self.client.get(reverse('create_quotation', args=[self.lead.pk]))
        self.assertEqual(response.context['quote_email_mode'], 'manual')

    def test_the_default_is_a_default_not_a_restriction(self):
        """Both routes stay on the page whichever way the tenant set it."""
        for mode in ('platform', 'manual'):
            with self.subTest(mode=mode):
                self.profile.quote_email_mode = mode
                self.profile.save(update_fields=['quote_email_mode'])
                body = self._editor()
                self.assertIn("sendQuote('email', 'platform')", body)
                self.assertIn("sendQuote('email', 'manual')", body)

    def test_the_preference_is_set_from_settings(self):
        self.client.post(reverse('profile'), {
            'quote_email_mode_submit': '1', 'quote_email_mode': 'manual'})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.quote_email_mode, 'manual')

    def test_a_nonsense_preference_is_refused(self):
        self.client.post(reverse('profile'), {
            'quote_email_mode_submit': '1', 'quote_email_mode': 'carrier-pigeon'})
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.quote_email_mode, 'platform')

    def test_the_preference_is_per_tenant(self):
        other = Tenant.objects.create(name='Acme Plumbing', slug='acme-mode')
        other_profile = TenantProfile.objects.create(tenant=other, quote_email_mode='manual')
        self.assertEqual(self.profile.quote_email_mode, 'platform')
        self.assertEqual(other_profile.quote_email_mode, 'manual')

    # -- recording a handoff ------------------------------------------------

    def test_marking_sent_records_the_channel(self):
        response = self.client.post(
            reverse('mark_quotation_sent', args=[self.quote.pk]),
            data=json.dumps({'channel': 'whatsapp'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.quote.refresh_from_db()
        self.assertTrue(self.quote.sent_via_whatsapp)
        self.assertFalse(self.quote.sent_via_email)
        self.assertEqual(self.quote.status, 'sent')

    def test_the_two_channels_stay_independent_through_a_handoff(self):
        for channel in ('whatsapp', 'email'):
            self.client.post(reverse('mark_quotation_sent', args=[self.quote.pk]),
                             data=json.dumps({'channel': channel}),
                             content_type='application/json')
        self.quote.refresh_from_db()
        self.assertTrue(self.quote.sent_via_whatsapp)
        self.assertTrue(self.quote.sent_via_email)
        self.assertEqual(self.quote.sent_channel_label(), 'Email + WhatsApp')

    def test_the_note_says_handed_over_not_delivered(self):
        """The server never saw the message, so the record must not claim it
        was delivered."""
        self.client.post(reverse('mark_quotation_sent', args=[self.quote.pk]),
                         data=json.dumps({'channel': 'whatsapp'}),
                         content_type='application/json')
        note = ConversationMessage.objects.filter(appointment=self.lead).last()
        self.assertIn('handed to the plumber to send', note.content)

    def test_an_unknown_channel_is_refused(self):
        response = self.client.post(
            reverse('mark_quotation_sent', args=[self.quote.pk]),
            data=json.dumps({'channel': 'smoke-signal'}), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.quote.refresh_from_db()
        self.assertFalse(self.quote.sent_via_whatsapp)

    def test_marking_sent_is_tenant_scoped(self):
        other = Tenant.objects.create(name='Acme Plumbing', slug='acme-mark')
        foreign = Quotation.objects.create(appointment=make_lead(9801, tenant=other))
        response = self.client.post(
            reverse('mark_quotation_sent', args=[foreign.pk]),
            data=json.dumps({'channel': 'email'}), content_type='application/json')
        self.assertEqual(response.status_code, 404)

    def test_marking_sent_needs_a_post(self):
        self.assertEqual(
            self.client.get(reverse('mark_quotation_sent', args=[self.quote.pk])).status_code,
            405)


class QuoteDocumentParityTests(StaffClientTestCase):
    """The editor's preview and the client copy are the SAME document.

    They had drifted: the view page used a two-column key/value panel and totals
    of "Labor / Materials / Total", while the editor previewed labelled blocks
    and "Material / Labour / Transport / Total". The same quote looked like two
    different papers, and the plumber was checking one and sending the other.
    """

    def setUp(self):
        super().setUp()
        self.lead = make_lead(9900, customer_name='Parity Lead',
                              customer_email='parity@example.com',
                              customer_area='Hatfield',
                              project_type='bathroom_renovation')
        self.quote = Quotation.objects.create(
            appointment=self.lead, labor_cost=Decimal('100'),
            transport_cost=Decimal('25'), notes='Deposit 50%')
        QuotationItem.objects.create(quotation=self.quote, description='Basin mixer',
                                     quantity=2, unit_price=Decimal('40'))

    def _pages(self):
        return {
            'editor': self.client.get(reverse('edit_quotation', args=[self.quote.pk])),
            'view': self.client.get(reverse('view_quotation', args=[self.quote.pk])),
        }

    @staticmethod
    def _body(response):
        """The rendered body, past the stylesheets.

        Class names appear in the <style> block too, and a naive index() over
        the whole page finds those first - which is a test measuring the CSS
        rather than the document.
        """
        html = response.content.decode()
        return html[html.rindex('</style>'):]

    def test_both_render_from_the_one_document_include(self):
        for name, response in self._pages().items():
            with self.subTest(page=name):
                names = [t.name for t in response.templates]
                self.assertIn('bot/includes/quote_flat_document.html', names)
                self.assertIn('bot/includes/quote_flat_letterhead.html', names)

    def test_both_share_the_document_styles(self):
        for name, response in self._pages().items():
            with self.subTest(page=name):
                self.assertIn('bot/includes/quote_flat_document_css.html',
                              [t.name for t in response.templates])

    def test_both_carry_the_same_blocks_in_the_same_order(self):
        for name, response in self._pages().items():
            body = self._body(response)
            with self.subTest(page=name):
                # From the letterhead down, the document is the same sequence on
                # both pages.
                doc = body[body.index('qf-doc__head'):]
                client_at = doc.index('Client Information')
                project_at = doc.index('Project Details')
                total_at = doc.index('Total Amount')
                self.assertLess(client_at, project_at)
                self.assertLess(project_at, total_at)

    def test_both_carry_the_same_totals_rows(self):
        """'Labor Cost / Materials Cost' on one and 'Material / Labour /
        Transport' on the other is how the two drifted."""
        for name, response in self._pages().items():
            body = self._body(response)
            with self.subTest(page=name):
                for row in ('Material cost', 'Labour', 'Transport', 'Total Amount'):
                    self.assertIn(row, body, row)
                self.assertNotIn('Labor Cost', body)
                self.assertNotIn('Materials Cost', body)

    def test_the_client_copy_shows_the_saved_figures(self):
        body = self._body(self._pages()['view'])
        self.assertIn('Basin mixer', body)
        self.assertIn('US$100', body)      # labour
        self.assertIn('US$25', body)       # transport
        self.assertIn('Deposit 50%', body)

    def test_the_editor_leaves_the_containers_for_its_own_js(self):
        body = self._body(self._pages()['editor'])
        for element_id in ('previewClient', 'previewProject', 'previewItems',
                           'previewMaterials', 'previewLabour', 'previewTransport',
                           'previewGrandTotal'):
            with self.subTest(element=element_id):
                self.assertIn('id="' + element_id + '"', body)

    def test_page_chrome_is_kept_off_the_customers_copy(self):
        """Status, created-at and the action buttons are dashboard metadata.
        Inside the document they would print on the customer's copy."""
        body = self._body(self._pages()['view'])
        self.assertLess(body.index('vq-meta'), body.index('qf-doc__head'))
        # The status row sits in a card marked no-print, above the document.
        chrome = body[:body.index('qf-doc__head')]
        self.assertIn('pbq-no-print', chrome)
        self.assertIn('Status', chrome)

    def test_the_closing_line_is_not_printed_twice(self):
        body = self._body(self._pages()['view'])
        self.assertEqual(body.count('Prepared for:'), 1)


class QuotePdfMatchesTheAppTests(StaffClientTestCase):
    """The PDF a customer receives is the document the plumber approved.

    It followed ONE hardcoded flat layout for everybody, so a sectioned tenant's
    customers got a plain list with none of their sections, subtotals, VAT,
    terms or banking - a document that looked nothing like the one on screen. It
    also hardcoded 'US$' and printed the materials_cost column rather than the
    item lines it is built from.

    Markup cannot be shared between an HTML page and a reportlab canvas, so what
    is asserted here is what a customer would actually notice: which layout was
    drawn, and that the FIGURES agree with the screen.
    """

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.get(slug='homebase')
        self.profile, _ = TenantProfile.objects.get_or_create(tenant=self.tenant)
        self.profile.letterhead = {
            'business_name': 'Homebase Plumbers',
            'services_blurb': 'Quality is our qualification',
            'phones': ['+263 77 481 9901'],
            'public_email': 'hello@homebase.example',
        }
        self.profile.save(update_fields=['letterhead'])

        self.lead = make_lead(9950, customer_name='Pdf Client',
                              customer_area='Hatfield',
                              customer_email='pdf@example.com',
                              project_type='bathroom_renovation')
        self.quote = Quotation.objects.create(
            appointment=self.lead, labor_cost=Decimal('100'),
            transport_cost=Decimal('25'), notes='Deposit 50%')
        QuotationItem.objects.create(quotation=self.quote, description='Basin mixer',
                                     quantity=2, unit_price=Decimal('40'))

    @staticmethod
    def _text(quotation):
        """The PDF's drawn strings, via its content stream."""
        import re
        import zlib
        from bot.quote_pdf import build_quotation_pdf

        path = build_quotation_pdf(quotation)
        try:
            with open(path, 'rb') as fh:
                raw = fh.read()
        finally:
            os.remove(path)

        # reportlab writes content streams ASCII85-encoded AND flate-compressed
        # by default, so both layers come off before there is any text to read.
        import base64
        chunks = []
        for stream in re.findall(rb'stream\r?\n(.*?)endstream', raw, re.S):
            body = stream.strip(b'\r\n')
            try:
                body = base64.a85decode(body, adobe=True)
            except Exception:
                pass
            try:
                body = zlib.decompress(body)
            except zlib.error:
                pass
            chunks.append(body)
        blob = b'\n'.join(chunks).decode('latin-1')
        # Text is drawn as (…) Tj / TJ; pull the literals back out.
        return '\n'.join(re.findall(r'\((.*?)\)\s*T[Jj]', blob))

    # -- it is a real PDF ---------------------------------------------------

    def test_it_produces_a_pdf(self):
        from bot.quote_pdf import build_quotation_pdf
        path = build_quotation_pdf(self.quote)
        try:
            with open(path, 'rb') as fh:
                self.assertTrue(fh.read(5).startswith(b'%PDF'))
            self.assertGreater(os.path.getsize(path), 800)
        finally:
            os.remove(path)

    # -- the flat sheet mirrors the flat document ---------------------------

    def test_the_flat_pdf_carries_the_same_blocks_as_the_screen(self):
        text = self._text(self.quote)
        for block in ('CLIENT INFORMATION', 'PROJECT DETAILS',
                      'Material cost', 'Labour', 'Transport', 'Total Amount'):
            self.assertIn(block, text, block)

    def test_the_flat_pdf_carries_the_leads_own_details(self):
        text = self._text(self.quote)
        self.assertIn('Pdf Client', text)
        self.assertIn('Hatfield', text)
        self.assertIn('Basin mixer', text)

    def test_the_figures_match_the_screen(self):
        text = self._text(self.quote)
        self.assertIn('US$80.00', text)      # 2 x 40, the item lines
        self.assertIn('US$100.00', text)     # labour
        self.assertIn('US$25.00', text)      # transport

    def test_the_notes_reach_the_customer(self):
        self.assertIn('Deposit 50%', self._text(self.quote))

    def test_the_letterhead_is_the_tenants_own(self):
        text = self._text(self.quote)
        self.assertIn('Homebase Plumbers', text)
        self.assertIn('Quality is our qualification', text)
        # ...and none of the values that used to be hardcoded for everyone.
        self.assertNotIn('HOMEBASE CONSTRUCTION', text)
        self.assertNotIn('Pritchard', text)

    def test_no_letterhead_omits_the_lines_rather_than_borrowing(self):
        bare = Tenant.objects.create(name='Bare Plumbing', slug='bare-pdf')
        TenantProfile.objects.create(tenant=bare)
        lead = make_lead(9951, tenant=bare, customer_name='Bare Client')
        quote = Quotation.objects.create(appointment=lead)
        text = self._text(quote)
        self.assertIn('Bare Plumbing', text)
        self.assertNotIn('Homebase', text)

    # -- a sectioned tenant gets THEIR sheet ---------------------------------

    def _sectioned_quote(self):
        barmak = Tenant.objects.create(name='Barmak Plumbing', slug='barmak-pdf')
        TenantProfile.objects.create(tenant=barmak, letterhead={
            'layout': 'sectioned',
            'business_name': 'Barmak Plumbing',
            'trading_name': 'ROYAL HARDWARE',
            'bank': {'account_name': 'Barmak Plumbing Private Limited',
                     'account_number': '1154714543'},
            'terms': ['Deposit 75% before work begins'],
            'signatory': 'T. Barmak',
        })
        lead = make_lead(9952, tenant=barmak, customer_name='Barmak Client',
                         project_type='bathroom_renovation')
        quote = Quotation.objects.create(
            appointment=lead, labor_cost=Decimal('200'),
            transport_cost=Decimal('30'), vat_percent=Decimal('15'),
            notes='Deposit 75% before work begins')
        QuotationItem.objects.create(quotation=quote, description='Copper pipe',
                                     section='PLUMBING MATERIALS',
                                     quantity=10, unit_price=Decimal('5'))
        return quote

    def test_a_sectioned_tenant_gets_their_own_sheet(self):
        text = self._text(self._sectioned_quote())
        # 'Quotation', the casing .bq-qtitle actually renders - the screen is
        # the source of truth for the wording, not the other way round.
        self.assertIn('Quotation', text)
        self.assertIn('PLUMBING MATERIALS', text)
        self.assertIn('SUB-TOTAL', text)
        self.assertIn('GRAND TOTAL', text)
        # ...and not the flat sheet's blocks.
        self.assertNotIn('CLIENT INFORMATION', text)

    def test_the_sectioned_table_leads_with_qty_like_the_screen(self):
        """The sheet's own column order is QTY | DESCRIPTION | UNIT PRICE |
        TOTAL PRICE. The PDF used to lead with Item and label them Qty/Price."""
        text = self._text(self._sectioned_quote())
        for header in ('QTY', 'DESCRIPTION', 'UNIT PRICE', 'TOTAL PRICE'):
            self.assertIn(header, text, header)
        self.assertLess(text.index('QTY'), text.index('DESCRIPTION'))

    def test_the_sectioned_letterhead_carries_the_trade_line(self):
        text = self._text(self._sectioned_quote())
        self.assertIn('DOMESTIC | INDUSTRIAL | COMMERCIAL', text)

    def test_a_long_blurb_wraps_instead_of_running_off_the_page(self):
        """Barmak's services blurb is one long line; unwrapped it ran past the
        right margin and was simply cut off mid-word on the customer's copy."""
        blurb = ('water & drain laying, all types of geyser, storage (jojo) tanks '
                 '& tank stands, gutters, flushing, toilet, tubs, wash hand basin, '
                 'sink, shower & all type mixers')
        tenant = Tenant.objects.create(name='Wrap Plumbing', slug='wrap-pdf')
        TenantProfile.objects.create(tenant=tenant, letterhead={
            'layout': 'sectioned', 'services_blurb': blurb})
        lead = make_lead(9954, tenant=tenant, customer_name='Wrap Client')
        quote = Quotation.objects.create(appointment=lead)
        text = self._text(quote)
        # The tail of the blurb has to survive, on some line or other.
        self.assertIn('mixers', text)

    def test_the_sectioned_sheet_carries_vat_terms_and_banking(self):
        text = self._text(self._sectioned_quote())
        self.assertIn('VAT', text)
        self.assertIn('Deposit 75% before work begins', text)
        self.assertIn('Banking Details', text)
        self.assertIn('1154714543', text)
        self.assertIn('Client signature', text)
        self.assertIn('T. Barmak', text)

    def test_the_sectioned_sheet_uses_the_tenants_trading_name(self):
        text = self._text(self._sectioned_quote())
        self.assertIn('ROYAL HARDWARE', text)

    def test_the_layout_follows_the_tenant_not_the_viewer(self):
        """Same rule as the screens: the LEAD's tenant owns the document."""
        from bot.views.quote_layout import is_sectioned, tenant_of
        sectioned = self._sectioned_quote()
        self.assertTrue(is_sectioned(tenant_of(None, quotation=sectioned)))
        self.assertFalse(is_sectioned(tenant_of(None, quotation=self.quote)))

    def test_a_tenant_with_no_bank_details_gets_no_banking_block(self):
        """Absent means omit, never another tenant's account number."""
        plain = Tenant.objects.create(name='Plain Sectioned', slug='plain-sectioned')
        TenantProfile.objects.create(tenant=plain, letterhead={
            'layout': 'sectioned', 'business_name': 'Plain Sectioned'})
        lead = make_lead(9953, tenant=plain, customer_name='Plain Client')
        quote = Quotation.objects.create(appointment=lead)
        text = self._text(quote)
        self.assertNotIn('Banking Details', text)
        self.assertNotIn('1154714543', text)

    # -- currency ------------------------------------------------------------

    def test_the_currency_is_the_tenants_own(self):
        """'US$' was hardcoded into every figure on every tenant's quote."""
        self.profile.currency = 'ZAR '
        self.profile.save(update_fields=['currency'])
        text = self._text(self.quote)
        self.assertIn('ZAR 100.00', text)
        self.assertNotIn('US$100.00', text)

    # -- the send paths use this same builder -------------------------------

    def test_download_and_send_share_one_builder(self):
        """What the plumber checks is byte-for-byte what the customer gets."""
        response = self.client.get(
            reverse('download_quotation_pdf', args=[self.quote.pk]))
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertTrue(b''.join(response.streaming_content).startswith(b'%PDF'))


class QuotePdfGeometryTests(StaffClientTestCase):
    """WHERE things land on the sheet, not just that they are on it.

    Text extraction alone said the old PDF was fine: every value was present.
    It was the LAYOUT that did not match - everything centred, the table led by
    Item instead of QTY, and a services blurb running clean off the right edge
    and being cut mid-word on the customer's copy. So these read the drawing
    coordinates.
    """

    PAGE_WIDTH = 595.28          # A4 points
    MARGIN = 40

    def setUp(self):
        super().setUp()
        self.tenant = Tenant.objects.create(name='Barmak Plumbing', slug='barmak-geom')
        TenantProfile.objects.create(
            tenant=self.tenant,
            location_line='20398 Budiriro 5B Cabs Harare',
            letterhead={
                'layout': 'sectioned',
                'trading_name': 'ROYAL HARDWARE',
                'services_blurb': (
                    'water & drain laying, all types of geyser, storage (jojo) '
                    'tanks & tank stands, gutters, flushing, toilet, tubs, wash '
                    'hand basin, sink, shower & all type mixers'),
                'phones': ['+263 77 387 1503', '+263 77 324 0167',
                           '+263 718 744 685', '+263 713 152 080'],
                'public_email': 'info@barmakplumbing.co.zw',
                'website': 'www.barmakplumbing.co.zw',
            })
        self.lead = make_lead(9960, tenant=self.tenant, customer_name='jonas',
                              project_type='bathroom_renovation')
        self.quote = Quotation.objects.create(appointment=self.lead)
        for section, desc, qty, unit in (
            ('PLUMBING MATERIALS', '20mm PPR pipe', 19, '4.50'),
            ('PLUMBING MATERIALS', '20mm elbows', 30, '0.80'),
            ('SANITARY WARE', 'Wash hand basin', 2, '65.00'),
        ):
            QuotationItem.objects.create(quotation=self.quote, section=section,
                                         description=desc, quantity=qty,
                                         unit_price=Decimal(unit))

    def _placed(self):
        """(x, y, size, text) for every string the PDF draws."""
        import base64
        import re
        import zlib
        from bot.quote_pdf import build_quotation_pdf

        path = build_quotation_pdf(self.quote)
        try:
            raw = open(path, 'rb').read()
        finally:
            os.remove(path)

        placed = []
        for chunk in re.findall(rb'stream\r?\n(.*?)endstream', raw, re.S):
            body = chunk.strip(b'\r\n')
            try:
                body = base64.a85decode(body, adobe=True)
            except Exception:
                pass
            try:
                body = zlib.decompress(body)
            except zlib.error:
                pass
            blob = body.decode('latin-1')
            size = 10.0
            for match in re.finditer(
                    r'/F\d+ ([\d.]+) Tf|1 0 0 1 ([\d.-]+) ([\d.-]+) Tm \((.*?)\) Tj',
                    blob):
                if match.group(1):
                    size = float(match.group(1))
                    continue
                placed.append((float(match.group(2)), float(match.group(3)),
                               size, match.group(4)))
        return placed

    def _find(self, placed, needle):
        for x, y, size, text in placed:
            if needle in text:
                return x, y, size, text
        self.fail(f'{needle!r} was never drawn')

    # -- the letterhead is two columns, not centred -------------------------

    def test_the_business_name_sits_on_the_left(self):
        x, _, _, _ = self._find(self._placed(), 'BARMAK PLUMBING')
        self.assertLess(x, 150, 'the name should be left-aligned, not centred')

    def test_the_contacts_sit_on_the_right(self):
        placed = self._placed()
        for needle in ('Budiriro', 'info@barmakplumbing.co.zw'):
            with self.subTest(line=needle):
                x, _, _, _ = self._find(placed, needle)
                self.assertGreater(x, self.PAGE_WIDTH / 2,
                                   'contacts belong in the right column')

    def test_the_trade_line_is_under_the_name(self):
        placed = self._placed()
        _, name_y, _, _ = self._find(placed, 'BARMAK PLUMBING')
        x, sub_y, _, _ = self._find(placed, 'DOMESTIC | INDUSTRIAL | COMMERCIAL')
        self.assertLess(sub_y, name_y)
        self.assertLess(x, 150)

    # -- nothing runs off the sheet ----------------------------------------

    def test_nothing_is_drawn_past_the_right_margin(self):
        """The services blurb was one long unwrapped line, cut mid-word."""
        from reportlab.pdfbase.pdfmetrics import stringWidth

        limit = self.PAGE_WIDTH - self.MARGIN + 6
        for x, _, size, text in self._placed():
            clean = text.replace('\\(', '(').replace('\\)', ')')
            width = stringWidth(clean, 'Helvetica', size)
            with self.subTest(text=clean[:40]):
                self.assertLessEqual(x + width, limit)

    def test_the_long_blurb_is_wrapped_over_lines(self):
        placed = self._placed()
        blurb_lines = [t for _, _, _, t in placed if 'water & drain laying' in t
                       or 'all type mixers' in t]
        self.assertGreaterEqual(len(blurb_lines), 2, 'the blurb should wrap')

    # -- the table matches the sheet ---------------------------------------

    def test_the_columns_are_in_the_sheets_own_order(self):
        placed = self._placed()
        qty_x, _, _, _ = self._find(placed, 'QTY')
        desc_x, _, _, _ = self._find(placed, 'DESCRIPTION')
        unit_x, _, _, _ = self._find(placed, 'UNIT PRICE')
        total_x, _, _, _ = self._find(placed, 'TOTAL PRICE')
        self.assertLess(qty_x, desc_x)
        self.assertLess(desc_x, unit_x)
        self.assertLess(unit_x, total_x)

    def test_the_column_header_is_drawn_once_not_per_section(self):
        """The sheet is ONE table whose section headings are rows."""
        headers = [t for _, _, _, t in self._placed() if t == 'DESCRIPTION']
        self.assertEqual(len(headers), 1)

    def test_both_sections_are_numbered_in_order(self):
        placed = self._placed()
        _, first_y, _, _ = self._find(placed, 'PLUMBING MATERIALS')
        _, second_y, _, _ = self._find(placed, 'SANITARY WARE')
        self.assertGreater(first_y, second_y, 'sections keep their entered order')

    def test_each_section_has_its_own_subtotal(self):
        subtotals = [t for _, _, _, t in self._placed() if t == 'SUB-TOTAL']
        # One per section, plus the net sub-total in the totals block.
        self.assertGreaterEqual(len(subtotals), 3)
