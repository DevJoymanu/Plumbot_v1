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
from ..tenant_config import blank_priced_catalog


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


# ── Tenant deletion (explicit off-boarding; password-gated) ──────────────────

import os as _os

# Second factor for destructive console actions — separate from the admin's
# login. Override via env; never logged.
PLATFORM_DELETE_PASSWORD = _os.environ.get('PLATFORM_DELETE_PASSWORD', 'Jones123#')


@superuser_required
@require_POST
def platform_delete_tenant(request, slug):
    """Permanently delete a tenant and ALL its business data. Gated by the
    deletion password (asked for at click time) on top of the superuser
    session. Homebase is never deletable. Business tables PROTECT against
    accidental cascade, so this deletes them explicitly, eyes open."""
    tenant = get_object_or_404(Tenant, slug=slug)
    if tenant.slug == 'homebase':
        messages.error(request, 'Homebase cannot be deleted.')
        return redirect('platform_console')
    if request.POST.get('delete_password') != PLATFORM_DELETE_PASSWORD:
        messages.error(request, 'Wrong deletion password — nothing was deleted.')
        return redirect('platform_console')

    from django.db import transaction

    from ..models import (
        Appointment, Job, Quotation, QuotationTemplate, ScheduledFollowup,
        ScheduledReminder, ServiceArea, TenantMembership, TestScenario,
        WhatsAppInboundEvent,
    )
    name = tenant.name
    with transaction.atomic():
        member_ids = list(
            TenantMembership.objects.filter(tenant=tenant).values_list('user_id', flat=True))
        # PROTECTed business rows first (appointment children cascade with it).
        Appointment.objects.filter(tenant=tenant).delete()
        for model in (Job, Quotation, ScheduledFollowup, ScheduledReminder,
                      QuotationTemplate, ServiceArea, TestScenario, WhatsAppInboundEvent):
            model.objects.filter(tenant=tenant).delete()
        tenant.delete()  # cascades profile/channels/prices/portfolio/intakes/memberships
        # Staff whose ONLY workspace this was: deactivate (keep the audit trail).
        from django.contrib.auth import get_user_model
        User = get_user_model()
        for user_id in member_ids:
            if not TenantMembership.objects.filter(user_id=user_id).exists():
                User.objects.filter(
                    pk=user_id, is_superuser=False).update(is_active=False)
    messages.success(request, f'Tenant "{name}" and all its data were permanently deleted.')
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
    ('plumber_contact', 'Plumber direct number', 'Customers can call this line for anything technical.'),
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


def _parse_intake_post(request) -> dict:
    """Normalise the wizard's POST into the intake draft shape (v2)."""
    import json as _json
    data = {'profile': {}, 'faq_facts': {}, 'prices': [], 'hours': {},
            'payment_methods': [], 'services': [], 'durations': {},
            'photos': [], 'notes': '', 'pasted_price_list': ''}
    for field, _label, _help in INTAKE_PROFILE_FIELDS:
        data['profile'][field] = (request.POST.get(field) or '').strip()
    free_quote = (request.POST.get('faq_free_quote') or '').strip()
    if free_quote:
        data['faq_facts']['free_quote'] = free_quote
    data['hours'] = {
        'days': [d for d in request.POST.getlist('days') if d],
        'open': (request.POST.get('hours_open') or '').strip(),
        'close': (request.POST.get('hours_close') or '').strip(),
    }
    data['excluded_areas'] = [
        a.strip().lower() for a in
        (request.POST.get('excluded_areas') or '').split(',') if a.strip()
    ]
    data['payment_methods'] = [p for p in request.POST.getlist('payment') if p]
    other_pay = (request.POST.get('payment_other') or '').strip()
    if other_pay:
        data['payment_methods'].append(other_pay)
    data['services'] = [s for s in request.POST.getlist('services') if s]
    other_service = (request.POST.get('service_other') or '').strip()
    if other_service:
        data['services'].append(other_service)
    data['durations'] = {
        'small': (request.POST.get('duration_small') or '').strip(),
        'big': (request.POST.get('duration_big') or '').strip(),
    }
    labels = request.POST.getlist('price_label')
    families = request.POST.getlist('price_family')
    variants = request.POST.getlist('price_variant')
    supplies = request.POST.getlist('price_supply')
    labours = request.POST.getlist('price_labour')
    allins = request.POST.getlist('price_allin')

    def at(lst, i):
        return (lst[i] if i < len(lst) else '').strip()

    for i, label in enumerate(labels):
        if not label.strip():
            continue
        data['prices'].append({
            'label': label.strip(),
            'family': at(families, i).lower() or 'other',
            'variant': at(variants, i).lower(),
            'supply': at(supplies, i),
            'labour': at(labours, i),
            'allin': at(allins, i),
        })
    try:
        photos = _json.loads(request.POST.get('photos_meta') or '[]')
        if isinstance(photos, list):
            data['photos'] = [
                {'path': str(p.get('path', ''))[:255],
                 'tag': str(p.get('tag', 'general'))[:40],
                 'caption': str(p.get('caption', ''))[:200],
                 'pair_with_prev': bool(p.get('pair_with_prev')),
                 # Annotator extras: the picked library item (for re-linking
                 # prices in the UI), the composed price line, the preview URL.
                 'lib': p.get('lib') if isinstance(p.get('lib'), dict) else None,
                 'price_line': str(p.get('price_line', ''))[:200],
                 'url': str(p.get('url', ''))[:500]}
                for p in photos if p.get('path')
            ]
    except (ValueError, AttributeError):
        pass
    data['pasted_price_list'] = (request.POST.get('pasted_price_list') or '').strip()
    data['notes'] = (request.POST.get('notes') or '').strip()
    return data


