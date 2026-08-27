"""TEMPORARY — dumps the quote item editors so their JS can be exercised in
Node. Delete after the check.
    python manage.py test bot.test_dump_quote_pages
"""
import os

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from bot.models import (Appointment, Quotation, QuotationItem, Tenant,
                        TenantMembership, TenantProfile)

OUT = os.environ.get('QUOTE_DUMP_DIR', 'quote_dump')

LETTERHEAD = {
    'layout': 'sectioned',
    'trading_name': 'ROYAL HARDWARE',
    'phones': ['+263 77 387 1503'],
    'bank': {'account_name': 'Barmak Plumbing Private Limited',
             'account_number': '1154714543'},
    'terms': ['deposit 75%'],
}


class DumpQuoteEditors(TestCase):
    def setUp(self):
        self.homebase, _ = Tenant.objects.get_or_create(
            slug='homebase', defaults={'name': 'Homebase Plumbers'})
        self.barmak = Tenant.objects.create(name='Barmak Plumbing', slug='barmak-plumbing')
        TenantProfile.objects.create(tenant=self.barmak, letterhead=LETTERHEAD)

        self.hb_user = self._staff('hb-dumper', self.homebase)
        self.bq_user = self._staff('bq-dumper', self.barmak)

    @staticmethod
    def _staff(username, tenant):
        user = get_user_model().objects.create_user(
            username=username, password='pw', is_staff=True)
        TenantMembership.objects.create(user=user, tenant=tenant, role='staff')
        return user

    def test_dump(self):
        os.makedirs(OUT, exist_ok=True)

        # ── flat editors (homebase) ──
        self.client.force_login(self.hb_user)
        hb_lead = Appointment.objects.create(
            phone_number='whatsapp:+15551110001', customer_name='Flat Client',
            tenant=self.homebase)
        hb_quote = Quotation.objects.create(appointment=hb_lead)
        for desc, qty, unit in [('Basin mixer', 1, 40), ('Angle valve', 3, 3)]:
            QuotationItem.objects.create(
                quotation=hb_quote, description=desc, quantity=qty, unit_price=unit)

        pages = {
            'flat_create': reverse('create_quotation', args=[hb_lead.pk]),
            'flat_standalone': reverse('standalone_quotation'),
            'flat_edit': reverse('edit_quotation', args=[hb_quote.pk]),
        }
        for name, url in pages.items():
            self._write(name, url)

        # ── sectioned editor (barmak) ──
        self.client.force_login(self.bq_user)
        bq_lead = Appointment.objects.create(
            phone_number='whatsapp:+263771234567', customer_name='Sectioned Client',
            tenant=self.barmak)
        self._write('sectioned_create', reverse('create_quotation', args=[bq_lead.pk]))

        bq_quote = Quotation.objects.create(appointment=bq_lead)
        for section, desc, qty, qty_text, unit in [
            ('PLUMBING MATERIALS', 'Basin mixer', 2, '2 pcs', 40),
            ('FITTINGS', 'Angle valve', 3, '3', 3),
        ]:
            QuotationItem.objects.create(
                quotation=bq_quote, description=desc, section=section,
                quantity=qty, quantity_text=qty_text, unit_price=unit)
        self._write('sectioned_edit', reverse('edit_quotation', args=[bq_quote.pk]))

    def _write(self, name, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f'{name} -> {response.status_code}')
        with open(os.path.join(OUT, name + '.html'), 'wb') as handle:
            handle.write(response.content)
        print('wrote', name)
