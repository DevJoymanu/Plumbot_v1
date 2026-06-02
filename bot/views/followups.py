from django.shortcuts import render, redirect, get_object_or_404
from django.views import View
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST, require_http_methods, require_GET
from django.utils.decorators import method_decorator
from .dashboard import _followups_workspace_data
from django.http import HttpResponse, JsonResponse, HttpResponseRedirect
from django.urls import reverse, reverse_lazy
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.views.generic import ListView, DetailView, TemplateView, CreateView, UpdateView, DeleteView
from django.db.models import Count, Q
from django.db import IntegrityError, connection, transaction
from django.utils import timezone
from django.forms import modelformset_factory
from django.templatetags.static import static
from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import requests
import pytz
import os
import json
import re
import tempfile
import base64
import logging

from ..models import (
    Appointment, Quotation, QuotationItem,
    QuotationTemplate, QuotationTemplateItem, ConversationMessage,
)
from ..forms import (
    AppointmentForm, SettingsForm, CalendarSettingsForm, AISettingsForm,
    QuotationForm, QuotationItemFormSet,
    QuotationTemplateForm, QuotationTemplateItemFormSet,
)
from ..decorators import staff_required, anonymous_required, StaffRequiredMixin
from ..whatsapp_cloud_api import whatsapp_api
from ..services.clients import (
    twilio_client, deepseek_client,
    TWILIO_WHATSAPP_NUMBER, GOOGLE_CALENDAR_CREDENTIALS,
    DEEPSEEK_API_KEY,
)
from ..utils import (
    _to_decimal, _to_float, _safe_logo_url, _safe_logo_data_uri,
    _reset_pk_sequence, _append_admin_note,
    clean_phone_number, format_phone_number_for_storage,
)

logger = logging.getLogger(__name__)
from ..services.lead_scoring import refresh_lead_score


@staff_required
def followup_dashboard(request):
    """Dashboard showing follow-up statistics and leads"""
    response_age = request.GET.get('response_age', '').strip()
    if not response_age:
        response_age = '1w_minus'
    context = _followups_workspace_data(response_age)
    context['active_nav'] = 'followups'
    
    return render(request, 'bot/pages/followup_dashboard.html', context)


@staff_required
def mark_lead_inactive(request, pk):
    """Manually mark a lead as inactive"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        reason = request.POST.get('reason', 'manual')
        appointment.mark_as_inactive_lead(reason=reason)
        
        messages.success(request, f'Lead for {appointment.customer_name or appointment.phone_number} marked as inactive')
        return redirect('appointments_list')
    
    return render(request, 'bot/pages/confirm_mark_inactive.html', {
        'appointment': appointment
    })


@staff_required
def reactivate_lead(request, pk):
    """Reactivate an inactive lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        appointment.is_lead_active = True
        appointment.followup_stage = 'none'
        appointment.lead_marked_inactive_at = None
        appointment.save()
        
        messages.success(request, f'Lead reactivated for {appointment.customer_name or appointment.phone_number}')
        return redirect('appointment_detail', pk=appointment.pk)
    
    return render(request, 'bot/pages/confirm_reactivate.html', {
        'appointment': appointment
    })


