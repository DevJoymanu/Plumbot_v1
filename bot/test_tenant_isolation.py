"""
Cross-tenant isolation: the standing guard, not a fix for one bug.

On 2026-09-08 a Barmak booking was emailed to HOMEBASE's plumber. The cause was
one line — `Plumbot(appointment.phone_number)` with no tenant, which resolves
to the homebase seed — and it had two effects at once: an empty homebase lead
was created on Barmak's customer's phone number (ghost lead 1145), and every
notification that instance sent went to the wrong company about somebody else's
customer.

Everything downstream behaved correctly. The Plumbot believed it was a homebase
lead, so its tenant and its appointment agreed with each other, and every
per-tenant resolver did exactly what it was told. Nothing compared the lead the
CALLER was holding with the lead the object ended up with.

These tests are that comparison, plus a source check so a new call site cannot
reintroduce it quietly. The source check is deliberately crude and deliberately
present: the failure it guards is invisible at runtime until a client tells you
they received another client's customer.
"""

import inspect
import re
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from .models import Appointment, Tenant
from .plumber_notifications import CrossTenantSend, assert_same_tenant
from .views.plumbot.base import Plumbot


class PlumbotKeepsItsTenantTests(TestCase):
    def setUp(self):
        self.barmak = Tenant.objects.create(name='Barmak', slug='barmak-x')
        self.other = Tenant.objects.create(name='Other', slug='other-x')
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+263715725627', tenant=self.barmak,
            customer_area='Arlington East')

    def test_for_appointment_keeps_the_lead_it_was_given(self):
        bot = Plumbot.for_appointment(self.lead)
        self.assertEqual(bot.appointment.pk, self.lead.pk)
        self.assertEqual(bot.tenant, self.barmak)

    def test_it_creates_no_second_lead_on_that_number(self):
        before = Appointment.objects.filter(
            phone_number=self.lead.phone_number).count()
        Plumbot.for_appointment(self.lead)
        self.assertEqual(
            Appointment.objects.filter(
                phone_number=self.lead.phone_number).count(), before)

    def test_the_bare_constructor_is_what_went_wrong(self):
        # Kept as a demonstration, not an endorsement: this is the exact call
        # that produced ghost lead 1145 and mailed the wrong plumber.
        bot = Plumbot(self.lead.phone_number)          # no tenant
        self.assertNotEqual(bot.appointment.pk, self.lead.pk)
        self.assertNotEqual(bot.tenant, self.barmak)

    def test_a_resolution_that_lands_elsewhere_raises(self):
        stranger = Appointment.objects.create(
            phone_number='whatsapp:+263715725627', tenant=self.other)
        with patch.object(Appointment.objects, 'get_or_create_lead',
                          return_value=(stranger, False)):
            with self.assertRaises(CrossTenantSend):
                Plumbot.for_appointment(self.lead)


class RecipientGuardTests(TestCase):
    def setUp(self):
        self.a = Tenant.objects.create(name='A', slug='a-x')
        self.b = Tenant.objects.create(name='B', slug='b-x')
        self.lead = Appointment.objects.create(
            phone_number='whatsapp:+263770000009', tenant=self.a)

    def test_the_matching_tenant_passes(self):
        assert_same_tenant(self.lead, self.a)          # must not raise

    def test_another_tenant_is_refused(self):
        with self.assertRaises(CrossTenantSend):
            assert_same_tenant(self.lead, self.b)

    def test_it_stays_out_of_the_way_when_there_is_nothing_to_compare(self):
        # Platform-level sends carry no tenant, and some callers carry no lead.
        assert_same_tenant(None, self.a)
        assert_same_tenant(self.lead, None)


class NoBareConstructionTests(TestCase):
    """No call site may build a Plumbot from a phone number alone.

    A source check, because the runtime symptom is a correct-looking email in
    the wrong company's inbox. Nothing fails, nothing logs, and the first
    report comes from a client.
    """

    # Offline entry points that legitimately have no tenant to pass: the
    # scenario runner and the chat REPL predate tenant threading and resolve to
    # the homebase seed on purpose.
    ALLOWED = {'bot/views/plumbot/base.py'}

    def test_every_call_site_passes_a_tenant(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in (root / 'bot').rglob('*.py'):
            rel = path.relative_to(root).as_posix()
            if rel in self.ALLOWED or '/test' in rel or rel.startswith('bot/test'):
                continue
            for n, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                stripped = line.strip()
                # Prose about the bug is not the bug.
                if stripped.startswith('#'):
                    continue
                if not re.search(r'(?<![.\w])Plumbot\(', line):
                    continue
                if 'tenant=' in line or 'for_appointment' in line:
                    continue
                offenders.append('%s:%d  %s' % (rel, n, line.strip()))
        self.assertEqual(
            offenders, [],
            'Plumbot built without a tenant resolves to the HOMEBASE seed, '
            'which creates a ghost lead and mails the wrong company. Use '
            'Plumbot.for_appointment(appointment), or pass tenant=. Found:\n'
            + '\n'.join(offenders))

    def test_the_dashboard_actions_use_for_appointment(self):
        from .views import appointments as views
        src = inspect.getsource(views)
        self.assertIn('Plumbot.for_appointment(appointment)', src)
        self.assertNotIn('Plumbot(appointment.phone_number)', src)
