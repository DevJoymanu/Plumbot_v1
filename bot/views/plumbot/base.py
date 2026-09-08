from .state_mixin import StateMixin
from .response_mixin import ResponseMixin
from .extraction_mixin import ExtractionMixin
from .availability_mixin import AvailabilityMixin
from .booking_mixin import BookingMixin
from .reschedule_mixin import RescheduleMixin
from .notification_mixin import NotificationMixin
from .plan_upload_mixin import PlanUploadMixin

from ...models import Appointment
import logging

logger = logging.getLogger(__name__)


class Plumbot(
    StateMixin,
    ResponseMixin,
    ExtractionMixin,
    AvailabilityMixin,
    BookingMixin,
    RescheduleMixin,
    NotificationMixin,
    PlanUploadMixin,
):
    @classmethod
    def for_appointment(cls, appointment):
        """THE way to build a Plumbot for a lead you are already holding.

        `Plumbot(phone_number)` with no tenant resolves to the HOMEBASE SEED
        (see get_or_create_lead), so calling it with another tenant's lead does
        two damaging things at once: it creates an empty homebase lead on that
        phone number, and every notification the instance then sends goes to
        HOMEBASE's plumber about somebody else's customer.

        That is not hypothetical. On 2026-09-08 the manual booking alert did
        exactly this: Barmak's lead 1144 produced ghost homebase lead 1145 and
        emailed Homebase's plumber about a Barmak booking. The dashboard
        Confirm button had the same defect and had been sending customer
        confirmations from the wrong tenant for far longer.

        Anywhere you have an Appointment, use this. The tenant then cannot be
        dropped, because it is not a separate argument anybody can forget.

        It also VERIFIES the result. Passing the tenant should return the same
        row you handed in, and if it does not, the lookup has landed on a
        different lead and everything this instance goes on to send would be
        about the wrong customer. Better to raise here than to notify.
        """
        bot = cls(appointment.phone_number, tenant=appointment.tenant)
        if bot.appointment.pk != appointment.pk:
            from bot.plumber_notifications import CrossTenantSend
            raise CrossTenantSend(
                'Plumbot for lead %s resolved to lead %s (%s vs %s)' % (
                    appointment.pk, bot.appointment.pk,
                    getattr(appointment.tenant, 'slug', '?'),
                    getattr(bot.appointment.tenant, 'slug', '?'),
                )
            )
        return bot

    def __init__(self, phone_number, tenant=None):
        self.phone_number = phone_number
        # Tenant-aware identity (Phase 1): phone is unique PER TENANT, so the
        # lookup must include the owner. None (scenario runner, chat REPL —
        # pre-threading callers) resolves to the homebase seed inside
        # get_or_create_lead.
        #
        # That fallback is a footgun for anyone holding a real lead, so it says
        # so. It is a warning rather than an error because the offline callers
        # that legitimately pass nothing must keep working.
        if tenant is None:
            logger.warning(
                'Plumbot built with no tenant for %s — falling back to the '
                'homebase seed. If you hold an Appointment, use '
                'Plumbot.for_appointment(appointment) instead.',
                phone_number,
            )
        self.appointment, _ = Appointment.objects.get_or_create_lead(
            phone_number, tenant=tenant,
        )
        self.tenant = self.appointment.tenant