@staff_required  
def test_followup_message(request, pk):
    """Send a test follow-up message for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    if request.method == 'POST':
        stage = request.POST.get('stage', 'day_1')
        
        # Import the management command to use its message generator
        from django.core.management import call_command
        from io import StringIO
        
        # You can manually craft a test message or use the generator
        from bot.management.commands.send_followups import Command
        cmd = Command()
        message = cmd.generate_followup_message(appointment, stage)
        
        # Send it
        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_text_message(clean_phone, message)
        
        messages.success(request, f'Test {stage} follow-up sent to {appointment.phone_number}')
        return redirect('appointment_detail', pk=appointment.pk)
    
    return render(request, 'bot/pages/test_followup.html', {
        'appointment': appointment,
        'stages': ['day_1', 'day_3', 'week_1', 'week_2', 'month_1']
    })


@staff_required
def test_followup_email(request):
    """
    Fire a delay-flow email to an arbitrary address for deliverability testing.

    GET params:
      to    — target email address (required)
      kind  — 'followup' (default) | 'last_check' | 'quote'
      apt   — appointment PK to clone context from (default: most recent
              appointment with a customer_email on file)
      probe — if set (e.g. ?probe=1), skip sending and instead test raw TCP
              reachability to SMTP ports 587/465/2525 from this container

    Returns JSON: {"sent": bool, "apt": int, "kind": str, "to": str}.
    The appointment's customer_email is temporarily overridden in-memory
    (NOT saved) so the real customer is never contacted.
    """
    from bot.customer_emails import (
        send_delay_followup_email,
        send_delay_last_check_email,
        send_delay_quote_email,
    )
    from bot.models import Appointment

    # Egress probe mode (?probe=1): test raw TCP reachability to the SMTP
    # submission ports from THIS container, isolating the network layer from
    # SMTP/TLS/auth. A timeout means the host is dropping outbound SMTP;
    # 'refused' means reachable but nothing listening. 2525 is the fallback
    # submission port relays expose for hosts that block 587/465.
    if request.GET.get("probe"):
        import socket
        import time

        results = {}
        for host, port in (
            ("smtp.gmail.com", 587),
            ("smtp.gmail.com", 465),
            ("smtp.gmail.com", 2525),
        ):
            started = time.monotonic()
            try:
                # 4s, not 10s: three sequential 10s connects = 30s, which hits
                # gunicorn's sync-worker timeout and gets the worker killed.
                socket.create_connection((host, port), timeout=4).close()
                outcome = "OPEN"
            except Exception as exc:  # noqa: BLE001 — surface every failure mode
                outcome = f"{type(exc).__name__}: {exc}"
            results[f"{host}:{port}"] = {
                "result": outcome,
                "ms": round((time.monotonic() - started) * 1000),
            }
        return JsonResponse({"probe": True, "ports": results})

    to_addr = (request.GET.get("to") or "").strip()
    if "@" not in to_addr:
        return JsonResponse({"error": "missing or invalid 'to' query param"}, status=400)

    kind = (request.GET.get("kind") or "followup").strip()
    apt_pk = request.GET.get("apt")

    if apt_pk:
        try:
            apt = Appointment.objects.get(pk=int(apt_pk))
        except (ValueError, Appointment.DoesNotExist):
            return JsonResponse({"error": f"appointment {apt_pk} not found"}, status=404)
    else:
        apt = (
            Appointment.objects
            .filter(customer_email__isnull=False)
            .exclude(customer_email="")
            .order_by("-id")
            .first()
        )
        if not apt:
            return JsonResponse({"error": "no appointment with a customer_email found"}, status=404)

    # In-memory override only — never save, so the real customer isn't touched.
    apt.customer_email = to_addr

    # Diagnostic mode: open an explicit mail connection and surface any SMTP
    # error in the JSON response so we don't have to dig through Railway logs.
    import traceback
    from django.conf import settings as _settings
    from django.core.mail import get_connection

    smtp_diag = {
        "host":     getattr(_settings, "EMAIL_HOST", ""),
        "port":     getattr(_settings, "EMAIL_PORT", None),
        "use_tls":  getattr(_settings, "EMAIL_USE_TLS", None),
        "use_ssl":  getattr(_settings, "EMAIL_USE_SSL", None),
        "user":     getattr(_settings, "EMAIL_HOST_USER", ""),
        "backend":  getattr(_settings, "EMAIL_BACKEND", ""),
        "from":     getattr(_settings, "DEFAULT_FROM_EMAIL", ""),
        "reply_to": getattr(_settings, "EMAIL_REPLY_TO", ""),
    }

    # Pre-flight: open the connection to surface auth / TLS errors directly.
    error = None
    try:
        conn = get_connection()
        conn.open()
        conn.close()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}"

    if error:
        return JsonResponse({
            "sent": False, "apt": apt.pk, "kind": kind, "to": to_addr,
            "smtp": smtp_diag, "error": error,
        }, status=500)

    try:
        if kind == "last_check":
            ok = send_delay_last_check_email(apt)
        elif kind == "quote":
            ok = send_delay_quote_email(apt, follow_up_date_str="next Friday")
        else:
            kind = "followup"
            ok = send_delay_followup_email(apt)
    except Exception as exc:
        return JsonResponse({
            "sent": False, "apt": apt.pk, "kind": kind, "to": to_addr,
            "smtp": smtp_diag,
            "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}",
        }, status=500)

    return JsonResponse({
        "sent": bool(ok), "apt": apt.pk, "kind": kind, "to": to_addr,
        "smtp": smtp_diag,
    })


@staff_required
def followup_test_suite(request):
    """
    Unified test console for everything follow-up related, in one page:

      • Customer emails — fire any (or ALL) of the delay emails, reminder
        emails, and the booking confirmation to an arbitrary address.
      • WhatsApp follow-up — generate the exact template message the cron would
        send for a given stage/attempt, and optionally deliver it to a number.
      • Follow-up cron — run `send_followups` with a dry-run / force toggle and
        see the captured output.

    Nothing here touches a real lead: the appointment used for context has its
    customer_email overridden in-memory only (never saved), so the genuine
    customer is never emailed. See the JSON-only `test_followup_email` endpoint
    for the lower-level deliverability / SMTP-probe diagnostic.
    """
    from bot.customer_emails import (
        send_delay_followup_email,
        send_delay_last_check_email,
        send_delay_quote_email,
        send_booking_confirmation_email,
        send_customer_reminder_email,
    )

    # kind -> (human label, callable(apt) -> bool)
    EMAIL_TESTS = {
        'delay_followup':       ('Delay re-engagement email',     lambda a: send_delay_followup_email(a)),
        'delay_last_check':     ('Delay last-check email',        lambda a: send_delay_last_check_email(a)),
        'delay_quote':          ('Delay quote + portfolio email', lambda a: send_delay_quote_email(a, follow_up_date_str='next Friday')),
        'booking_confirmation': ('Booking confirmation email',    lambda a: send_booking_confirmation_email(a)),
        'reminder_two_days':    ('Reminder — 2 days before',      lambda a: send_customer_reminder_email(a, 'two_days')),
        'reminder_one_day':     ('Reminder — 1 day before',       lambda a: send_customer_reminder_email(a, 'one_day')),
        'reminder_morning':     ('Reminder — morning of',         lambda a: send_customer_reminder_email(a, 'morning')),
        'reminder_two_hours':   ('Reminder — 2 hours before',     lambda a: send_customer_reminder_email(a, 'two_hours')),
        'reminder_thirty_mins': ('Reminder — 30 mins before',     lambda a: send_customer_reminder_email(a, 'thirty_mins')),
    }
    FOLLOWUP_QUESTIONS = ['service_type', 'project_description', 'area', 'availability', 'complete']

    context = {
        'active_nav': 'followups',
        'email_tests': [(k, label) for k, (label, _fn) in EMAIL_TESTS.items()],
        'followup_questions': FOLLOWUP_QUESTIONS,
        'default_email': getattr(request.user, 'email', '') or '',
        'recent_appointments': Appointment.objects.order_by('-id')[:25],
        'results': None,
        # echo back form values so the page keeps state after a POST
        'form': request.POST if request.method == 'POST' else {},
    }

    if request.method != 'POST':
        return render(request, 'bot/pages/followup_test_suite.html', context)

    action = request.POST.get('action', '')
    apt_pk = (request.POST.get('apt') or '').strip()

    def _resolve_apt():
        """Return (appointment, error_message)."""
        if apt_pk:
            try:
                return Appointment.objects.get(pk=int(apt_pk)), None
            except (ValueError, Appointment.DoesNotExist):
                return None, f'Appointment {apt_pk} not found.'
        apt = Appointment.objects.order_by('-id').first()
        if not apt:
            return None, 'No appointments exist to use as test context.'
        return apt, None

    # ── SMTP egress probe ───────────────────────────────────────────────────
    # A raw TCP reachability test from THIS container to the submission ports,
    # isolating the network layer from SMTP/TLS/auth. A timeout means the host
    # is silently dropping outbound SMTP (Railway blocks these on some plans);
    # 'refused' means reachable but nothing is listening. This is the first
    # thing to run when sends fail with TimeoutError.
    if action == 'probe':
        import socket
        import time
        from concurrent.futures import ThreadPoolExecutor

        configured_host = (getattr(settings, 'EMAIL_HOST', '') or 'smtp.gmail.com').strip()
        configured_port = getattr(settings, 'EMAIL_PORT', 587)

        # IPv4-only connect, matching IPv4SMTPBackend — the IPv6 AAAA record
        # for these hosts fails instantly with ENETUNREACH on Railway and would
        # otherwise mask the real (IPv4) result.
        #
        # Timeout is deliberately short (4s) and all targets run concurrently:
        # gunicorn's sync worker is killed after 30s, so a sequential sweep of
        # blocking connects (7 × 8s = 56s) would crash the worker. Concurrent +
        # short keeps total wall-time at ~one timeout.
        PROBE_TIMEOUT = 4

        def _probe_ipv4(target):
            host, port, _is_cfg = target
            started = time.monotonic()
            try:
                infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
                if not infos:
                    raise OSError('no IPv4 address (A record) for host')
                af, socktype, proto, _canon, sa = infos[0]
                s = socket.socket(af, socktype, proto)
                s.settimeout(PROBE_TIMEOUT)
                s.connect(sa)
                s.close()
                return True, None, round((time.monotonic() - started) * 1000)
            except Exception as exc:  # noqa: BLE001 — surface every failure mode
                return False, f'{type(exc).__name__}: {exc}', round((time.monotonic() - started) * 1000)

        # The configured endpoint first, then known relays that ALSO listen on
        # 2525 — this distinguishes "Gmail/this-host blocked" from "all outbound
        # SMTP blocked". If a 2525 relay is OPEN, SMTP is viable via that relay.
        targets = [(configured_host, configured_port, True)]
        for host, port in (
            ('smtp.gmail.com', 587),
            ('smtp.sendgrid.net', 587),
            ('smtp.sendgrid.net', 2525),
            ('smtp-relay.brevo.com', 587),
            ('smtp-relay.brevo.com', 2525),
            ('smtp.mailgun.org', 2525),
        ):
            if (host, port) != (configured_host, configured_port):
                targets.append((host, port, False))

        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            outcomes = list(pool.map(_probe_ipv4, targets))

        port_results = []
        for (host, port, is_cfg), (ok, err, ms) in zip(targets, outcomes):
            port_results.append({
                'label': f'{host}:{port}' + (' (configured)' if is_cfg else ''),
                'ok': ok, 'error': err, 'ms': ms,
            })

        if any(r['ok'] for r in port_results):
            messages.success(request, 'At least one SMTP endpoint is reachable over IPv4 — a relay on that host/port will work.')
        else:
            messages.error(request, 'No SMTP endpoint is reachable over IPv4 — this host blocks all outbound SMTP. SMTP cannot work here; email must be sent from somewhere with SMTP egress.')
        context['results'] = {'type': 'probe', 'host': configured_host, 'items': port_results}

    # ── Customer email tests ────────────────────────────────────────────────
    elif action == 'send_emails':
        to_email = (request.POST.get('to_email') or '').strip()
        kinds = request.POST.getlist('email_kinds')
        if request.POST.get('send_all'):
            kinds = list(EMAIL_TESTS.keys())

        if '@' not in to_email:
            messages.error(request, 'Enter a valid target email address.')
        elif not kinds:
            messages.error(request, 'Select at least one email to send (or use "Send ALL").')
        else:
            apt, err = _resolve_apt()
            if err:
                messages.error(request, err)
            else:
                # In-memory override only — the real customer is never emailed.
                apt.customer_email = to_email

                # SMTP pre-flight so an auth/TLS failure surfaces on the page
                # rather than failing silently for every email. Skipped when
                # SendGrid is the active transport (sends go over HTTPS/443,
                # not SMTP — an SMTP probe would always fail on Railway and
                # wrongly block every send).
                smtp_error = None
                if not getattr(settings, 'SENDGRID_API_KEY', ''):
                    import traceback
                    from django.core.mail import get_connection
                    try:
                        conn = get_connection()
                        conn.open()
                        conn.close()
                    except Exception as exc:
                        smtp_error = f'{type(exc).__name__}: {exc}\n{traceback.format_exc()[-600:]}'

                items = []
                if smtp_error:
                    items.append({'label': 'SMTP connection', 'ok': False, 'error': smtp_error})
                else:
                    for kind in kinds:
                        label, fn = EMAIL_TESTS[kind]
                        try:
                            ok = fn(apt)
                            items.append({
                                'label': label, 'ok': bool(ok),
                                'error': None if ok else 'send returned False (check logs)',
                            })
                        except Exception as exc:
                            items.append({
                                'label': label, 'ok': False,
                                'error': f'{type(exc).__name__}: {exc}',
                            })

                sent_n = sum(1 for i in items if i['ok'])
                fail_n = len(items) - sent_n
                if sent_n:
                    messages.success(request, f'✅ {sent_n} email(s) sent to {to_email}.')
                if fail_n:
                    messages.warning(request, f'⚠️ {fail_n} email(s) failed — see results below.')
                context['results'] = {
                    'type': 'email', 'to': to_email, 'apt': apt.pk, 'items': items,
                }

    # ── WhatsApp follow-up message ──────────────────────────────────────────
    elif action == 'followup_message':
        to_phone = (request.POST.get('to_phone') or '').strip()
        question = request.POST.get('question', 'complete')
        try:
            attempt = max(1, min(4, int(request.POST.get('attempt') or 1)))
        except ValueError:
            attempt = 1
        do_send = bool(request.POST.get('do_send'))

        apt, err = _resolve_apt()
        if err:
            messages.error(request, err)
        else:
            from bot.management.commands.send_followups import Command
            cmd = Command()
            # Use the deterministic template engine (no DeepSeek call) so the
            # preview is repeatable; this is exactly what the cron falls back to.
            gen = cmd._template_message(apt, question, attempt)
            message = gen['message']

            sent = False
            send_error = None
            if do_send:
                if not to_phone:
                    send_error = 'Phone number required to actually send.'
                else:
                    try:
                        whatsapp_api.send_text_message(clean_phone_number(to_phone), message)
                        sent = True
                    except Exception as exc:
                        send_error = f'{type(exc).__name__}: {exc}'

            if sent:
                messages.success(request, f'✅ Follow-up message sent to {to_phone}.')
            elif send_error:
                messages.error(request, f'Failed to send: {send_error}')
            else:
                messages.info(request, 'Generated follow-up preview (not sent).')

            context['results'] = {
                'type': 'followup', 'message': message, 'question': question,
                'attempt': attempt, 'sent': sent, 'error': send_error, 'apt': apt.pk,
            }

    # ── Follow-up cron run ──────────────────────────────────────────────────
    elif action == 'run_cron':
        from django.core.management import call_command
        from io import StringIO
        out = StringIO()
        cmd_args = []
        if request.POST.get('dry_run'):
            cmd_args.append('--dry-run')
        if request.POST.get('force'):
            cmd_args.append('--force')
        try:
            call_command('send_followups', *cmd_args, stdout=out)
            messages.success(request, 'Follow-up cron run completed.')
            context['results'] = {'type': 'cron', 'output': out.getvalue(), 'error': None}
        except Exception as exc:
            import traceback
            messages.error(request, f'Cron run failed: {exc}')
            context['results'] = {
                'type': 'cron', 'output': out.getvalue(),
                'error': f'{type(exc).__name__}: {exc}\n{traceback.format_exc()[-800:]}',
            }

    return render(request, 'bot/pages/followup_test_suite.html', context)


@staff_required
@require_POST
def manual_followup_check(request):
    """Manually trigger follow-up check (for testing/debugging)"""
    from django.core.management import call_command
    from io import StringIO
    
    try:
        out = StringIO()
        call_command('send_followups', stdout=out)
        output = out.getvalue()
        
        messages.success(request, 'Follow-up check completed successfully')
        
        # Show summary
        for line in output.split('\n'):
            if 'Sent:' in line or 'Skipped:' in line or 'Errors:' in line:
                messages.info(request, line.strip())
        
    except Exception as e:
        messages.error(request, f'Error running follow-up check: {str(e)}')
    
    return redirect('followup_dashboard')


@staff_required
@require_POST
def pause_chatbot(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.pause_chatbot()
    # Also write delay signal so automated follow-ups stop immediately
    notes = appointment.internal_notes or ''
    if '[DELAY_SIGNAL]' not in notes:
        appointment.internal_notes = (notes + '\n[DELAY_SIGNAL]').strip()
        appointment.save(update_fields=['internal_notes'])
    _append_admin_note(appointment, f"{request.user.username}: chatbot paused — follow-ups also paused.")
    messages.success(request, 'Chatbot paused. Automated follow-ups also stopped.')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def resume_chatbot(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    appointment.resume_chatbot()
    # Clear delay signal so automated follow-ups can resume
    notes = appointment.internal_notes or ''
    if '[DELAY_SIGNAL]' in notes:
        cleaned = '\n'.join(
            line for line in notes.splitlines()
            if '[DELAY_SIGNAL]' not in line
        ).strip()
        appointment.internal_notes = cleaned
        appointment.save(update_fields=['internal_notes'])
    _append_admin_note(appointment, f"{request.user.username}: chatbot resumed — follow-ups also resumed.")
    messages.success(request, 'Chatbot resumed. Automated follow-ups also restarted.')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def pause_auto_followup(request, pk):
    """Pause automatic follow-ups for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    pause_duration = request.POST.get('pause_duration')
    
    if pause_duration == 'permanent':
        # Pause indefinitely
        appointment.manual_followup_paused = True
        appointment.manual_followup_paused_until = None
        pause_msg = "permanently"
    else:
        # Pause for specified hours
        hours = int(pause_duration)
        pause_until = timezone.now() + timedelta(hours=hours)
        appointment.manual_followup_paused = True
        appointment.manual_followup_paused_until = pause_until
        
        # Human-friendly duration
        if hours == 24:
            pause_msg = "for 24 hours"
        elif hours == 48:
            pause_msg = "for 48 hours"
        elif hours == 168:
            pause_msg = "for 1 week"
        elif hours == 720:
            pause_msg = "for 1 month"
        else:
            pause_msg = f"for {hours} hours"
    
    appointment.save()
    
    messages.success(request, f'⏸️ Automatic follow-ups paused {pause_msg}')
    logger.info(f"Auto follow-ups paused {pause_msg} for appointment {pk} by {request.user.username}")
    
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def resume_auto_followup(request, pk):
    """Resume automatic follow-ups for a specific lead"""
    appointment = get_object_or_404(Appointment, pk=pk)
    
    appointment.manual_followup_paused = False
    appointment.manual_followup_paused_until = None
    appointment.save()
    
    messages.success(request, '▶️ Automatic follow-ups resumed')
    logger.info(f"Auto follow-ups resumed for appointment {pk} by {request.user.username}")
    
    return redirect('appointment_detail', pk=pk)


