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
    def __init__(self, phone_number, tenant=None):
        self.phone_number = phone_number
        # Tenant-aware identity (Phase 1): phone is unique PER TENANT, so the
        # lookup must include the owner. None (dashboard actions, scenario
        # runner, chat REPL — pre-threading callers) resolves to the homebase
        # seed inside get_or_create_lead.
        self.appointment, _ = Appointment.objects.get_or_create_lead(
            phone_number, tenant=tenant,
        )
        self.tenant = self.appointment.tenant
