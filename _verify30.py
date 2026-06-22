from bot.models import Appointment
from django.utils import timezone
import pytz, re

cat = pytz.timezone('Africa/Johannesburg')
now = timezone.now()
print('NOW (SAST):', now.astimezone(cat).strftime('%Y-%m-%d %H:%M:%S'))

a = Appointment.objects.get(id=467)
sd = a.scheduled_datetime.astimezone(cat)
mins_to_appt = (a.scheduled_datetime - now).total_seconds() / 60
print(f'appt #467 scheduled (SAST): {sd.strftime("%H:%M")}  | mins until appt: {mins_to_appt:.1f}')

flags = re.findall(r'\[(email_[^\]]+)\]', a.internal_notes or '')
print('email eflags now:', flags)
print('email_30min_467 set?:', '[email_30min_467]' in (a.internal_notes or ''))
print('email_2hr_467 set?:  ', '[email_2hr_467]' in (a.internal_notes or ''))
