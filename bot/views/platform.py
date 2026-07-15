"""
Platform (developer) console views — docs/MULTI_TENANT_PLAN.md §3.4.

Phase 0 ships only the tenant switcher; the console proper (tenant CRUD,
impersonation, health boards) lands in Phase 3.
"""

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from ..middleware import TENANT_SESSION_KEY
from ..models import Tenant


def _superuser(user):
    return user.is_active and user.is_superuser


@require_POST
@user_passes_test(_superuser)
def switch_tenant(request):
    """Superuser-only: pin a tenant on the session (the navbar switcher).
    Non-superusers never see the control and are rejected here anyway."""
    slug = request.POST.get('tenant', '')
    tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
    if tenant is None:
        messages.error(request, 'Unknown or inactive tenant.')
    else:
        request.session[TENANT_SESSION_KEY] = tenant.slug
        messages.success(request, f'Now viewing tenant: {tenant.name}')
    return redirect(request.POST.get('next') or 'dashboard')
