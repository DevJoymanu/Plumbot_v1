# bot/views/ — dashboard views

(The Plumbot mixins in `plumbot/` have their own CLAUDE.md.) Read before editing:

- `quotations.py`, `quotation_templates.py`, anything producing a quote or its PDF: `docs/current-state/quotes.md`
- `appointments.py`, `dashboard.py`, `followups.py`, `conversations`: `docs/current-state/dashboard-ui.md` and `docs/current-state/followups-and-cron.md`
- `sent_emails.py`, email settings: `docs/current-state/email.md`
- `platform.py`, auth, profile, branding: `docs/current-state/tenancy-and-permissions.md`
- `billing.py` (the operator invoicing tenants): `docs/current-state/billing.md`

What goes wrong here most:

- **Scope at the QUERY, through the one resolver**: `_visible_quotations`, `QuotationTemplate.for_user` / `.editable_by`, `_editable_template`, `_visible_templates`. A bare `get(pk=…)` has already leaked one tenant's quotes, templates and prices to another three times. No workspace means no rows, never all rows.
- **Visibility is not permission**: ask "can they see it" and "can they change it" separately.
- **Mutations are POST** (`@require_POST`); a GET that changes state is one a browser can prefetch.
- **A `next` is honoured only through `safe_return_path`**, which keeps the local path and nothing else.
- **Owner-only and superuser-only are decorators on the view** (`@owner_required`, `@superuser_required`); the template check is only presentation.
- **Every new staff page or action gets a case in `bot/test_views_actions.py`**, asserting the DB effect with outbound mocked.
