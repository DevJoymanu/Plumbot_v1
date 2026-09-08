"""TEMPORARY — dumps the quote item editors so their JS can be exercised in
Node. Delete after the check.
    python manage.py test bot.test_dump_quote_pages
"""
import os

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from bot.models import (Appointment, Quotation, QuotationItem,
                        QuotationTemplate, QuotationTemplateItem, Tenant,
                        TenantMembership, TenantProfile)

OUT = os.environ.get('QUOTE_DUMP_DIR', 'quote_dump')

LETTERHEAD = {
    'layout': 'sectioned',
    'trading_name': 'ROYAL HARDWARE',
    'phones': ['+263 77 387 1503'],
    'bank': {'account_name': 'Barmak Plumbing Private Limited',
             'account_number': '1154714543'},
    'terms': ['deposit 75%'],
    'default_deposit_percent': 75,
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
            tenant=self.homebase,
            customer_area='Borrowdale',
            # The PLAN tab only renders for a lead who has something behind it.
            project_description='Two bathrooms and a guest toilet. Plan attached.')
        hb_lead.plan_file.save('house-plan.pdf',
                               SimpleUploadedFile('house-plan.pdf', b'%PDF-1.4 fake'),
                               save=True)
        hb_quote = Quotation.objects.create(appointment=hb_lead)
        for desc, qty, unit in [('Basin mixer', 1, 40), ('Angle valve', 3, 3)]:
            QuotationItem.objects.create(
                quotation=hb_quote, description=desc, quantity=qty, unit_price=unit)

        hb_tpl = QuotationTemplate.objects.create(
            name='Standard bathroom', project_type='bathroom_renovation',
            tenant=self.homebase, default_labor_cost=120, default_transport_cost=15)
        QuotationTemplateItem.objects.create(
            template=hb_tpl, description='Toilet suite', section='CONTROL VALVES',
            quantity=1, quantity_text='19 length', unit_price=180)

        pages = {
            'flat_create': reverse('create_quotation', args=[hb_lead.pk]),
            'flat_standalone': reverse('standalone_quotation'),
            'flat_edit': reverse('edit_quotation', args=[hb_quote.pk]),
            'tpl_flat_create': reverse('create_quotation_template'),
            'tpl_flat_edit': reverse('edit_quotation_template', args=[hb_tpl.pk]),
        }
        for name, url in pages.items():
            self._write(name, url)

        # ── sectioned editor (barmak) ──
        self.client.force_login(self.bq_user)
        bq_lead = Appointment.objects.create(
            phone_number='whatsapp:+263771234567', customer_name='Sectioned Client',
            tenant=self.barmak, customer_area='Budiriro',
            project_description='Re-pipe the whole house.')
        bq_lead.plan_file.save('site-plan.pdf',
                               SimpleUploadedFile('site-plan.pdf', b'%PDF-1.4 fake'),
                               save=True)
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

        # ── the sectioned TEMPLATE builder (barmak) ──
        self._write('tpl_sectioned_create', reverse('create_quotation_template'))

        tpl = QuotationTemplate.objects.create(
            name='Standard drainage', project_type='general', tenant=self.barmak,
            default_labor_cost=625, default_transport_cost=70)
        for order, (section, qty, qty_text, desc, unit) in enumerate([
            ('CONTROL VALVES', 5, '5', '20mm ball cork', 10),
            ('CONTROL VALVES', 1, '1', '20mm pressure control valve', 28),
            ('DRAINAGE PIPE & MATERIAL', 19, '19 length', '110mm pvc UG Pipe', 18),
        ]):
            QuotationTemplateItem.objects.create(
                template=tpl, section=section, description=desc, quantity=qty,
                quantity_text=qty_text, unit_price=unit, sort_order=order)
        self._write('tpl_sectioned_edit',
                    reverse('edit_quotation_template', args=[tpl.pk]))

    def _write(self, name, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, f'{name} -> {response.status_code}')
        with open(os.path.join(OUT, name + '.html'), 'wb') as handle:
            handle.write(response.content)
        print('wrote', name)