def intake_form(request, token):
    """PUBLIC (token-gated) owner intake wizard. Submissions are drafts — the
    admin reviews and approves before anything goes live."""
    import json as _json
    intake = get_object_or_404(TenantIntake, token=token)
    if intake.status in ('approved', 'rejected'):
        return render(request, 'bot/pages/intake_done.html',
                      {'intake': intake}, status=200)

    if request.method == 'POST':
        data = _parse_intake_post(request)
        # Titles are mandatory: every photo needs a name, except a 'before'
        # shot whose pair takes the 'after' photo's title.
        photos = data.get('photos') or []
        untitled = any(
            not (p.get('caption') or '').strip()
            and not (i + 1 < len(photos) and photos[i + 1].get('pair_with_prev'))
            for i, p in enumerate(photos))
        if untitled:
            intake.data = data  # keep the draft — nothing the owner typed is lost
            intake.save(update_fields=['data'])
            return render(request, 'bot/pages/intake_form.html', {
                'intake': intake,
                'tenant': intake.tenant,
                'profile_fields': INTAKE_PROFILE_FIELDS,
                'existing_json': _json.dumps(intake.data or {}),
                'error': 'Please provide names of items for the image.',
            })
        intake.data = data
        intake.status = 'submitted'
        intake.submitted_at = timezone.now()
        intake.save(update_fields=['data', 'status', 'submitted_at'])
        return render(request, 'bot/pages/intake_done.html', {'intake': intake})

    return render(request, 'bot/pages/intake_form.html', {
        'intake': intake,
        'tenant': intake.tenant,
        'profile_fields': INTAKE_PROFILE_FIELDS,
        'existing_json': _json.dumps(intake.data or {}),
    })


@require_POST
def intake_autosave(request, token):
    """Per-step autosave (public, token-gated): merge the wizard's current
    state into the draft so owners resume where they left off."""
    from django.http import JsonResponse
    intake = get_object_or_404(TenantIntake, token=token)
    if intake.status != 'pending':
        return JsonResponse({'ok': False, 'error': 'closed'}, status=409)
    intake.data = _parse_intake_post(request)
    intake.save(update_fields=['data'])
    return JsonResponse({'ok': True})


@require_POST
def intake_photo_upload(request, token):
    """Media upload for the gallery step (public, token-gated). Stores the
    file immediately under tenant_portfolios/<slug>/; the wizard tracks
    path+tag+caption client-side and submits them in photos_meta."""
    from django.http import JsonResponse

    from ..media_library import save_portfolio_upload
    intake = get_object_or_404(TenantIntake, token=token)
    if intake.status != 'pending':
        return JsonResponse({'ok': False, 'error': 'closed'}, status=409)
    upload = request.FILES.get('photo')
    if upload is None:
        return JsonResponse({'ok': False, 'error': 'no file'}, status=400)
    path, error = save_portfolio_upload(intake.tenant, upload)
    if error:
        return JsonResponse({'ok': False, 'error': error}, status=400)
    from django.core.files.storage import default_storage
    try:
        url = default_storage.url(path)
    except Exception:
        url = ''
    return JsonResponse({'ok': True, 'path': path, 'url': url})


_WEEK = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']


def _compose_business_hours(hours: dict):
    """Wizard day-chips + time pickers → the profile's business_hours shape
    ({'days': 'Monday-Saturday', 'open', 'close', 'closed': ['sun']})."""
    selected = [d for d in _WEEK if d in (hours.get('days') or [])]
    if not selected or not hours.get('open') or not hours.get('close'):
        return None
    return {
        'days': f"{selected[0].title()}-{selected[-1].title()}",
        'open': hours['open'],
        'close': hours['close'],
        'closed': [d[:3] for d in _WEEK if d not in selected],
    }


def _join_natural(items):
    items = [i for i in items if i]
    if not items:
        return ''
    if len(items) == 1:
        return items[0]
    return ', '.join(items[:-1]) + f", and {items[-1]}"


