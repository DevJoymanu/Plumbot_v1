"""
Whole-tree template audit (2026-09-14).

``test_views_actions.py`` smoke-GETs every staff page, and every one of the
faults below rendered a 200 while it was broken, so that layer could not see
any of them. Each check here runs against EVERY template rather than the
handful that happened to be wrong, because the broken page is by definition
the one nobody opens:

* a child that extends a layout and then emits its own ``<html>``
* a page that re-renders a partial its layout already renders
* a template reachable from no view at all
* a ``{% url %}`` naming a route that does not exist

Run: ``python manage.py test bot.test_template_audit``
"""

import pathlib
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import get_resolver, reverse
from django.utils import timezone

from .models import Appointment, Tenant, TenantMembership


def _templates():
    """(root, [paths]) for every template on disk."""
    root = pathlib.Path(settings.BASE_DIR) / 'bot' / 'templates'
    return root, sorted(root.rglob('*.html'))


class TemplateStructureTests(TestCase):
    """Structure: one document per page, one copy of each layout partial."""

    # A real tag opens its line. `<body>` inside a JS comment or a string is
    # prose about the document, not a second copy of it.
    _DOC_TAG = re.compile(r'^\s*<\s*(!DOCTYPE|html|head|body)\b', re.I | re.M)
    _EXTENDS = re.compile(r'{%\s*extends\s')

    def test_a_child_template_never_re_renders_the_document(self):
        """bot/pages/schedule_job.html extended base.html and then emitted its
        own <!DOCTYPE html><html><head><body> inside {% block content %}. The
        layout wrapped a whole second document; browsers discard the inner
        tags, so the page's own <title> silently never applied.

        The same defect QuoteMobileLayoutTests pins for the quote screens.
        schedule_job was simply not in that list, so it shipped.
        """
        root, paths = _templates()
        offenders = []
        for path in paths:
            src = path.read_text(encoding='utf-8')
            if not self._EXTENDS.search(src):
                continue  # a standalone public page is allowed a document
            for match in self._DOC_TAG.finditer(src):
                lineno = src[:match.start()].count('\n') + 1
                offenders.append(f'{path.relative_to(root).as_posix()}:{lineno} '
                                 f'-> {match.group(0).strip()}')

        self.assertEqual(offenders, [], (
            'These templates extend a layout AND emit their own document '
            'structure, so the layout wraps a second <html>:\n  '
            + '\n  '.join(offenders)))

    def test_only_a_layout_renders_the_messages_partial(self):
        """Every layout renders bot/includes/messages.html already. A page that
        includes it again shows every flash message twice, which is what
        gallery, offer and followup_test_suite did.
        """
        root, paths = _templates()
        offenders = []
        for path in paths:
            rel = path.relative_to(root).as_posix()
            if rel.startswith('bot/layouts/'):
                continue
            for lineno, line in enumerate(
                    path.read_text(encoding='utf-8').splitlines(), 1):
                if 'include "bot/includes/messages.html"' in line:
                    offenders.append(f'{rel}:{lineno}')

        self.assertEqual(offenders, [], (
            'messages.html is rendered by the layouts; including it again '
            'double-renders every flash message:\n  ' + '\n  '.join(offenders)))

    def test_no_template_is_unreachable(self):
        """Reachability is the transitive closure of extends/include from the
        templates named in view code.

        A duplicate tree of 42 one-line shims sat outside it, two of them
        including pages that had been deleted. Dead templates get edited by
        mistake and read as the live ones.
        """
        root, paths = _templates()
        on_disk = {p.relative_to(root).as_posix() for p in paths}

        named_in_python = set()
        for py in sorted((pathlib.Path(settings.BASE_DIR) / 'bot').rglob('*.py')):
            source = py.read_text(encoding='utf-8', errors='replace')
            for hit in re.findall(r"['\"]([A-Za-z0-9_/]+\.html)['\"]", source):
                if hit in on_disk:
                    named_in_python.add(hit)

        def refs(name):
            src = (root / name).read_text(encoding='utf-8', errors='replace')
            return re.findall(r"{%\s*(?:extends|include)\s+[\"']([^\"']+)[\"']", src)

        reached, stack = set(), list(named_in_python)
        while stack:
            name = stack.pop()
            if name in reached or name not in on_disk:
                continue
            reached.add(name)
            stack.extend(refs(name))

        self.assertEqual(sorted(on_disk - reached), [], (
            'These templates are reachable from no view:\n  '
            + '\n  '.join(sorted(on_disk - reached))))


