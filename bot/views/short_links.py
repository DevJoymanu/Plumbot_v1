"""
The public forwarder behind a business's short plumber link:
https://wa.<business domain>/q/<code> -> https://wa.me/<plumber>?text=<per-lead message>

WHAT: decodes the signed code (bot/short_links.py), checks the request came in
on THAT lead's own tenant's short-link domain, rebuilds the wa.me link from the
lead's current details and answers 302.
WHY here and public: the lead taps it from WhatsApp with no session. The code
is signed, so it cannot be walked to other leads, and a code opened on another
tenant's domain is a 404 (a Barmak lead's link never resolves on Homebase's
domain, or the reverse).
HOW it stays safe for a GET: it reads, never writes. WhatsApp's link-preview
fetcher requests it too, so a click log here would count previews as taps.
Pinned by ShortLinkTests.
"""

from django.http import Http404, HttpResponseRedirect
from django.views.decorators.http import require_GET

from ..models import Appointment
from ..plumber_link import long_quote_link
from ..short_links import decode
from ..tenant_config import get_config


@require_GET
def plumber_link_redirect(request, code):
    lead_id = decode(code)
    if lead_id is None:
        raise Http404
    appointment = Appointment.objects.filter(pk=lead_id).select_related('tenant').first()
    if appointment is None:
        raise Http404
    domain = get_config(appointment.tenant).short_link_domain
    if not domain or request.get_host().split(':', 1)[0].lower() != domain:
        raise Http404
    target = long_quote_link(appointment)
    if not target:
        raise Http404
    response = HttpResponseRedirect(target)
    # The message is rebuilt from the lead's current details on every tap, so
    # nothing may cache an old one.
    response['Cache-Control'] = 'no-store'
    return response