def _compose_payment_fact(methods) -> str:
    joined = _join_natural(methods)
    if not joined:
        return ''
    return (f"{joined} — all good.\n\n"
            "You'll get the full price before anything starts, no surprises.")


def _compose_services_fact(services) -> str:
    joined = _join_natural([s.lower() for s in services])
    if not joined:
        return ''
    return f"Yes, we handle {joined}."


def _compose_duration_fact(durations: dict) -> str:
    small, big = durations.get('small'), durations.get('big')
    if small and big:
        return (f"It depends on the scope — smaller jobs usually take {small}, "
                f"while bigger jobs run {big}.")
    if small or big:
        return f"Most jobs take {small or big}, depending on the scope."
    return ''


def _apply_intake_photos(tenant, photos):
    """Approved gallery photos → TenantPortfolioItem rows (before/after pairs
    merge into one item; tags become keywords so the bot sends RELEVANT shots)."""
    from django.utils.text import slugify as _slugify

    from ..models import TenantPortfolioItem
    previous_path = None
    index = TenantPortfolioItem.objects.filter(tenant=tenant).count()
    for photo in photos:
        path, tag = photo.get('path'), (photo.get('tag') or 'general').lower()
        if not path:
            continue
        if photo.get('pair_with_prev') and previous_path:
            # This shot is the AFTER of a pair; the previous upload is the BEFORE.
            item = TenantPortfolioItem.objects.filter(
                tenant=tenant, filename=previous_path).first()
            if item is not None:
                item.pair_filename = item.filename
                item.filename = path
                title = photo.get('caption') or f"{tag.title()} — before & after"
                item.title = title[:120]
                item.price_line = (photo.get('price_line') or item.price_line or '')[:200]
                item.save(update_fields=['pair_filename', 'filename', 'title', 'price_line'])
                previous_path = path
                continue
        index += 1
        TenantPortfolioItem.objects.get_or_create(
            tenant=tenant,
            item_id=_slugify(f"{tag}-{index}")[:80],
            defaults=dict(
                filename=path,
                title=(photo.get('caption') or f"{tag.title()} work")[:120],
                description=photo.get('caption', ''),
                price_line=(photo.get('price_line') or '')[:200],
                keywords=[tag],
                sort_order=index,
            ),
        )
        previous_path = path


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
            composed_hours = _compose_business_hours(hours)
            if composed_hours:
                profile.business_hours = composed_hours
            if data.get('excluded_areas'):
                profile.excluded_areas = data['excluded_areas']
            merged = dict(profile.faq_facts or {})
            merged.update(data.get('faq_facts') or {})
            # Structured answers → the bot's fact sentences.
            payment_line = _compose_payment_fact(data.get('payment_methods') or [])
            if payment_line:
                merged['payment'] = payment_line
            services_line = _compose_services_fact(data.get('services') or [])
            if services_line:
                merged['services'] = services_line
            duration_line = _compose_duration_fact(data.get('durations') or {})
            if duration_line:
                merged['job_duration'] = duration_line
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
            _apply_intake_photos(tenant, data.get('photos') or [])
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


