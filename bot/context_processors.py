from django.db.utils import OperationalError, ProgrammingError


NAV_MAP = {
    "dashboard": "dashboard",
    "appointments_list": "appointments",
    "appointment_detail": "appointments",
    "priority_leads": "leads",
    "update_priority_lead_card": "leads",
    "followup_dashboard": "followups",
    "manual_followup_check": "followups",
    "job_appointments_list": "jobs",
    "schedule_job": "jobs",
    "reschedule_job": "jobs",
    "gallery": "gallery",
    "offer": "offer",
    "quotation_templates_list": "templates",
    "quotation_template_detail": "templates",
    "create_quotation_template": "templates",
    "edit_quotation_template": "templates",
    "duplicate_template": "templates",
    "delete_template": "templates",
    "standalone_quotation": "new_quote",
    "create_quotation_standalone": "new_quote",
    # The quote screens themselves: the list highlighted nothing on an
    # editor or a client copy, so working on a quote left the sidebar blank.
    "quotations_list": "quotations",
    "create_quotation": "quotations",
    "view_quotation": "quotations",
    "preview_quotation": "quotations",
    "edit_quotation": "quotations",
    "quotation_whatsapp_handoff": "quotations",
    "settings": "settings",
    "calendar_settings": "settings",
    "ai_settings": "settings",
    "profile": "profile",
    "change_password": "profile",
}

# Quotes, templates and the tenant's own offer are one job - raising and
# pricing work - and share ONE sidebar item. This set is the single answer to
# "does this page belong to that group?", read by the desktop accordion (which
# renders open on its own pages) and by the mobile More button.
QUOTES_NAV_GROUP = {"quotations", "new_quote", "templates", "offer"}


def in_app_frame(request) -> bool:
    """Is this page being rendered INSIDE one of the app's own iframes?

    The conversations workspace and the follow-ups dashboard both load a whole
    page into a pane and mark it with `frame=1`. That page already sits inside
    the shell's chrome, so rendering its own sidebar and bottom bar puts a
    SECOND nav bar on screen - which is what every quote editor opened from a
    lead's Quotes tab did, because the editors extend the full layout and
    nothing told them where they were.

    ONE reader for the question, so a page cannot answer it differently from
    the layout that acts on it. `base_template` (appointment detail, the job
    screens) is the older answer to the same question and stays as it is: it
    swaps the layout outright for pages that also need the panel's fixed
    height.
    """
    return getattr(request, "GET", {}).get("frame") == "1"


# The query keys that describe WHERE a framed page is being shown, and that a
# page reached from inside the frame must keep: `frame` drops the chrome,
# `hidetabs` drops the lead page's own tab bar (the conversations workspace
# draws its own), `source` picks the lead page's side panel. Carrying `frame`
# alone got the nav right and then put a second TAB bar in the conversations
# pane the moment a quote handed back to the lead.
FRAME_CONTEXT_KEYS = ("frame", "hidetabs", "source")


def frame_params(request) -> dict:
    """The frame context this request was made in, as {key: value}.

    Empty outside a frame, so every caller can append it unconditionally.
    Read by `frame_query` (links rendered on the server), the redirect step in
    `FrameContextMiddleware`, `quote_return_url`, and - via `frame_query` - the
    script that keeps in-frame navigation in the frame.
    """
    if not in_app_frame(request):
        return {}
    get = request.GET
    return {key: get[key] for key in FRAME_CONTEXT_KEYS if get.get(key)}


def with_frame(url: str, request) -> str:
    """`url` with this request's frame context added, keys it already has kept.

    A local URL only - callers pass paths they built or validated. A url that
    already names a key keeps its own value: it knows better where it goes.
    """
    params = frame_params(request)
    if not params or not url:
        return url
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key, value in params.items():
        query.setdefault(key, value)
    return urlunsplit(parts._replace(query=urlencode(query)))


def _frame_query(request) -> str:
    from urllib.parse import urlencode
    params = frame_params(request)
    return ("?" + urlencode(params)) if params else ""


def plumbot_shell(request):
    match = getattr(request, "resolver_match", None)
    url_name = getattr(match, "url_name", "") or ""

    from .decorators import is_platform_owner

    active_nav = NAV_MAP.get(url_name, "")

    counts = {
        "active_nav": active_nav,
        "nav_group_quotes": active_nav in QUOTES_NAV_GROUP,
        "hot_lead_count": 0,
        "pending_followup_count": 0,
        # Templates gate the delete-conversation control on this, matching
        # @owner_required on the view. Superuser is deliberately NOT enough.
        "is_platform_owner": is_platform_owner(getattr(request, "user", None)),
        # Read by base.html, which then renders neither nav. The page keeps its
        # own scroller and layout - only the chrome the frame already has comes
        # off.
        "chromeless": in_app_frame(request),
        # Appended to server-rendered links that leave a framed page for a
        # whole page of its own, so that page stays chromeless too. Empty
        # outside a frame. Global rather than per view: the lead page and the
        # quote pages both need it and must not build it two ways.
        "frame_query": _frame_query(request),
    }

    try:
        # Use the same canonical definitions as the dashboard so the nav badges
        # match every other surface: hot leads = priority leads from the last week
        # that haven't booked; follow-ups = leads actually DUE to be contacted now
        # (mirrors the send_followups cron), not every follow_up_status='pending'.
        from .views.dashboard import priority_lead_count, _due_followup_leads
        _tenant = getattr(request, "tenant", None)
        counts["hot_lead_count"] = priority_lead_count(_tenant)
        counts["pending_followup_count"] = len(_due_followup_leads(tenant=_tenant))
    except (OperationalError, ProgrammingError):
        pass

    # Tenant switcher (superusers only — plan §3.4). request.tenant is pinned
    # by TenantMiddleware; the switcher lists the alternatives.
    user = getattr(request, "user", None)
    if user is not None and getattr(user, "is_superuser", False):
        try:
            from .models import Tenant
            counts["platform_tenants"] = list(
                Tenant.objects.filter(is_active=True).order_by("name")
            )
        except (OperationalError, ProgrammingError):
            pass

    return counts