class TemplateUrlTagTests(TestCase):
    """Every {% url %} names a real route and passes a count it accepts.

    Four tags pointed at routes that did not exist, in two templates whose
    views were themselves never routed - so the NoReverseMatch sat there
    unreachable and unseen. Only a whole-tree check finds that.
    """

    _TAG = re.compile(r'{%\s*url\s+(.*?)%}', re.S)

    @staticmethod
    def _split(rest):
        """Tag body -> (name token, argument count). Quote- and paren-aware."""
        rest = re.sub(r'\bas\s+\w+\s*$', '', rest.strip())
        tokens, current, quote, depth = [], '', None, 0
        for char in rest:
            if quote:
                current += char
                if char == quote:
                    quote = None
                continue
            if char in '"\'':
                quote = char
                current += char
                continue
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
            if char.isspace() and depth == 0:
                if current:
                    tokens.append(current)
                current = ''
            else:
                current += char
        if current:
            tokens.append(current)
        if not tokens:
            return None, 0
        return tokens[0], len(tokens) - 1

    def test_every_url_tag_resolves(self):
        accepted = {}
        for key, value in get_resolver().reverse_dict.items():
            if isinstance(key, str):
                accepted[key] = {len(params) for _, params in value[0]}

        root, paths = _templates()
        offenders = []
        for path in paths:
            src = path.read_text(encoding='utf-8')
            rel = path.relative_to(root).as_posix()
            for match in self._TAG.finditer(src):
                name, count = self._split(match.group(1))
                if not name or name[0] not in '"\'':
                    continue  # a variable route name; nothing to check here
                name = name.strip('"\'')
                lineno = src[:match.start()].count('\n') + 1
                if name not in accepted:
                    offenders.append(f'{rel}:{lineno} -> no route named {name!r}')
                elif count not in accepted[name]:
                    offenders.append(
                        f'{rel}:{lineno} -> {name!r} given {count} argument(s), '
                        f'accepts {sorted(accepted[name])}')

        self.assertEqual(offenders, [], (
            'These url tags raise NoReverseMatch when the page renders:\n  '
            + '\n  '.join(offenders)))


class CalendarDataEndpointTests(TestCase):
    """The calendar page ships no server-side context, so this endpoint is its
    only data source - and it was never routed.

    `fetch('/api/appointments/')` 404'd into a console.error, leaving the
    calendar permanently empty while rendering a clean 200.
    """

    def setUp(self):
        self.tenant = Tenant.objects.get(slug='homebase')
        self.user = get_user_model().objects.create_user(
            username='cal-staff', password='pass12345', is_staff=True)
        TenantMembership.objects.create(
            user=self.user, tenant=self.tenant, role='staff')

    def test_the_endpoint_the_calendar_fetches_exists(self):
        self.assertEqual(reverse('appointment_data'), '/api/appointments/')

    def test_the_calendar_page_names_the_route_rather_than_a_path(self):
        self.client.force_login(self.user)
        body = self.client.get(reverse('calendar')).content.decode()
        self.assertIn(f"fetch('{reverse('appointment_data')}')", body)

    def test_it_returns_the_tenant_appointments(self):
        Appointment.objects.create(
            phone_number='whatsapp:+15550009001', customer_name='Cal Lead',
            tenant=self.tenant, status='confirmed',
            scheduled_datetime=timezone.now() + timezone.timedelta(days=1))
        self.client.force_login(self.user)

        response = self.client.get(reverse('appointment_data'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['customerName'] for row in response.json()],
                         ['Cal Lead'])

    def test_it_is_staff_gated(self):
        """It carries customer names and phone numbers. The page it feeds was
        ungated too, which was harmless only while it returned nothing.
        """
        response = self.client.get(reverse('appointment_data'))
        self.assertEqual(response.status_code, 302)
        self.assertNotIn('customerName', response.content.decode())


class UserAdministrationRouteTests(TestCase):
    """The user-management screens: written, gated, cross-linked by name, and
    routed nowhere - so neither could ever render and their four broken
    {% url %} tags were never reached.
    """

    def setUp(self):
        self.root = get_user_model().objects.create_superuser(
            username='root', password='pass12345', email='root@example.com')
        self.member = get_user_model().objects.create_user(
            username='member', password='pass12345', is_staff=True)
        # Without a workspace TenantMiddleware answers 403 of its own before
        # the view runs, so the gate under test would never be reached.
        TenantMembership.objects.create(
            user=self.member, tenant=Tenant.objects.get(slug='homebase'),
            role='staff')

    def test_the_screens_render_for_a_superuser(self):
        self.client.force_login(self.root)
        for name in ('user_management', 'create_user'):
            with self.subTest(route=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_plain_staff_are_refused(self):
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(reverse('user_management')).status_code, 403)

    def test_the_state_changing_actions_refuse_a_get(self):
        """Both were plain <a href> links. A GET that flips a user's access is
        one a browser or a scanner can prefetch.
        """
        self.client.force_login(self.root)
        for name in ('toggle_user_status', 'promote_to_superuser'):
            with self.subTest(route=name):
                response = self.client.get(reverse(name, args=[self.member.pk]))
                self.assertEqual(response.status_code, 405)

    def test_toggle_deactivates_on_post(self):
        self.client.force_login(self.root)
        self.client.post(reverse('toggle_user_status', args=[self.member.pk]))
        self.member.refresh_from_db()
        self.assertFalse(self.member.is_active)

    def test_promote_grants_superuser_on_post(self):
        self.client.force_login(self.root)
        self.client.post(reverse('promote_to_superuser', args=[self.member.pk]))
        self.member.refresh_from_db()
        self.assertTrue(self.member.is_superuser)

    def test_the_screen_posts_both_actions(self):
        """The buttons must be forms, or the POST-only views 405 on every tap."""
        self.client.force_login(self.root)
        body = self.client.get(reverse('user_management')).content.decode()
        for name in ('toggle_user_status', 'promote_to_superuser'):
            with self.subTest(action=name):
                self.assertIn(
                    f'action="{reverse(name, args=[self.member.pk])}"', body)


