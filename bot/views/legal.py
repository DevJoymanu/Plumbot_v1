"""Public legal pages (privacy policy, terms of service) for the platform
operator (HomeX Media, trading as Plumbot) — not tenant-scoped, no login
required. Needed as live URLs for WhatsApp Cloud API / Meta App Review."""
from django.shortcuts import render


def privacy_policy(request):
    return render(request, 'bot/pages/legal_privacy.html')


def terms_of_service(request):
    return render(request, 'bot/pages/legal_terms.html')
