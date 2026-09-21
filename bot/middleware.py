"""
Tenant resolution middleware (Phase 0 — docs/MULTI_TENANT_PLAN.md §3.2).

Pins ``request.tenant`` for every request so downstream views can scope
querysets with ``.for_tenant(request.tenant)`` (the Phase-3 work). Resolution
order:

1. Superuser with a session-selected tenant (the navbar switcher) → that
   tenant. Platform admins can look at any tenant.
2. Authenticated user with a ``TenantMembership`` → their (first) tenant.
   Multi-tenant users are out of scope until a real case exists.
3. Fallback → the ``homebase`` seed tenant. During the transition every staff
   user is Homebase staff; Phase 3 replaces this fallback with explicit
   memberships.

``request.tenant`` may be ``None`` only when the database has no tenants at
all (fresh empty DB) — callers must treat that as "platform not initialised",
never as "all tenants".

Webhook traffic does NOT use this: inbound WhatsApp resolves its tenant by
``phone_number_id`` (Phase 1), not by session.
"""

from django.http import HttpResponseForbidden

from .models import Tenant, TenantMembership

TENANT_SESSION_KEY = 'platform_tenant_slug'

# Paths that don't require a tenant workspace: auth, public surfaces, the
# webhook, the platform console itself, and static assets.
_EXEMPT_PREFIXES = (
    '/login', '/logout', '/webhook', '/intake/', '/call/', '/call', '/site-visit/',
    '/plan-quote/', '/phone-quote/', '/visit/', '/e/',
    '/admin', '/platform', '/static', '/media', '/favicon',
)

_NO_WORKSPACE_HTML = (
    '<div style="font-family:Arial,sans-serif; max-width:480px; margin:80px auto; '
    'text-align:center; color:#0b1c30;">'
    '<h1 style="color:#006591; font-size:22px;">No workspace assigned</h1>'
    '<p style="color:#5b7285;">Your login works, but it is not linked to a '
    'business workspace yet. Ask your platform administrator to add you to '
    'your company from the console.</p>'
    '<p><a href="/logout/" style="color:#006591;">Log out</a></p></div>'
)


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tenant = self._resolve(request)
        # Separation rule: an authenticated non-superuser with NO membership
        # gets a clear block, never a silent fallback into homebase's data.
        user = getattr(request, 'user', None)
        if (
            user is not None and user.is_authenticated and not user.is_superuser
            and request.tenant is None
            and not request.path.startswith(_EXEMPT_PREFIXES)
        ):
            return HttpResponseForbidden(_NO_WORKSPACE_HTML)
        return self.get_response(request)

    def _resolve(self, request):
        user = getattr(request, 'user', None)
        if user is not None and user.is_authenticated:
            if user.is_superuser:
                slug = request.session.get(TENANT_SESSION_KEY)
                if slug:
                    tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
                    if tenant is not None:
                        return tenant
                # Platform admin's default LENS (not a membership): homebase.
                return Tenant.objects.filter(slug='homebase').first()
            membership = (
                TenantMembership.objects
                .filter(user=user, tenant__is_active=True)
                .select_related('tenant')
                .order_by('pk')
                .first()
            )
            if membership is not None:
                return membership.tenant
            return None  # staff without a keycard — middleware blocks above
        return Tenant.objects.filter(slug='homebase').first()


class FrameContextMiddleware:
    """Keeps a redirect answered inside one of the app's iframes IN the frame.

    WHAT: when a request carries the frame context (`frame=1`, plus `hidetabs`
    / `source` - see `context_processors.frame_params`) and the response is a
    redirect to a local path, the same context is added to that path.

    WHY: a framed page's links and forms are kept in frame mode (server-side by
    `frame_query`, and by the script in `frame_links.html`), but most actions
    answer with a redirect built by `reverse()`, which knows nothing about the
    frame. Delete a quote from its framed view page and the default landing -
    the lead page - came back WITH its own sidebar and bottom bar inside the
    pane: the second nav bar, one step later. Fixing that at every redirect
    site would be forty edits that the next view forgets; this is one.

    HOW: only local paths (a single leading "/") are touched, so an off-site
    or protocol-relative Location is never rewritten; a key the Location
    already names keeps its own value. Pinned by OneNavBarInsideTheFrameTests.
    """

    _REDIRECTS = {301, 302, 303, 307, 308}

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if response.status_code not in self._REDIRECTS:
            return response
        location = response.get('Location') or ''
        if not location.startswith('/') or location.startswith('//'):
            return response
        from .context_processors import with_frame
        framed = with_frame(location, request)
        if framed != location:
            response['Location'] = framed
        return response