@staff_required
def send_followup(request, pk):
    """Send MANUAL follow-up message via WhatsApp."""
    from django.http import JsonResponse as _JsonResponse
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'

    appointment = get_object_or_404(Appointment, pk=pk)

    if request.method != 'POST':
        return redirect('appointment_detail', pk=appointment.pk)

    message = request.POST.get('message', '').strip()
    if not message:
        if is_ajax:
            return _JsonResponse({'ok': False, 'error': 'Message cannot be empty.'}, status=400)
        messages.error(request, 'Message cannot be empty')
        return redirect('appointment_detail', pk=appointment.pk)

    try:
        customer_name = appointment.customer_name or "there"
        personalized_message = message.replace('{name}', customer_name)
        clean_phone = clean_phone_number(appointment.phone_number)

        whatsapp_api.send_text_message(clean_phone, personalized_message)

        appointment.add_conversation_message('assistant', f"[MANUAL FOLLOW-UP] {personalized_message}")
        appointment.last_followup_sent = timezone.now()
        appointment.followup_count = (appointment.followup_count or 0) + 1
        appointment.followup_stage = 'responded'
        appointment.save(update_fields=['last_followup_sent', 'followup_count', 'followup_stage'])

        logger.info(f"✅ MANUAL follow-up sent by {request.user.username} to {clean_phone}")

        if is_ajax:
            return _JsonResponse({'ok': True, 'message': f'Message sent to {clean_phone}.'})
        messages.success(request, f'✅ Manual follow-up sent to {clean_phone}!')

    except Exception as e:
        error_msg = str(e)
        logger.error(f"❌ MANUAL follow-up error: {error_msg}")
        if is_ajax:
            return _JsonResponse({'ok': False, 'error': f'Failed to send: {error_msg}'}, status=500)
        messages.error(request, f'Failed to send message: {error_msg}')

    return redirect('appointment_detail', pk=appointment.pk)


