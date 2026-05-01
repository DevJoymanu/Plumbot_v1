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
    def __init__(self, phone_number):
        self.phone_number = phone_number
        self.appointment, _ = Appointment.objects.get_or_create(
            phone_number=phone_number,
            defaults={'status': 'pending'}
        )
