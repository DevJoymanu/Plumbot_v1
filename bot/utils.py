import os
import base64
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import connection
from django.templatetags.static import static
from django.utils import timezone


def _to_decimal(value, default='0.00'):
    """Convert API numeric inputs to Decimal safely."""
    if value in (None, ''):
        return Decimal(default)
    try:
        cleaned = (
            str(value).strip()
            .replace('US$', '')
            .replace('$', '')
            .replace(',', '')
            .replace(' ', '')
        )
        return Decimal(cleaned)
    except (InvalidOperation, TypeError, ValueError):
        return Decimal(default)


def _to_float(value, default=0.0):
    """Safe float conversion using decimal normalizer."""
    try:
        return float(_to_decimal(value, default=str(default)))
    except Exception:
        return float(default)


def _safe_logo_url():
    """Return static logo URL without crashing when manifest entry is missing."""
    for path in ('images/logo.jpg', 'logo.jpg'):
        try:
            return static(path)
        except ValueError:
            continue
    return '/static/images/logo.jpg'


def _safe_logo_data_uri():
    """Return inline data URI for logo when static serving is unavailable."""
    logo_candidates = [
        os.path.join(settings.BASE_DIR, 'bot', 'static', 'images', 'logo.jpg'),
        os.path.join(settings.BASE_DIR, 'bot', 'static', 'logo.jpg'),
        os.path.join(settings.BASE_DIR, 'static', 'images', 'logo.jpg'),
    ]
    logo_path = next((p for p in logo_candidates if os.path.exists(p)), None)
    if not logo_path:
        return ''
    try:
        with open(logo_path, 'rb') as f:
            encoded = base64.b64encode(f.read()).decode('ascii')
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        return ''


def _reset_pk_sequence(model):
    """Reset Postgres PK sequence to current MAX(id) for a model table."""
    if connection.vendor != 'postgresql':
        return False
    table_name = model._meta.db_table
    pk_column = model._meta.pk.column
    quoted_table = connection.ops.quote_name(table_name)
    quoted_pk = connection.ops.quote_name(pk_column)
    sql = (
        f"SELECT setval(pg_get_serial_sequence('{table_name}', '{pk_column}'), "
        f"COALESCE(MAX({quoted_pk}), 1), true) FROM {quoted_table};"
    )
    with connection.cursor() as cursor:
        cursor.execute(sql)
    return True


def _append_admin_note(appointment, message):
    timestamp = timezone.localtime(timezone.now()).strftime('%Y-%m-%d %H:%M')
    existing = appointment.admin_notes or ''
    appointment.admin_notes = f"[{timestamp}] {message}\n{existing}".strip()
    appointment.save(update_fields=['admin_notes'])


def clean_phone_number(phone):
    """Convert phone number to WhatsApp Cloud API format (no prefix, no +)."""
    return phone.replace('whatsapp:', '').replace('+', '').strip()


def format_phone_number_for_storage(phone):
    """Format phone number for database storage with whatsapp: prefix."""
    if not phone.startswith('whatsapp:'):
        return f"whatsapp:+{phone}"
    return phone