@staff_required
@require_POST  
def send_portfolio_to_lead(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    clean_phone = clean_phone_number(appointment.phone_number)
    sent = send_previous_work_photos(clean_phone, appointment)
    if sent:
        messages.success(request, 'Portfolio photos queued for sending.')
    else:
        messages.warning(request, 'No portfolio photos configured.')
    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def send_image_to_lead(request, pk):
    appointment = get_object_or_404(Appointment, pk=pk)
    image_url = request.POST.get('image_url', '').strip()
    caption = request.POST.get('caption', '').strip()

    if not image_url:
        messages.error(request, 'No image URL provided.')
        return redirect('appointment_detail', pk=pk)

    try:
        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_media_message(
            to=clean_phone,
            media_url=image_url,
            media_type='image',
            caption=caption or None,
        )
        appointment.add_conversation_message(
            'assistant',
            f'[IMAGE SENT] URL: {image_url} | Caption: {caption}'
        )
        appointment.last_outbound_at = timezone.now()
        appointment.save(update_fields=['last_outbound_at'])
        messages.success(request, 'Image sent successfully!')
    except Exception as e:
        messages.error(request, f'Failed to send image: {str(e)}')

    return redirect('appointment_detail', pk=pk)


@staff_required
@require_POST
def send_pdf_to_lead(request, pk):
    """Send a PDF picked from the staff member's device to the lead via WhatsApp."""
    appointment = get_object_or_404(Appointment, pk=pk)
    uploaded = request.FILES.get('document')
    caption = request.POST.get('caption', '').strip()

    if not uploaded:
        messages.error(request, 'Please choose a PDF file to send.')
        return redirect('appointment_detail', pk=pk)

    # Validate file type and size (WhatsApp documents max 100 MB)
    name_lower = (uploaded.name or '').lower()
    is_pdf = name_lower.endswith('.pdf') or uploaded.content_type == 'application/pdf'
    if not is_pdf:
        messages.error(request, 'Only PDF files can be sent. Please select a .pdf document.')
        return redirect('appointment_detail', pk=pk)
    if uploaded.size and uploaded.size > 100 * 1024 * 1024:
        messages.error(request, 'PDF is too large to send (100 MB max).')
        return redirect('appointment_detail', pk=pk)

    # Keep the original filename so it shows correctly in WhatsApp
    safe_filename = os.path.basename(uploaded.name) or f'document_{appointment.pk}.pdf'
    if not safe_filename.lower().endswith('.pdf'):
        safe_filename += '.pdf'

    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.pdf', delete=False) as tmp:
            for chunk in uploaded.chunks():
                tmp.write(chunk)
            temp_path = tmp.name

        clean_phone = clean_phone_number(appointment.phone_number)
        whatsapp_api.send_local_document(
            clean_phone,
            temp_path,
            caption=caption or None,
            filename=safe_filename,
        )

        appointment.add_conversation_message(
            'assistant',
            f'[PDF SENT] {safe_filename}' + (f' | Caption: {caption}' if caption else '')
        )
        appointment.last_outbound_at = timezone.now()
        appointment.save(update_fields=['last_outbound_at'])
        messages.success(request, f'PDF "{safe_filename}" sent successfully!')
    except Exception as e:
        messages.error(request, f'Failed to send PDF: {str(e)}')
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass

    return redirect('appointment_detail', pk=pk)


@staff_required
def send_bulk_followup(request):
    """Send manual follow-up to multiple leads at once"""
    if request.method == 'POST':
        lead_ids = request.POST.getlist('lead_ids')
        message_template = request.POST.get('message_template', '').strip()
        pause_duration = int(request.POST.get('pause_duration', 48))
        
        if not lead_ids or not message_template:
            messages.error(request, 'Please select leads and provide a message')
            return redirect('followup_dashboard')
        
        results = {
            'sent': 0,
            'failed': 0,
            'errors': []
        }
        
        for lead_id in lead_ids:
            try:
                appointment = Appointment.objects.get(id=lead_id)
                
                # Personalize message with customer name
                customer_name = appointment.customer_name or "there"
                personalized_message = message_template.replace('{name}', customer_name)
                
                # Send message
                clean_phone = clean_phone_number(appointment.phone_number)
                whatsapp_api.send_text_message(clean_phone, personalized_message)
                
                # Update tracking
                appointment.add_conversation_message('assistant', f"[BULK MANUAL FOLLOW-UP] {personalized_message}")
                appointment.last_followup_sent = timezone.now()
                appointment.last_manual_followup_sent = timezone.now()
                appointment.followup_count = (appointment.followup_count or 0) + 1
                appointment.manual_followup_count = (appointment.manual_followup_count or 0) + 1
                appointment.is_automatic_followup = False
                appointment.followup_stage = 'responded'
                
                # Pause automatic follow-ups
                pause_until = timezone.now() + timedelta(hours=pause_duration)
                appointment.manual_followup_paused = True
                appointment.manual_followup_paused_until = pause_until
                
                appointment.save()
                
                results['sent'] += 1
                
            except Exception as e:
                results['failed'] += 1
                results['errors'].append(f"Lead {lead_id}: {str(e)}")
                logger.error(f"Bulk follow-up error for lead {lead_id}: {str(e)}")
        
        # Show results
        if results['sent'] > 0:
            messages.success(request, f"✅ Sent {results['sent']} manual follow-ups (auto follow-ups paused)")
        if results['failed'] > 0:
            messages.warning(request, f"⚠️ Failed to send {results['failed']} messages")
        
        return redirect('followup_dashboard')
    
    # GET request - show bulk follow-up form
    active_leads = Appointment.objects.filter(
        is_lead_active=True,
        status='pending'
    ).order_by('-last_customer_response')
    
    return render(request, 'bot/pages/bulk_followup.html', {
        'leads': active_leads
    })
