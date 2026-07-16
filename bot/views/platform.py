"""
Platform (developer) console — docs/MULTI_TENANT_PLAN.md §3.4.

Superuser-only operator screens: tenant list, create/deactivate,
impersonation (the session tenant switcher), and the per-tenant config
editor (profile + price sheet). The onboarding wizard and intake
draft→approve flow build on these (Phase 3.3/5).
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.db.models import Count
from django.forms import modelformset_factory
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from django.utils import timezone

from ..middleware import TENANT_SESSION_KEY
from ..models import Tenant, TenantIntake, TenantPriceItem, TenantProfile


def _superuser(user):
    return user.is_active and user.is_superuser


superuser_required = user_passes_test(_superuser)


@require_POST
@superuser_required
def switch_tenant(request):
    """Superuser-only: pin a tenant on the session (impersonation — the
    dashboard then shows that tenant's world)."""
    slug = request.POST.get('tenant', '')
    tenant = Tenant.objects.filter(slug=slug, is_active=True).first()
    if tenant is None:
        messages.error(request, 'Unknown or inactive tenant.')
    else:
        request.session[TENANT_SESSION_KEY] = tenant.slug
        messages.success(request, f'Now viewing tenant: {tenant.name}')
    return redirect(request.POST.get('next') or 'dashboard')


@superuser_required
def platform_console(request):
    """Tenant list — the console home."""
    tenants = (
        Tenant.objects
        .annotate(lead_count=Count('appointments'))
        .prefetch_related('whatsapp_channels')
        .order_by('name')
    )
    rows = []
    for tenant in tenants:
        channel = next(iter(tenant.whatsapp_channels.all()), None)
        rows.append({
            'tenant': tenant,
            'lead_count': tenant.lead_count,
            'channel': channel,
            'has_profile': TenantProfile.objects.filter(tenant=tenant).exists(),
        })
    return render(request, 'bot/pages/platform_console.html', {
        'rows': rows,
        'active_nav': 'platform',
        'current_tenant': getattr(request, 'tenant', None),
    })


@require_POST
@superuser_required
def platform_create_tenant(request):
    name = (request.POST.get('name') or '').strip()
    if not name:
        messages.error(request, 'A tenant needs a name.')
        return redirect('platform_console')
    slug = slugify(request.POST.get('slug') or name)[:60]
    if Tenant.objects.filter(slug=slug).exists():
        messages.error(request, f'Slug "{slug}" is already taken.')
        return redirect('platform_console')
    if Tenant.objects.filter(name__iexact=name).exists():
        messages.error(request, f'A tenant named "{name}" already exists.')
        return redirect('platform_console')
    tenant = Tenant.objects.create(name=name, slug=slug)
    TenantProfile.objects.create(tenant=tenant)  # blank — nullability rule
    cloned = _clone_golden_pack(tenant)
    messages.success(
        request,
        f'Tenant "{name}" created with {cloned} golden scenarios. '
        'Fill in their config, then run the pack green before go-live.')
    return redirect('platform_tenant_config', slug=tenant.slug)


def _clone_golden_pack(tenant) -> int:
    """Copy homebase's scenario pack to a new tenant (Phase 5): instant
    regression coverage — the pack must run green against the tenant's own
    config before they go live (§12 hard gate)."""
    from ..models import TestScenario
    homebase = Tenant.objects.filter(slug='homebase').first()
    if homebase is None or tenant.pk == homebase.pk:
        return 0
    cloned = 0
    for scenario in TestScenario.objects.filter(tenant=homebase, is_active=True):
        _, created = TestScenario.objects.get_or_create(
            tenant=tenant, name=scenario.name,
            defaults=dict(
                category=scenario.category,
                description=scenario.description,
                content=scenario.content,
            ),
        )
        cloned += int(created)
    return cloned


@require_POST
@superuser_required
def platform_toggle_tenant(request, slug):
    tenant = get_object_or_404(Tenant, slug=slug)
    if tenant.slug == 'homebase' and tenant.is_active:
        messages.error(request, 'Refusing to deactivate the homebase tenant from the console.')
        return redirect('platform_console')
    tenant.is_active = not tenant.is_active
    tenant.save(update_fields=['is_active'])
    state = 'activated' if tenant.is_active else 'deactivated'
    messages.success(request, f'Tenant "{tenant.name}" {state}.')
    return redirect('platform_console')


# ── Staff & logins (checklist 6.6) ───────────────────────────────────────────

@superuser_required
@require_POST
def platform_add_staff(request, slug):
    """Create a dashboard login for a tenant: Django user (is_staff, never
    superuser) + TenantMembership with the chosen role."""
    from django.contrib.auth import get_user_model
    from ..models import TenantMembership
    tenant = get_object_or_404(Tenant, slug=slug)
    username = (request.POST.get('username') or '').strip()
    email = (request.POST.get('email') or '').strip()
    password = request.POST.get('password') or ''
    role = request.POST.get('role') if request.POST.get('role') in ('owner', 'staff') else 'staff'
    if not username or len(password) < 8:
        messages.error(request, 'Username and a password of at least 8 characters are required.')
        return redirect('platform_tenant_config', slug=slug)
    User = get_user_model()
    if User.objects.filter(username__iexact=username).exists():
        messages.error(request, f'Username "{username}" is already taken.')
        return redirect('platform_tenant_config', slug=slug)
    user = User.objects.create_user(
        username=username, email=email, password=password, is_staff=True)
    TenantMembership.objects.create(user=user, tenant=tenant, role=role)
    messages.success(
        request,
        f'Login created: {username} ({role}) for {tenant.name}. '
        'Share the temporary password securely and ask them to change it in Profile.')
    return redirect('platform_tenant_config', slug=slug)


def _tenant_member_or_404(tenant, user_id):
    from ..models import TenantMembership
    membership = get_object_or_404(
        TenantMembership.objects.select_related('user'),
        tenant=tenant, user_id=user_id)
    if membership.user.is_superuser:
        raise Http404  # platform admins are never managed from here
    return membership


@superuser_required
@require_POST
def platform_toggle_staff(request, slug, user_id):
    tenant = get_object_or_404(Tenant, slug=slug)
    membership = _tenant_member_or_404(tenant, user_id)
    user = membership.user
    user.is_active = not user.is_active
    user.save(update_fields=['is_active'])
    state = 'reactivated' if user.is_active else 'deactivated'
    messages.success(request, f'{user.username} {state}.')
    return redirect('platform_tenant_config', slug=slug)


@superuser_required
@require_POST
def platform_reset_staff_password(request, slug, user_id):
    tenant = get_object_or_404(Tenant, slug=slug)
    membership = _tenant_member_or_404(tenant, user_id)
    password = request.POST.get('password') or ''
    if len(password) < 8:
        messages.error(request, 'New password must be at least 8 characters.')
        return redirect('platform_tenant_config', slug=slug)
    user = membership.user
    user.set_password(password)
    user.save(update_fields=['password'])
    messages.success(
        request,
        f'Password reset for {user.username}. Share it securely; they can change it in Profile.')
    return redirect('platform_tenant_config', slug=slug)


# ── Owner intake (decision #2: draft → admin verify → approve) ───────────────

INTAKE_PROFILE_FIELDS = [
    # (field, label, help)
    ('plumber_name', 'Lead plumber name', 'The person customers can ask for.'),
    ('plumber_contact', 'Plumber direct number', 'e.g. +2637...'),
    ('business_whatsapp', 'Business WhatsApp number', 'Where email buttons point. Can be the same as above.'),
    ('location_area', 'Suburb / area', 'e.g. Hatfield'),
    ('location_city', 'City', 'e.g. Harare'),
    ('email_from_name', 'Email sender name', 'Whose name customer emails come from.'),
]

INTAKE_FAQ_TOPICS = [
    ('payment', 'How do customers pay?', 'e.g. Cash, EcoCash, and bank transfer.'),
    ('free_quote', 'Is the quote/visit free?', 'Your wording for the free-visit promise.'),
    ('job_duration', 'How long do jobs take?', 'Typical durations for small vs big jobs.'),
    ('services', 'What do you handle?', 'One sentence listing your services.'),
]


@superuser_required
@require_POST
def platform_new_intake(request, slug):
    tenant = get_object_or_404(Tenant, slug=slug)
    intake = TenantIntake.objects.create(tenant=tenant)
    link = request.build_absolute_uri(f"/intake/{intake.token}/")
    messages.success(
        request,
        f'Intake link for {tenant.name} (send it to the owner): {link}')
    return redirect('platform_tenant_config', slug=tenant.slug)


def intake_form(request, token):
    """PUBLIC (token-gated) owner intake form. Submissions are drafts — the
    admin reviews and approves before anything goes live."""
    intake = get_object_or_404(TenantIntake, token=token)
    if intake.status in ('approved', 'rejected'):
        return render(request, 'bot/pages/intake_done.html',
                      {'intake': intake}, status=200)

    if request.method == 'POST':
        data = {'profile': {}, 'faq_facts': {}, 'prices': [], 'hours': {}, 'notes': ''}
        for field, _label, _help in INTAKE_PROFILE_FIELDS:
            data['profile'][field] = (request.POST.get(field) or '').strip()
        for topic, _q, _h in INTAKE_FAQ_TOPICS:
            value = (request.POST.get(f'faq_{topic}') or '').strip()
            if value:
                data['faq_facts'][topic] = value
        data['hours'] = {
            'days': (request.POST.get('hours_days') or '').strip(),
            'open': (request.POST.get('hours_open') or '').strip(),
            'close': (request.POST.get('hours_close') or '').strip(),
        }
        data['excluded_areas'] = [
            a.strip().lower() for a in
            (request.POST.get('excluded_areas') or '').split(',') if a.strip()
        ]
        # Price rows arrive as parallel arrays from the dynamic table.
        labels = request.POST.getlist('price_label')
        families = request.POST.getlist('price_family')
        supplies = request.POST.getlist('price_supply')
        labours = request.POST.getlist('price_labour')
        allins = request.POST.getlist('price_allin')
        for i, label in enumerate(labels):
            if not label.strip():
                continue
            data['prices'].append({
                'label': label.strip(),
                'family': (families[i] if i < len(families) else '').strip().lower() or 'other',
                'supply': (supplies[i] if i < len(supplies) else '').strip(),
                'labour': (labours[i] if i < len(labours) else '').strip(),
                'allin': (allins[i] if i < len(allins) else '').strip(),
            })
        data['notes'] = (request.POST.get('notes') or '').strip()
        intake.data = data
        intake.status = 'submitted'
        intake.submitted_at = timezone.now()
        intake.save(update_fields=['data', 'status', 'submitted_at'])
        return render(request, 'bot/pages/intake_done.html', {'intake': intake})

    return render(request, 'bot/pages/intake_form.html', {
        'intake': intake,
        'tenant': intake.tenant,
        'profile_fields': INTAKE_PROFILE_FIELDS,
        'faq_topics': INTAKE_FAQ_TOPICS,
        'existing': intake.data or {},
    })


def _to_decimal_or_none(raw):
    from decimal import Decimal, InvalidOperation
    raw = (raw or '').replace('US$', '').replace('$', '').replace(',', '').strip()
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        return None


@superuser_required
def platform_review_intake(request, pk):
    """Review a submitted intake; approve applies it to the live config."""
    intake = get_object_or_404(TenantIntake, pk=pk)
    tenant = intake.tenant

    if request.method == 'POST':
        decision = request.POST.get('decision')
        intake.review_note = (request.POST.get('review_note') or '').strip()
        if decision == 'approve' and intake.status == 'submitted':
            profile, _ = TenantProfile.objects.get_or_create(tenant=tenant)
            data = intake.data or {}
            for field, value in (data.get('profile') or {}).items():
                if value and hasattr(profile, field):
                    setattr(profile, field, value)
            hours = data.get('hours') or {}
            if hours.get('days') and hours.get('open') and hours.get('close'):
                profile.business_hours = hours
            if data.get('excluded_areas'):
                profile.excluded_areas = data['excluded_areas']
            merged = dict(profile.faq_facts or {})
            merged.update(data.get('faq_facts') or {})
            profile.faq_facts = merged
            profile.save()
            for row in data.get('prices') or []:
                TenantPriceItem.objects.update_or_create(
                    tenant=tenant, family=row.get('family') or 'other',
                    variant=row.get('variant', ''),
                    defaults=dict(
                        label=row.get('label', ''),
                        supply=_to_decimal_or_none(row.get('supply')),
                        labour=_to_decimal_or_none(row.get('labour')),
                        allin=_to_decimal_or_none(row.get('allin')),
                    ),
                )
            intake.status = 'approved'
            intake.reviewed_at = timezone.now()
            intake.save(update_fields=['status', 'review_note', 'reviewed_at'])
            messages.success(request, f'Intake approved and applied to {tenant.name}.')
            return redirect('platform_tenant_config', slug=tenant.slug)
        if decision == 'reject':
            intake.status = 'rejected'
            intake.reviewed_at = timezone.now()
            intake.save(update_fields=['status', 'review_note', 'reviewed_at'])
            messages.success(request, 'Intake rejected — nothing was applied.')
            return redirect('platform_console')
        messages.error(request, 'No valid decision.')

    return render(request, 'bot/pages/platform_intake_review.html', {
        'intake': intake,
        'tenant': tenant,
        'active_nav': 'platform',
    })


class TenantProfileForm(forms.ModelForm):
    class Meta:
        model = TenantProfile
        fields = [
            'plumber_name', 'plumber_contact', 'business_whatsapp',
            'location_line', 'location_area', 'location_city',
            'business_hours', 'timezone_name', 'excluded_areas',
            'currency', 'packages', 'faq_facts', 'scripts',
            'licensed_claim_enabled', 'email_from_name', 'email_sender',
        ]
        widgets = {
            field: forms.Textarea(attrs={'rows': 3})
            for field in ('business_hours', 'excluded_areas', 'packages', 'faq_facts', 'scripts')
        }


PriceItemFormSet = modelformset_factory(
    TenantPriceItem,
    fields=['family', 'variant', 'label', 'short_label',
            'supply', 'labour', 'flat', 'allin', 'sort_order', 'is_active'],
    extra=1, can_delete=True,
)


@superuser_required
def platform_tenant_config(request, slug):
    """Per-tenant config editor: profile + price sheet. The ONLY place tenant
    config is edited until the owner intake flow ships (decision #2)."""
    tenant = get_object_or_404(Tenant, slug=slug)
    profile, _ = TenantProfile.objects.get_or_create(tenant=tenant)
    prices_qs = TenantPriceItem.objects.filter(tenant=tenant)

    if request.method == 'POST':
        form = TenantProfileForm(request.POST, instance=profile)
        formset = PriceItemFormSet(request.POST, queryset=prices_qs)
        if form.is_valid() and formset.is_valid():
            form.save()
            items = formset.save(commit=False)
            for item in items:
                item.tenant = tenant
                item.save()
            for item in formset.deleted_objects:
                item.delete()
            from ..whatsapp_cloud_api import invalidate_client_cache
            invalidate_client_cache()
            messages.success(request, f'Config saved for {tenant.name}.')
            return redirect('platform_tenant_config', slug=tenant.slug)
        messages.error(request, 'Fix the errors below — nothing was saved.')
    else:
        form = TenantProfileForm(instance=profile)
        formset = PriceItemFormSet(queryset=prices_qs)

    channel = tenant.whatsapp_channels.first()
    return render(request, 'bot/pages/platform_tenant_config.html', {
        'tenant': tenant,
        'form': form,
        'formset': formset,
        'channel': channel,
        'intakes': tenant.intakes.all()[:10],
        'staff': tenant.memberships.select_related('user').order_by('user__username'),
        'active_nav': 'platform',
    })
