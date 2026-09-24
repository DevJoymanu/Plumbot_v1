# Current state of the codebase

Orientation for the next session: what the system does today, why it's built this way, what's fragile, and the conventions to keep. Reflects the codebase as of June 2026.

One file per area. Read the one for the code you are about to change BEFORE you change it; the nested CLAUDE.md in each directory says which.

- [The conversation pipeline](pipeline.md) - Inbound pipeline, unified classifier, conversation storage, quoted replies, the reasoning controller, and the outbound chain every reply goes through.
- [Sales and pricing](sales-and-pricing.md) - Portfolio/catalogue and every pricing rule (price close, budget ladder, replacements, materials lists).
- [Conversation rules](conversation-rules.md) - The copy and flow conventions every customer-facing reply follows: the free visit and the fee said once, the availability ask, "anytime", the area fallback, advancing the sale, no emojis, no dashes.
- [Booking, availability and rescheduling](booking-and-scheduling.md) - Booking, availability and rescheduling
- [Follow-ups, cron and cancelling](followups-and-cron.md) - send_followups (the cap, the floor, the owner scripts, placement), the Railway cron trap, and the staff stop/cancel controls.
- [Post-visit debrief and the plan path](post-visit-and-plan-path.md) - SiteVisitReport, the quote follow-up sequence, PlanQuoteRequest and VisitProposal.
- [Email](email.md) - Inbound (reply-only), sending identities, transports, the sent-email screens and the Settings > Email health panel.
- [Tenancy, branding and permissions](tenancy-and-permissions.md) - No Homebase value reaches another tenant; tenant hours; logos; who may do what on the dashboard.
- [Quotes and quote templates](quotes.md) - My Quotes, templates (client vs global, the sectioned builder), the flat and sectioned editors, the document, sending, deposits, drafts and the plan tab.
- [Platform billing](billing.md) - The operator invoicing tenants: invoices, payments and receipts, the PDFs, and the platform billing email identity.
- [Dashboard UI](dashboard-ui.md) - The sidebar, the detail page (date/time form, compact header, chat composer), the diary, lead search, the inbox date window, and mobile parity.
