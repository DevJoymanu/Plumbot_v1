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
    '/login', '/logout', '/webhook', '/intake/', '/call/', '/call',
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