class AccessDeniedHandlerTests(TestCase):
    """staff_required / superuser_required raise PermissionDenied, which
    rendered Django's bare default 403. access_denied_view was written for the
    handler403 contract (request, exception=None) but was never wired, so its
    template was dead and the message never reached anyone.
    """

    def test_a_refusal_renders_the_projects_own_page(self):
        user = get_user_model().objects.create_user(
            username='denied', password='pass12345', is_staff=True)
        # A workspace, or TenantMiddleware answers 403 first and the gate
        # under test never runs.
        TenantMembership.objects.create(
            user=user, tenant=Tenant.objects.get(slug='homebase'), role='staff')
        self.client.force_login(user)

        response = self.client.get(reverse('user_management'))

        self.assertEqual(response.status_code, 403)
        self.assertIn('do not have permission', response.content.decode())


class MobileParityTests(TestCase):
    """Anything reachable on the desktop must be reachable on a phone.

    The sidebar is hidden outright below 768px, so every destination in it has
    to have a counterpart in the bottom bar or its More sheet. Two did not:
    the platform console and the workspace switcher were desktop-only, which
    left an operator on a phone locked into whichever tenant they last picked
    at a desk, with no way into the console at all.
    """

    NAV = 'bot/templates/bot/includes/main_nav.html'
    MOBILE = 'bot/templates/bot/includes/mobile_nav.html'

    @staticmethod
    def _route_names(path):
        src = (pathlib.Path(settings.BASE_DIR) / path).read_text(encoding='utf-8')
        return set(re.findall(r"{%\s*url\s+'([a-z_]+)'", src))

    def test_the_sidebar_is_hidden_on_a_phone(self):
        """The premise of the test below: if this stops being true, parity is
        no longer the mobile nav's job alone."""
        css = (pathlib.Path(settings.BASE_DIR)
               / 'bot/templates/bot/includes/plumbot_variables_css.html'
               ).read_text(encoding='utf-8')
        self.assertRegex(css, r'\.pb-sidenav\s*\{\s*display:\s*none')

    def test_every_desktop_destination_is_reachable_on_mobile(self):
        missing = self._route_names(self.NAV) - self._route_names(self.MOBILE)
        self.assertEqual(missing, set(), (
            'These are in the sidebar but nowhere in the bottom bar or its '
            'More sheet, so a phone cannot reach them: %s' % sorted(missing)))

    def test_the_switcher_really_renders_for_an_operator(self):
        """The static check above only proves the tag is written. Both navs
        gate on `platform_tenants`, so a context that never supplies it would
        satisfy that check while the control silently never appeared."""
        Tenant.objects.get_or_create(slug='second-co',
                                     defaults={'name': 'Second Co'})
        root = get_user_model().objects.create_superuser(
            username='op', password='pass12345', email='op@example.com')
        self.client.force_login(root)

        body = self.client.get(reverse('dashboard')).content.decode()

        sheet = body.split('id="pbMobileSheet"', 1)[1]
        self.assertIn(reverse('switch_tenant'), sheet,
                      'the workspace switcher is missing from the mobile sheet')
        self.assertIn(reverse('platform_console'), sheet,
                      'the platform console is missing from the mobile sheet')

    def test_a_plain_staff_phone_gets_neither(self):
        """Same two gates as the sidebar - the sheet must not widen them."""
        user = get_user_model().objects.create_user(
            username='phone-staff', password='pass12345', is_staff=True)
        TenantMembership.objects.create(
            user=user, tenant=Tenant.objects.get(slug='homebase'), role='staff')
        self.client.force_login(user)

        body = self.client.get(reverse('dashboard')).content.decode()

        sheet = body.split('id="pbMobileSheet"', 1)[1]
        self.assertNotIn(reverse('switch_tenant'), sheet)
        self.assertNotIn(reverse('platform_console'), sheet)


