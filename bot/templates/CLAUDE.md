# bot/templates/ — the dashboard and quote screens

**Load the `plumbot-ui-design` skill before editing any template.** Then read:

- anything quote-related (editors, the document, the list, templates, the PDF's HTML twins): `docs/current-state/quotes.md`
- everything else on the dashboard: `docs/current-state/dashboard-ui.md`
- the site-visit banner and debrief form: `docs/current-state/post-visit-and-plan-path.md`

What goes wrong here most:

- **A page that renders 200 can still be dead.** A handler that throws (a temporal-dead-zone `const`, a real newline inside a JS string literal) kills every control on a page Django's tests call healthy. JS-heavy pages are exercised in jsdom against the dumped pages (`bot/test_dump_quote_pages.py`); run it.
- **`{# #}` cannot span lines** in a Django template (`TemplateCommentTests` catches it). Use `{% comment %}` for more than one line.
- **Quote screens extend `bot/layouts/base.html` and include `bot/includes/quote_responsive_css.html`.** Every `<td>` in a stacked or editable table carries a `data-label`. The action bar closes the page and is never pinned to the viewport (the QUOTE/PLAN tab bar is the one exception).
- **One document, one include**: the flat quote document and letterhead are shared by the editor preview and the client copy, and neither may carry a Homebase value. Every letterhead value is optional and absent means omit.
- **Mobile parity**: every destination in `main_nav.html` has a counterpart in `mobile_nav.html`; no viewport caps zoom; form controls are 16px below 768px as a CSS rule, never a JS pass; a wide table scrolls inside its own box.
- **A `<form>` is never inside an `<a>` and never nested in another `<form>`**; use `formaction` on the outer form's button instead.
- **Inside a framed pane (`chromeless`), a link out to a full page carries `frame_query`**, or the pane renders a second nav bar.
- **Gate the VIEW, never only the template**: hiding a button is presentation, not permission.
