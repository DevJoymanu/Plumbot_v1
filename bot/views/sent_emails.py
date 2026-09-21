"""Sent-Emails dashboard: the record of post-visit / quote / site-visit mail.

Three pieces:
  • track_email_open  — the public first-party 1x1 pixel that stamps opens.
  • sent_emails_queryset / sent_email_filters — scoped list feeding the
    "Sent Emails" tab on the Follow-ups dashboard.
  • sent_email_detail — the full stored HTML, delivery status and open state.

Scoping mirrors the rest of the dashboard: rows are limited to the current
workspace tenant, so one tenant never sees another's mail.
"""

import re

from django.http import HttpResponse, Http404
from django.shortcuts import render, get_object_or_404
from django.views.decorators.http import require_GET
from django.views.decorators.csrf import csrf_exempt

from ..decorators import staff_required
from ..models import SentEmail


# Strip our own tracking pixel so viewing the email in the dashboard never
# records a false "open" (the sandboxed iframe still loads images).
_PIXEL_IMG_RE = re.compile(r'<img[^>]*?/e/[0-9a-fA-F-]+\.gif[^>]*?>', re.IGNORECASE)


def _strip_tracking_pixel(html):
    return _PIXEL_IMG_RE.sub('', html or '')


# A 43-byte fully transparent 1x1 GIF.
_PIXEL = (
    b'GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!\xf9\x04\x01'
    b'\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;'
)


# Category groups the dashboard filters by, matching the owner's ask
# (post-visit / quote / site-visit). 'all' = every dashboard category.
CATEGORY_GROUPS = {
    'post_visit': [
        SentEmail.Category.POST_VISIT_CONFIRM,
        SentEmail.Category.POST_VISIT_ASK,
        SentEmail.Category.POST_VISIT_HANDBACK,
    ],
    'quote': [
        SentEmail.Category.QUOTE_SENT,
        SentEmail.Category.QUOTE_FOLLOWUP,
        SentEmail.Category.PLAN_QUOTE_PLUMBER,
    ],
    'site_visit': [
        SentEmail.Category.VISIT_CHECKIN,
        SentEmail.Category.SITE_VISIT_FORM,
    ],
    # Everything the follow-up machinery sends the lead between first contact
    # and the visit. Without this group the delay re-engagements, reminders,
    # booking confirmations and staff-queued emails -- the bulk of what
    # actually goes out -- had no chip to select them.
    'followup': [
        SentEmail.Category.FOLLOWUP,
        SentEmail.Category.DELAY,
        SentEmail.Category.REMINDER,
        SentEmail.Category.BOOKING,
    ],
    'plumber': [
        SentEmail.Category.PLUMBER_ALERT,
    ],
}


def _pixel_response():
    resp = HttpResponse(_PIXEL, content_type='image/gif')
    # Never let a proxy cache the pixel, or a second open would not register.
    resp['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp['Pragma'] = 'no-cache'
    resp['Content-Length'] = str(len(_PIXEL))
    return resp


@csrf_exempt
@require_GET
def track_email_open(request, token):
    """Public open pixel. Always returns the gif — a bad/again token must never
    error, it just does not record. Token-only, no session (it is fetched by a
    mail client), and exempt in TenantMiddleware."""
    try:
        row = SentEmail.objects.filter(open_token=token).first()
        if row is not None:
            row.mark_opened()
    except Exception:
        pass
    return _pixel_response()


def sent_emails_queryset(request):
    """Rows for the current workspace, newest first."""
    tenant = getattr(request, 'tenant', None)
    qs = SentEmail.objects.select_related('appointment')
    if tenant is not None:
        qs = qs.filter(tenant=tenant)
    return qs


def sent_email_filters(request):
    """Resolve the ?se_group / ?se_status filters and return
    (queryset, active_group, active_status).

    The default is ALL email for the workspace. It used to be the narrow
    post-visit/quote/site-visit set, which is why the tab read as empty: nearly
    every row a real workspace holds is a delay re-engagement, a reminder, a
    booking confirmation or a plumber alert, and none of those were in that set.
    A tab called "Sent Emails" that shows a third of the email we sent is worse
    than no tab, because it gets believed -- the narrow set is still one chip.
    """
    group = (request.GET.get('se_group') or 'all').strip()
    status = (request.GET.get('se_status') or '').strip()

    qs = sent_emails_queryset(request)
    if group in CATEGORY_GROUPS:
        qs = qs.filter(category__in=CATEGORY_GROUPS[group])
    else:  # 'dashboard' = the post-visit/quote/site-visit set; 'all' = no filter
        group = group if group == 'dashboard' else 'all'
        if group == 'dashboard':
            qs = qs.filter(category__in=list(SentEmail.DASHBOARD_CATEGORIES))

    if status in dict(SentEmail.Status.choices):
        qs = qs.filter(status=status)
    return qs, group, status


@staff_required
@require_GET
def sent_email_detail(request, pk):
    """Full content + delivery/open state for one sent email, scoped to the
    workspace so a pk from another tenant 404s.

    ?raw=1 returns just the stored HTML body (pixel stripped) for the sandboxed
    preview iframe; otherwise the detail page with metadata and status.
    """
    row = get_object_or_404(sent_emails_queryset(request), pk=pk)
    if request.GET.get('raw'):
        body = _strip_tracking_pixel(row.html_body) or (
            '<p style="font-family:sans-serif;color:#666;padding:16px;">'
            'No HTML body was stored for this email.</p>')
        return HttpResponse(body, content_type='text/html; charset=utf-8')
    return render(request, 'bot/pages/sent_email_detail.html', {
        'active_nav': 'followups',
        'email': row,
    })
