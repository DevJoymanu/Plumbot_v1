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

from .models import Tenant, TenantMembership

TENANT_SESSION_KEY = 'platform_tenant_slug'


class TenantMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tenant = self._resolve(request)
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
            membership = (
                TenantMembership.objects
                .filter(user=user, tenant__is_active=True)
                .select_related('tenant')
                .order_by('pk')
                .first()
            )
            if membership is not None:
                return membership.tenant
        return Tenant.objects.filter(slug='homebase').first()