class MobileZoomTests(TestCase):
    """Zoom must not cost the operator anything - neither the ability to use
    it, nor control of when it happens."""

    @staticmethod
    def _templates():
        root = pathlib.Path(settings.BASE_DIR) / 'bot' / 'templates'
        return root, sorted(root.rglob('*.html'))

    def test_no_page_caps_pinch_zoom(self):
        """maximum-scale and user-scalable=no take zoom away from someone who
        may need it to read the screen at all. The three staff layouts capped
        it at 5x; nothing needs to cap it."""
        root, paths = self._templates()
        offenders = []
        for path in paths:
            src = path.read_text(encoding='utf-8')
            for lineno, line in enumerate(src.splitlines(), 1):
                if 'name="viewport"' not in line and "name='viewport'" not in line:
                    continue
                if 'maximum-scale' in line or 'user-scalable' in line:
                    offenders.append(f'{path.relative_to(root).as_posix()}:{lineno}')
        self.assertEqual(offenders, [], (
            'These viewport tags restrict zoom:\n  ' + '\n  '.join(offenders)))

    def test_form_controls_are_sixteen_px_on_a_phone(self):
        """Under 16px, iOS zooms the page in when a field takes focus and does
        not zoom back out. As a CSS rule this covers fields added after load;
        the JS pass it replaced ran once at DOMContentLoaded and so missed
        every quote item row the plumber added."""
        css = (pathlib.Path(settings.BASE_DIR)
               / 'bot/templates/bot/includes/plumbot_variables_css.html'
               ).read_text(encoding='utf-8')
        block = css.split('@media (max-width: 767px) {')[-1]
        self.assertIn('input, select, textarea { font-size: 16px; }', block)

    def test_no_template_stamps_a_font_size_from_javascript(self):
        """An inline style outranks the mobile layer, so a JS pass does not
        reinforce that rule - it overrides it, and only for what existed when
        it ran."""
        root, paths = self._templates()
        offenders = []
        for path in paths:
            src = path.read_text(encoding='utf-8')
            for lineno, line in enumerate(src.splitlines(), 1):
                if re.search(r'style\.fontSize\s*=', line):
                    offenders.append(f'{path.relative_to(root).as_posix()}:{lineno}')
        self.assertEqual(offenders, [], (
            'Set the size in the mobile CSS layer instead:\n  '
            + '\n  '.join(offenders)))

    def test_the_shell_tracks_the_mobile_url_bar(self):
        """100vh on a phone is the viewport with the URL bar hidden, and the
        shell does not scroll, so while the bar shows that surplus is
        unreachable."""
        css = (pathlib.Path(settings.BASE_DIR)
               / 'bot/templates/bot/includes/plumbot_variables_css.html'
               ).read_text(encoding='utf-8')
        shell = css.split('.pb-shell {')[1].split('}')[0]
        self.assertIn('100dvh', shell)
        self.assertIn('100vh', shell, 'keep the fallback for older browsers')

    def test_the_bottom_bar_clears_the_home_indicator(self):
        """Otherwise its lowest row of taps lands in the system swipe area."""
        css = (pathlib.Path(settings.BASE_DIR)
               / 'bot/templates/bot/includes/plumbot_variables_css.html'
               ).read_text(encoding='utf-8')
        bar = css.split('.pb-bottomnav {')[1].split('}')[0]
        self.assertIn('safe-area-inset-bottom', bar)


class WideTableTests(TestCase):
    """A table wider than the screen must scroll inside its own box.

    The page's own scroller is not an acceptable answer: on these screens it
    drags the header and every other card sideways with it.
    """

    #: Layouts that constrain themselves and need no scroll box.
    SELF_CONSTRAINED = ('pbq-table--stack', 'pbq-table--edit', 'bq-table', 'bq-totals')

    def test_every_table_is_either_constrained_or_scrollable(self):
        root = pathlib.Path(settings.BASE_DIR) / 'bot' / 'templates'
        offenders = []
        for path in sorted(root.rglob('*.html')):
            src = path.read_text(encoding='utf-8')
            for m in re.finditer(r'<table\b[^>]*>', src):
                tag = m.group(0)
                if any(c in tag for c in self.SELF_CONSTRAINED):
                    continue
                before = src[max(0, m.start() - 400):m.start()]
                if ('pb-table-scroll' in before or 'table-responsive' in before
                        or 'overflow-x' in before or 'overflow: auto' in before):
                    continue
                lineno = src[:m.start()].count('\n') + 1
                offenders.append(f'{path.relative_to(root).as_posix()}:{lineno} '
                                 f'{" ".join(tag.split())[:60]}')
        self.assertEqual(offenders, [], (
            'Wrap these in <div class="pb-table-scroll"> or give the table a '
            'fixed layout:\n  ' + '\n  '.join(offenders)))