class PriceItemForm(forms.ModelForm):
    class Meta:
        model = TenantPriceItem
        # 'flat' retired from the editor — a single figure goes in All-in.
        # Existing flat values are preserved (the form never touches the field).
        fields = ['family', 'variant', 'label', 'short_label',
                  'supply', 'labour', 'allin', 'parts',
                  'sort_order', 'is_active']
        # parts (component breakdown, e.g. tub + mixer + install) is edited by
        # the inline chip UI; the raw JSON rides along in a hidden input.
        widgets = {'parts': forms.HiddenInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A brand-new custom row (Add item) posts no family — it's derived from
        # the name on save; catalogue rows always carry their hidden family.
        self.fields['family'].required = False


def _price_formset(extra):
    """Price-sheet formset. `extra` blank rows carry the prefill template on a
    tenant that has no items yet; 0 once the sheet has real rows."""
    return modelformset_factory(
        TenantPriceItem, form=PriceItemForm, extra=extra, can_delete=True,
    )


def _is_unpriced(item):
    """A template row the owner never touched — no headline figure AND no
    component amount, so there is nothing to persist yet."""
    no_money = all(getattr(item, f) is None for f in ('supply', 'labour', 'flat', 'allin'))
    no_part_amount = not any(
        p.get('amount') not in (None, '') for p in (item.parts or []))
    return no_money and no_part_amount


def _money(value, cur):
    """'US$170' — whole-dollar 'from' rates, no trailing .00."""
    if value is None:
        return None
    value = int(value) if value == int(value) else value
    return f"{cur}{value}"


def _price_line(item, cur):
    """A configured item as a human price string for the read-only view —
    headline figure + an optional breakdown line. Returns None for an item
    that carries no figure at all (nothing to show)."""
    parts = [p for p in (item.parts or []) if p.get('amount') not in (None, '')]
    parts_sub = ' · '.join(
        f"{p.get('name', 'part')} {_money(p['amount'], cur)}" for p in parts)
    if item.allin is not None:
        headline = f"{_money(item.allin, cur)} all-in"
        sub = (f"supply {_money(item.supply, cur)} + labour {_money(item.labour, cur)}"
               if item.supply is not None and item.labour is not None else parts_sub)
    elif item.flat is not None:
        headline, sub = _money(item.flat, cur), parts_sub
    elif item.supply is not None or item.labour is not None:
        total = (item.supply or 0) + (item.labour or 0)
        headline = f"{_money(total, cur)} all-in"
        sub = f"supply {_money(item.supply or 0, cur)} + labour {_money(item.labour or 0, cur)}"
    elif parts:
        headline = f"{_money(sum(p['amount'] for p in parts), cur)} all-in"
        sub = parts_sub
    else:
        return None
    return {'label': item.label or item.family, 'family': item.family,
            'headline': headline, 'sub': sub, 'is_active': item.is_active}


@superuser_required
def platform_tenant_config(request, slug):
    """Read-only tenant overview: channel, profile, configured prices, staff,
    intake. Editing lives on platform_tenant_config_edit."""
    from ..tenant_config import get_config
    tenant = get_object_or_404(Tenant, slug=slug)
    profile = TenantProfile.objects.filter(tenant=tenant).first()
    cur = (profile.currency if profile and profile.currency else 'US$')
    priced_items = [line for line in
                    (_price_line(it, cur) for it in TenantPriceItem.objects.filter(tenant=tenant))
                    if line]
    faq_topics = [key.replace('_', ' ').title()
                  for key in (profile.faq_facts if profile else {}) or {}]
    return render(request, 'bot/pages/platform_tenant_config.html', {
        'tenant': tenant,
        'profile': profile,
        'cfg': get_config(tenant),
        'currency': cur,
        'priced_items': priced_items,
        'faq_topics': faq_topics,
        'channel': tenant.whatsapp_channels.first(),
        'intakes': tenant.intakes.all()[:10],
        'staff': tenant.memberships.select_related('user').order_by('user__username'),
        'active_nav': 'platform',
    })


@superuser_required
def platform_tenant_config_edit(request, slug):
    """Per-tenant config editor: profile + price sheet. The ONLY place tenant
    config is edited until the owner intake flow ships (decision #2)."""
    tenant = get_object_or_404(Tenant, slug=slug)
    profile, _ = TenantProfile.objects.get_or_create(tenant=tenant)
    prices_qs = TenantPriceItem.objects.filter(tenant=tenant)

    # Offer every homebase catalogue item the tenant doesn't already have as a
    # fill-in template row (labels prefilled, prices blank) — so even a tenant
    # with a few rows sees the full standard list to price. Existing rows keep
    # their own figures; a template row persists only once it's priced, and any
    # still-unpriced item re-appears next visit until it's given a figure.
    have = {(p.family, p.variant) for p in prices_qs}
    prefill = [row for row in blank_priced_catalog()
               if (row['family'], row['variant']) not in have] or None

    if request.method == 'POST':
        form = TenantProfileForm(request.POST, instance=profile)
        formset = _price_formset(0)(request.POST, queryset=prices_qs)
        if form.is_valid() and formset.is_valid():
            from django.db import IntegrityError, transaction
            form.save()
            items = formset.save(commit=False)
            for item in items:
                if item.pk is None:
                    # Untouched template rows (new + no price) are dropped.
                    if _is_unpriced(item):
                        continue
                    # A custom "Add item" row carries no family — key it off the name.
                    if not item.family:
                        item.family = (slugify(item.label) or 'custom')[:40]
                        item.variant = ''
                item.tenant = tenant
                try:
                    with transaction.atomic():
                        item.save()
                except IntegrityError:
                    messages.warning(request, f'Skipped duplicate item "{item.label}".')
            for item in formset.deleted_objects:
                item.delete()
            from ..whatsapp_cloud_api import invalidate_client_cache
            invalidate_client_cache()
            messages.success(request, f'Config saved for {tenant.name}.')
            return redirect('platform_tenant_config', slug=tenant.slug)
        messages.error(request, 'Fix the errors below — nothing was saved.')
    else:
        form = TenantProfileForm(instance=profile)
        formset = _price_formset(len(prefill) if prefill else 0)(
            queryset=prices_qs, initial=prefill)

    return render(request, 'bot/pages/platform_tenant_config_edit.html', {
        'tenant': tenant,
        'form': form,
        'formset': formset,
        'currency': (profile.currency or 'US$'),
        'active_nav': 'platform',
    })
