"""
The Plumbot flow map, as data. `build_flow_map.py` turns it into FLOW.md and
plumbot-flow.html.

WHAT: every step, decision and message of the WhatsApp sales process, the
crons that follow leads up, the post-visit and plan paths, and the dashboard
operations staff run, as nodes and edges.

WHY IT IS HAND-WRITTEN BUT CODE-CHECKED: the ORDER of the flow (which step
outranks which) is a judgement the code only expresses as a 1,700-line
function, so a person writes the graph. Everything a person could get wrong
about the DETAIL is read from the code instead: each node's `code` pointers
are resolved against the real source on every build (a renamed function or a
moved anchor fails the build), and its `copy` is parsed out of
`bot/copy_catalog.py` and the functions named, so the wording on the map is
the wording customers receive.

HOW TO EDIT (read this before changing the flow in code):
  * A pointer is "name", "Class.name" or "bot/path.py::name" (use the path
    form when a name is defined in more than one file), optionally followed by
    "#text": the block under the first line in that definition containing the
    text, up to the next section header comment. The router's steps are
    anchored on their own "# -- STEP ..." / "# ── ... ──" headers.
  * `copy` entries are "catalog:NAME" for a copy_catalog constant, or a code
    pointer whose customer-readable strings are extracted (logs, regexes and
    docstrings are skipped).
  * Kinds (the shape each node is drawn with) are in KINDS below. Edge kinds:
    "flow" (solid), "no" (dotted fall-through), "loop" (back to an earlier
    step), "handoff" (into another section).
  * `bot/test_flow_map.py` fails when a copy_catalog sentence, a router or
    generate_response section header, or a Railway cron command has no node.
    Add the node here in the same commit as the code.
"""

TITLE = 'Plumbot Flow Map'
GITHUB_BLOB = 'https://github.com/DevJoymanu/Plumbot_v1/blob/main/'
# Used only for the "Open in VS Code" links in the HTML viewer, which lets the
# reader change it. A constant here (not the build machine's path) keeps the
# generated file identical wherever it is built.
LOCAL_REPO_ROOT = 'D:/SAAS/CRMs/Plumbing/Plumbing_CRM'

# The reply ladders: functions that check things in a fixed order where the
# first match answers. Their rungs are the nodes anchored inside them, in the
# order the anchors sit in the code (read at build time, never typed here).
# Every section header at or above `header_indent` must have a node anchored
# on it (the coverage check), so a new router step cannot land unmapped.
LADDERS = [
    {'id': 'router', 'function': 'bot/whatsapp_webhook.py::_generate_and_schedule_reply',
     'header_indent': 8, 'title': 'Every inbound message',
     'blurb': 'The reply router. Runs on every batched turn.'},
    {'id': 'step4', 'function': 'ResponseMixin.generate_response',
     'header_indent': 16, 'title': 'Inside rung "generate_response" (qualification and booking)',
     'blurb': 'What the last rung of the router runs, in its own order.'},
]

# Shape and colour per kind. `shape` is the Mermaid shape named in FLOW.md's
# legend; the HTML viewer draws its own SVG for each kind.
KINDS = {
    'trigger':     {'label': 'Trigger', 'shape': 'Stadium', 'fill': '#d9ecff', 'stroke': '#2f6fb3',
                    'meaning': 'Something starts a flow: an inbound message, a cron tick, a staff click.'},
    'step':        {'label': 'Step', 'shape': 'Rectangle', 'fill': '#eef2f4', 'stroke': '#5b6f7a',
                    'meaning': 'Code does something deterministic.'},
    'decision':    {'label': 'If (code)', 'shape': 'Diamond', 'fill': '#fff4d6', 'stroke': '#b8860b',
                    'meaning': 'A deterministic if: keywords, stored state, dates, regexes.'},
    'ai_decision': {'label': 'If (model)', 'shape': 'Hexagon', 'fill': '#f0e4ff', 'stroke': '#7b4bc4',
                    'meaning': 'A branch decided by the DeepSeek classification or the controller.'},
    'gate':        {'label': 'Override gate', 'shape': 'Inverted trapezoid', 'fill': '#ffe1dc', 'stroke': '#c0392b',
                    'meaning': "A guard that outranks the flow: the customer's own words, a hard stop, a duplicate."},
    'send':        {'label': 'WhatsApp message', 'shape': 'Parallelogram', 'fill': '#d8f3ea', 'stroke': '#0b7a75',
                    'meaning': 'A message the customer receives on WhatsApp. Click for the exact wording.'},
    'email':       {'label': 'Email / PDF', 'shape': 'Reversed parallelogram', 'fill': '#e2f0d9', 'stroke': '#4d8a2c',
                    'meaning': 'An email or document sent to the customer or the plumber.'},
    'ai_write':    {'label': 'Model writes', 'shape': 'Subroutine', 'fill': '#ece6fb', 'stroke': '#5e45a8',
                    'meaning': 'A DeepSeek call that writes or reads text (fenced, with a fallback).'},
    'data':        {'label': 'State', 'shape': 'Cylinder', 'fill': '#e6e9ee', 'stroke': '#46545f',
                    'meaning': 'Something stored on the lead: a field, a notes tag, a row.'},
    'staff':       {'label': 'Staff action', 'shape': 'Trapezoid', 'fill': '#ffe9d1', 'stroke': '#c26a17',
                    'meaning': 'A person does this on the dashboard (or the plumber on a form).'},
    'notify':      {'label': 'Internal alert', 'shape': 'Flag', 'fill': '#fde2ef', 'stroke': '#b03a74',
                    'meaning': 'A message to the plumber or the team, never customer copy.'},
    'loop':        {'label': 'Loop / cron job', 'shape': 'Circle', 'fill': '#dff4f8', 'stroke': '#1f8aa0',
                    'meaning': 'Runs repeatedly: a cron pass over leads, a retry ladder.'},
    'wait':        {'label': 'Wait', 'shape': 'Rounded', 'fill': '#f4f0e6', 'stroke': '#8c7a4f',
                    'meaning': 'A deliberate delay: the debounce batch, the human reply delay.'},
    'end':         {'label': 'End', 'shape': 'Double circle', 'fill': '#ffffff', 'stroke': '#14212b',
                    'meaning': 'The turn ends here, usually in silence.'},
}

SECTIONS = [
    {'id': 'inbound', 'title': 'Inbound message',
     'blurb': 'A WhatsApp event arrives: tenant, WAMID dedup, message type, logging, delay marking, the debounce batch.'},
    {'id': 'router', 'title': 'Router preamble',
     'blurb': 'The start of every reply turn: pause, hard stop, contact cards, the unified DeepSeek turn, area capture, the proof step and controller moves.'},
    {'id': 'pending', 'title': 'Answers to our last question',
     'blurb': 'Pending states read before anything else: scope confirm, the no-handlers, the timeline pivot, the materials list and the FAQ layer.'},
    {'id': 'intents', 'title': 'Requests and objections',
     'blurb': 'Router STEP 0-a to 1a: link questions, hesitation, the price-guide choice, multi-intent, pictures and catalogue, the budget ladder.'},
    {'id': 'delay', 'title': 'Delay, exit and complaint',
     'blurb': 'Router STEP 1b and the out-of-scope handler: timeframe, near or far, the job-date ladder, email and portfolio, complaints.'},
    {'id': 'pricing', 'title': 'Pricing',
     'blurb': 'Router STEP 1c to 3c: a highlighted photo, the price guide, product prices, quotes lean to the visit, the overview, repeats, new builds.'},
    {'id': 'qualify', 'title': 'Qualification and booking',
     'blurb': 'Router STEP 4, generate_response: extraction, the question order, the scripted first ask, retries, booking and the name ask.'},
    {'id': 'outbound', 'title': 'Outbound chain',
     'blurb': 'finalise_outbound, which every reply passes: the model reader, the strippers, speak as WE, no dashes, one question; then the delayed send.'},
    {'id': 'followups', 'title': 'Follow-ups and crons',
     'blurb': 'The Railway crons: follow-ups with the 4-touch cap and the handoff, delay nudges, the job-date ladder, reminders, email, the unanswered sweep.'},
    {'id': 'postvisit', 'title': 'Visit, quote and plan path',
     'blurb': 'After the visit: the debrief form, quotes and their follow-up sequence; the plan path; future-dated visit proposals; phone quotes.'},
    {'id': 'ops', 'title': 'Dashboard operations',
     'blurb': 'What staff do on the dashboard and how each action feeds the automated flow.'},
    {'id': 'platform', 'title': 'Platform and billing',
     'blurb': 'The operator side: tenants, their config and lead magnet, client intake, invoices, payments and billing reminders.'},
]

NODES = []
EDGES = []


def N(id, kind, section, label, code=(), copy=(), note=''):
    NODES.append({'id': id, 'kind': kind, 'section': section, 'label': label,
                  'code': list(code), 'copy': list(copy), 'note': note})


def E(a, b, label='', kind='flow'):
    EDGES.append({'from': a, 'to': b, 'label': label, 'kind': kind})


W = 'bot/whatsapp_webhook.py::'
R = 'bot/whatsapp_webhook.py::_generate_and_schedule_reply#'
G = 'ResponseMixin.generate_response#'
O = 'bot/out_of_scope_handler.py::'
F = 'bot/management/commands/send_followups.py::Command.'


# ═══ Inbound message ═════════════════════════════════════════════════════════
S = 'inbound'
N('in_webhook', 'trigger', S, 'WhatsApp webhook event arrives',
  code=[W + 'handle_webhook_event', W + 'process_webhook_in_background'],
  note='Meta Cloud API POSTs to /webhook/. Processed in a background thread so Meta gets its 200 at once.')
N('in_tenant', 'decision', S, 'Which tenant owns this number?',
  code=[W + '_resolve_tenant_for_value'],
  note='No tenant means the event is unroutable and is dropped, never attributed to the wrong business.')
N('in_drop', 'end', S, 'Drop the event')
N('in_status', 'decision', S, 'Status update (sent / delivered / read)?',
  code=[W + 'process_message_change#HANDLE STATUSES FIRST'])
N('in_status_rec', 'data', S, 'Record delivery status and send cost',
  code=[W + 'process_status_updates', W + '_record_send_cost'])
N('in_wamid', 'gate', S, 'WAMID seen before?',
  code=[W + 'process_message_change#if message_id:'],
  note='A unique WhatsAppInboundEvent row per message id; an IntegrityError is a duplicate delivery and is skipped. Never remove this (CLAUDE.md).')
N('in_dup', 'end', S, 'Ignore the duplicate')
N('in_type', 'decision', S, 'Message type?',
  code=[W + "process_message_change#if message_type == 'text':"])
N('in_audio', 'send', S, 'Voice note: ask them to type it',
  code=[W + 'handle_audio_message', W + '_voice_note_topic'],
  copy=['catalog:VOICE_NOTE_ASK', 'catalog:VOICE_NOTE_CONTEXT', 'catalog:VOICE_NOTE_ASKED'],
  note='We cannot play voice notes. When we were waiting on an answer the reply names it, so the chat never dead-ends.')
N('in_sticker', 'send', S, 'Sticker or unsupported media reply',
  code=[W + 'handle_unsupported_media'], copy=[W + 'handle_unsupported_media'])
N('in_location', 'step', S, 'Location pin read as the area',
  code=[W + 'handle_location_message'])
N('in_contacts', 'step', S, 'Contact card logged as a marker turn',
  code=[W + 'handle_contacts_message', 'bot/shared_contact.py::marker_text'],
  note='The card becomes a marker line in the transcript and is read by the contact-card step of the router.')
N('in_media', 'step', S, 'Download media, vision describes it',
  code=[W + 'handle_media_message'],
  note='Images and documents are stored, described by the vision model and logged with the description.')
N('in_is_plan', 'decision', S, 'Is it their own plan or drawing?',
  code=[W + 'handle_media_message#_verified_plan = False', 'bot/plan_detection.py::is_their_own_plan', W + '_description_is_a_plan'],
  note='A PDF, or an image vision calls a plan drawing, AND it is theirs (not our own catalogue sent back). has_plan alone never puts a lead on the plan path.')
N('in_plan', 'data', S, 'Lead goes on the plan path (plan_status)',
  code=[W + 'handle_media_message#A plan on file puts the lead on the PLAN path'])
N('in_list', 'decision', S, 'A photographed materials list?',
  code=['bot/services/vision.py::transcribe_materials_list', 'bot/materials_list.py::looks_like_materials_list'],
  note='Vision says "written materials list"; a second detail=high call reads every line onto the photo turn. Nothing is priced until the lead asks.')
N('in_media_ack', 'send', S, 'Media ack: what we saw, then the next question',
  code=[W + '_compose_media_ack', W + '_media_ack_reply', W + '_schedule_media_ack', 'bot/photo_ask.py::seen_line'],
  copy=[W + '_compose_media_ack', W + '_materials_list_question', 'bot/photo_ask.py::seen_line', 'bot/photo_ask.py::list_seen_line'],
  note='Names what vision reported, then asks the next field (the approved two-slot ask once they are ready for a day). Goes through finalise_outbound.')
N('in_media_alert', 'notify', S, 'Plumber alerted with the file',
  code=[W + '_schedule_plumber_alert'])
N('in_text', 'gate', S, 'Same text from this sender seconds ago?',
  code=[W + '_is_duplicate_text_event'],
  note='A second net after WAMID dedup, for a retried delivery that arrives with a new id.')
N('in_log', 'data', S, 'Find or create the lead, log the user turn',
  code=[W + 'handle_text_message#get_or_create_lead'],
  note='Source attribution, the CTWA 72h window, the quoted ("highlighted") message resolved from its WAMID, the turn logged, last response stamped.')
N('in_postvisit', 'step', S, 'Post-visit lead names a day? move to confirm',
  code=['bot/post_visit.py::note_inbound_reply'])
N('in_delay', 'decision', S, 'Delay signal in this message?',
  code=[O + 'detect_delay_signal_message', O + 'mark_delay_signal'])
N('in_delay_set', 'data', S, 'Mark [DELAY_SIGNAL]',
  code=[O + 'mark_delay_signal'])
N('in_ack_hold', 'decision', S, 'Bare ack while delayed? keep the pause',
  code=[O + 'should_hold_silently', W + '_clear_delay_signal_if_present'],
  note='"ok" or "thanks" after the delay farewell keeps the hold; anything substantive clears it.')
N('in_svc', 'ai_write', S, 'Classify the service type (once)',
  code=['bot/service_type_classifier.py::classify_and_save'])
N('in_score', 'notify', S, 'Lead score; HOT lead alerts the admin',
  code=[W + 'notify_admin_of_priority_lead'])
N('in_batch', 'wait', S, 'Debounce: rapid texts become one turn',
  code=[W + '_enqueue_for_response', W + '_flush_text_batch'],
  note='Messages inside the batch window reset the timer and are answered together; an in-flight delayed send is cancelled. A batched turn is newline-joined, so exact-match checks on it fail.')

E('in_webhook', 'in_tenant')
E('in_tenant', 'in_drop', 'no tenant', 'no')
E('in_tenant', 'in_status', 'tenant found')
E('in_status', 'in_status_rec', 'yes')
E('in_status', 'in_wamid', 'messages', 'no')
E('in_wamid', 'in_dup', 'seen')
E('in_wamid', 'in_type', 'new', 'no')
E('in_type', 'in_text', 'text')
E('in_type', 'in_media', 'image / document / video')
E('in_type', 'in_audio', 'audio / voice')
E('in_type', 'in_sticker', 'sticker')
E('in_type', 'in_location', 'location')
E('in_type', 'in_contacts', 'contacts')
E('in_media', 'in_is_plan')
E('in_is_plan', 'in_plan', 'yes')
E('in_is_plan', 'in_list', 'no', 'no')
E('in_plan', 'in_media_ack')
E('in_list', 'in_media_ack')
E('in_media', 'in_media_alert')
E('in_text', 'in_dup', 'duplicate')
E('in_text', 'in_log', 'new', 'no')
E('in_contacts', 'in_batch')
E('in_log', 'in_postvisit')
E('in_postvisit', 'in_delay')
E('in_delay', 'in_delay_set', 'yes')
E('in_delay', 'in_ack_hold', 'no', 'no')
E('in_delay_set', 'in_svc')
E('in_ack_hold', 'in_svc')
E('in_svc', 'in_score')
E('in_score', 'in_batch')

# ═══ Router preamble ═════════════════════════════════════════════════════════
S = 'router'
N('ro_start', 'trigger', S, 'Reply turn starts',
  code=['bot/whatsapp_webhook.py::_generate_and_schedule_reply'],
  note='Router order IS the logic: the first step to produce a reply wins (bot/CLAUDE.md).')
N('ro_paused', 'decision', S, 'Bot paused for this lead?',
  code=[R + 'if appointment.chatbot_paused:'])
N('ro_silent', 'end', S, 'No reply (staff has the chat)')
N('ro_postack', 'decision', S, 'Booked lead sent a bare thanks?',
  code=[R + 'is_post_booking_ack_message(message_body)', W + 'is_post_booking_ack_message'])
N('ro_email', 'data', S, 'Capture any email address in the message',
  code=[R + "if not (appointment.customer_email or '').strip():"],
  note='Only fills a blank customer_email. An address given at any moment is kept even when the flow has moved on.')
N('ro_stop', 'gate', S, 'Hard stop ("stop messaging me")?',
  code=[R + 'HARD STOP', O + 'is_hard_stop_request', W + '_mark_stop_requested'],
  note='Marks [STOP_REQUESTED], which every cron reads too, so the chasing ends permanently.')
N('ro_stop_send', 'send', S, 'Hard-stop acknowledgement',
  code=[O + 'build_hard_stop_reply'], copy=[O + 'build_hard_stop_reply'])
N('ro_contact', 'decision', S, 'Contact card, or answer to our card question?',
  code=[R + 'A contact card, or the answer', 'bot/shared_contact.py::handle_turn'])
N('ro_contact_send', 'send', S, 'Contact card reply',
  code=['bot/shared_contact.py::handle_turn'],
  copy=['catalog:CONTACT_WILL_CALL', 'catalog:CONTACT_ASK', 'catalog:CONTACT_KEPT'],
  note='Card plus "call them": the plumber is emailed a call script at once. Card alone: we ask. Our ask answered: yes calls, no keeps the number.')
N('ro_contact_brief', 'notify', S, 'Plumber emailed a call script',
  code=['bot/shared_contact.py::send_brief'])
N('ro_uc', 'ai_write', S, 'Unified DeepSeek turn: intent, product, fields, next move',
  code=[R + 'UNIFIED DEEPSEEK CLASSIFIER', 'bot/unified_classifier.py::unified_turn'],
  note='One call per turn returns every classification the router needs, plus the controller plan. On failure every field is None, so each field the flow depends on has a deterministic floor.')
N('ro_kwdate', 'step', S, 'Bare weekday becomes a date (no LLM)',
  code=[W + '_keyword_availability_date'])
N('ro_shadow', 'step', S, 'Controller shadow log',
  code=[R + 'CONTROLLER SHADOW MODE', 'bot/controller.py::record_plan'])
N('ro_move', 'ai_decision', S, 'Controller move?',
  code=[R + 'CONTROLLER ROUTING', 'bot/controller.py::decide_move', 'bot/controller.py::apply_fee_gate', 'bot/controller.py::apply_plan_path_gate'],
  note='Drives only book_visit, handle_objection, show_work and close_pleasantry. Held back when the lead is answering the close, asks us something, or a booking is half made. PLUMBOT_CONTROLLER_ROUTING=0 turns it off.')
N('ro_norm', 'step', S, 'Remember the English rendering (rules only)',
  code=[R + 'Inbound language normalisation', 'bot/message_normalizer.py::remember'],
  note='Deterministic resolvers match English; a Shona message is handed to them in English. Nothing customer-facing reads it.')
N('ro_area', 'decision', S, 'Area in this message?',
  code=[R + 'Area backfill'],
  note='The classifier first, else a deterministic read when we just asked the area. Stored BEFORE routing so no branch below can lose it.')
N('ro_area_excl', 'decision', S, 'Excluded city?',
  code=['_is_excluded_city'])
N('ro_area_set', 'data', S, 'Store customer_area')
N('ro_proof_gate', 'gate', S, "Lead's words outrank the proof step?",
  code=[R + "if _move == 'show_work':"],
  note='A delay, a deferral, a price question, or a portfolio already sent cancels show_work.')
N('ro_proof', 'send', S, 'Proof step: 2 or 3 matched past jobs, then the next question',
  code=[R + 'THE PROOF STEP', 'bot/portfolio_catalog.py::proof_images_for_job', 'bot/controller_templates.py::show_examples'],
  copy=['bot/controller_templates.py::show_examples', 'bot/controller_templates.py::photo_followup'],
  note='Photos matched to their own words; no match sends nothing. The scripted question rides behind the photos only when it has not been asked yet.')
N('ro_move_send', 'send', S, 'Controller reply: visit close, fee objection or pleasantry',
  code=[R + 'if _move:', 'bot/controller_templates.py::paid_visit_close'],
  copy=['bot/controller_templates.py::paid_visit_close', 'bot/controller_templates.py::fee_objection', 'bot/controller_templates.py::close_pleasantry'])

E('ro_start', 'ro_paused')
E('ro_paused', 'ro_silent', 'paused')
E('ro_paused', 'ro_postack', 'no', 'no')
E('ro_postack', 'ro_silent', 'yes')
E('ro_postack', 'ro_email', 'no', 'no')
E('ro_email', 'ro_stop')
E('ro_stop', 'ro_stop_send', 'stop')
E('ro_stop', 'ro_contact', 'no', 'no')
E('ro_contact', 'ro_contact_send', 'yes')
E('ro_contact_send', 'ro_contact_brief')
E('ro_contact', 'ro_uc', 'no', 'no')
E('ro_uc', 'ro_kwdate')
E('ro_kwdate', 'ro_shadow')
E('ro_shadow', 'ro_move')
E('ro_move', 'ro_norm')
E('ro_norm', 'ro_area')
E('ro_area', 'ro_area_excl', 'area given')
E('ro_area_excl', 'ro_area_set', 'no', 'no')
E('ro_area', 'ro_proof_gate', 'none', 'no')
E('ro_area_set', 'ro_proof_gate')
E('ro_area_excl', 'ro_proof_gate', 'excluded')
E('ro_proof_gate', 'ro_proof', 'show_work stands')
E('ro_proof_gate', 'ro_move_send', 'other move')
E('ro_proof_gate', 'p_sc', 'no move', 'no')
E('ro_proof', 'p_sc', 'no photos match', 'no')
E('in_batch', 'ro_start', 'batch flushes', 'handoff')

# ═══ Answers to our last question ═══════════════════════════════════════════
S = 'pending'
N('p_sc', 'decision', S, 'Waiting on "is X the only thing?"',
  code=[R + 'SERVICE-CONFIRM FOLLOW-UP'],
  note='[SERVICE_CONFIRM_PENDING] / [AWAITING_MORE_ITEMS]. One-shot tags.')
N('p_sc_delay', 'gate', S, 'A delay signal outranks the scope question',
  code=[R + '_sc_delay_override = ('])
N('p_sc_more', 'send', S, 'Ask what else to sort while we are there',
  code=[R + "elif _aff == 'no':"], copy=[R + "elif _aff == 'no':"])
N('p_sc_adv', 'send', S, 'Scope captured: advance to booking',
  code=['ResponseMixin._advance_after_scope'], copy=['ResponseMixin._advance_after_scope'])
N('p_vc', 'decision', S, '"Nothing else" after our value check?',
  code=[R + 'VALUE-CHECK "NOTHING ELSE"'])
N('p_detail', 'decision', S, '"No" to our request for detail?',
  code=[R + '"NO" TO THE REQUEST FOR PROJECT DETAIL', 'ResponseMixin._handle_no_to_detail_request'])
N('p_detail_send', 'send', S, 'Stop asking for detail, move on',
  code=['ResponseMixin._handle_no_to_detail_request', 'ResponseMixin._get_first_pass_question'],
  note='No copy of its own: the declined detail becomes the service type and the next scripted question goes out.')
N('p_lock', 'decision', S, '"No" to the lock-in close?',
  code=[R + '"NO" TO THE LOCK-IN CLOSE', 'ResponseMixin._handle_no_to_lock_in'])
N('p_lock_send', 'send', S, 'Price or timing? one question, two options',
  code=['ResponseMixin._handle_no_to_lock_in'], copy=['ResponseMixin._handle_no_to_lock_in'])
N('p_slot', 'decision', S, '"No" or "neither" to our day or time?',
  code=[R + '"NO" / "NEITHER" TO A DAY OR TIME OFFER', 'ResponseMixin._handle_no_to_slot_offer'])
N('p_slot_send', 'send', S, 'Open the question up (never the same two slots)',
  code=['ResponseMixin._handle_no_to_slot_offer'], copy=['ResponseMixin._handle_no_to_slot_offer'])
N('p_nb', 'decision', S, '"No" to the new-build confirmation?',
  code=[R + '"NO" TO THE NEW-BUILD CONFIRMATION', 'ResponseMixin._handle_new_build_rejection'])
N('p_nb_send', 'send', S, 'Clear the wrong guess, ask what the job is',
  code=['ResponseMixin._handle_new_build_rejection'], copy=['ResponseMixin._handle_new_build_rejection'])
N('p_pivot', 'ai_decision', S, 'At the date stage, pivots to a timeline?',
  code=[R + 'DATE-STAGE TIMELINE PIVOT', 'ResponseMixin._dispatch_timeline_pivot'],
  note='DeepSeek resolved the date; code does the math. More than 7 days out parks the lead, within a week keeps booking. Not while the delay flow is waiting on this answer.')
N('p_pivot_send', 'send', S, 'Timeline reply: park far, keep booking near',
  code=['ResponseMixin._dispatch_timeline_pivot'], copy=['ResponseMixin._dispatch_timeline_pivot'])
N('p_list', 'decision', S, 'Price asked on their materials list?',
  code=[R + 'A PHOTOGRAPHED MATERIALS LIST', W + '_materials_list_price_reply'])
N('p_list_send', 'send', S, "Materials list priced from the tenant's own quote lines",
  code=['bot/materials_list.py::build_list_price_reply'],
  copy=['bot/materials_list.py::build_list_price_reply'],
  note='Owner-approved copy (2026-09-18). Unpriced items are never listed; a line nothing matches is left for the quote, never guessed.')
N('p_faq', 'decision', S, 'FAQ topic (or an AI-routed service question)?',
  code=[R + 'FAQ LAYER', 'bot/faq.py::match_faq_topic', 'bot/faq.py::faq_fact'],
  note='Skipped for delay, complaint, out-of-scope and explicit photo or catalogue asks. A topic this tenant has no fact for falls through.')
N('p_faq_first', 'send', S, 'First ask: the exact FAQ fact plus a tie-down',
  code=['ResponseMixin._append_tiedown', 'ResponseMixin._service_continuation_reply'],
  copy=['ResponseMixin._service_continuation_reply'],
  note='Script first, vary on retry: the first answer to a topic is the fixed fact.')
N('p_faq_repeat', 'ai_write', S, 'Repeat of the topic: DeepSeek paraphrases the fact',
  code=['ResponseMixin.ai_answer_faq'], copy=['ResponseMixin.ai_answer_faq'])

E('p_sc', 'p_sc_delay', 'yes')
E('p_sc_delay', 'p_vc', 'delay: hand on', 'no')
E('p_sc_delay', 'p_sc_more', 'plain no')
E('p_sc_delay', 'p_sc_adv', 'named more / answered')
E('p_sc', 'p_vc', 'no', 'no')
E('p_vc', 'p_sc_adv', 'yes')
E('p_vc', 'p_detail', 'no', 'no')
E('p_detail', 'p_detail_send', 'yes')
E('p_detail', 'p_lock', 'no', 'no')
E('p_lock', 'p_lock_send', 'yes')
E('p_lock', 'p_slot', 'no', 'no')
E('p_slot', 'p_slot_send', 'yes')
E('p_slot', 'p_nb', 'no', 'no')
E('p_nb', 'p_nb_send', 'yes')
E('p_nb', 'p_pivot', 'no', 'no')
E('p_pivot', 'p_pivot_send', 'yes')
E('p_pivot', 'p_list', 'no', 'no')
E('p_list', 'p_list_send', 'yes')
E('p_list', 'p_faq', 'no', 'no')
E('p_faq', 'p_faq_first', 'first time')
E('p_faq', 'p_faq_repeat', 'repeat')
E('p_faq', 'i_here', 'no topic', 'no')

# ═══ Requests and objections ════════════════════════════════════════════════
S = 'intents'
N('i_here', 'decision', S, '"Send it here" again, PDF already sent?',
  code=[R + 'STEP 0-a'])
N('i_here_send', 'send', S, 'It is the PDF just above',
  copy=['catalog:PORTFOLIO_ALREADY_HERE'],
  note='Goes through the model reader so it fits their words (context over the script).')
N('i_link', 'decision', S, 'Asks about the plumber link?',
  code=[R + 'STEP 0-:', 'bot/plumber_link.py::asks_about_the_link'],
  note='"What is this link?", "why is it so long?", "is this a scam?" right after we sent it.')
N('i_link_send', 'send', S, 'What the link is, then the contact card',
  code=['bot/plumber_link.py::link_explanation', W + '_send_reply_then_contact_card'],
  copy=['catalog:LINK_WHAT_IT_IS', 'catalog:LINK_WHAT_IT_IS_NAMELESS', 'catalog:LINK_WHY_LONG',
        'catalog:LINK_NOTHING_ELSE', 'catalog:LINK_OR_MESSAGE_DIRECTLY', 'catalog:LINK_OR_MESSAGE_DIRECTLY_NAMELESS'])
N('i_hes', 'decision', S, 'Hesitating over the visit, or pushing for an exact figure?',
  code=[R + 'STEP 0-h', 'bot/hesitation.py::reply_for', 'bot/hesitation.py::is_visit_hesitation', 'bot/hesitation.py::pushes_for_exact_figure'],
  note='Live chats lead with the visit; the online quote is the second door, offered ONCE. English only, never to a booked lead.')
N('i_hes_send', 'send', S, 'No pressure on the visit: free online quote first',
  code=['bot/hesitation.py::reply_for', 'bot/plumber_link.py::portfolio_handoff'],
  copy=['catalog:HESITATION_ACK', 'catalog:ONLINE_QUOTE_STILL_OPEN', 'catalog:ONLINE_QUOTE_STILL_OPEN_NAMELESS',
        'catalog:FIRM_PRICE_NO_GUESS', 'catalog:FIRM_PRICE_ONLINE',
        'catalog:PORTFOLIO_HANDOFF_FREE_FIRST', 'catalog:PORTFOLIO_HANDOFF_WHO', 'catalog:PORTFOLIO_HANDOFF_WHO_NAMELESS',
        'catalog:PORTFOLIO_HANDOFF_CERTAINTY', 'catalog:PORTFOLIO_LINK_LONG', 'catalog:PORTFOLIO_LINK_DETAILS',
        'catalog:PORTFOLIO_LINK_READY', 'catalog:HESITATION_ACK_SN', 'catalog:ONLINE_QUOTE_STILL_OPEN_SN',
        'catalog:ONLINE_QUOTE_STILL_OPEN_NAMELESS_SN', 'catalog:FIRM_PRICE_NO_GUESS_SN', 'catalog:FIRM_PRICE_ONLINE_SN',
        'catalog:PORTFOLIO_HANDOFF_FREE_FIRST_SN', 'catalog:PORTFOLIO_HANDOFF_WHO_SN', 'catalog:PORTFOLIO_HANDOFF_WHO_NAMELESS_SN',
        'catalog:PORTFOLIO_HANDOFF_CERTAINTY_SN', 'catalog:PORTFOLIO_LINK_LONG_SN', 'catalog:PORTFOLIO_LINK_DETAILS_SN',
        'catalog:PORTFOLIO_LINK_READY_SN'])
N('i_choice', 'decision', S, 'Answer to "quick look or quote online first?"',
  code=[R + 'STEP 0-b', 'bot/price_guide.py::read_choice'],
  note='[PRICE_CHOICE_PENDING] is cleared this turn whatever they say; a delay or a reply that picks neither falls through.')
N('i_online', 'send', S, 'Online: the plumber, his WhatsApp link, their details typed in',
  code=['bot/plumber_link.py::quote_offer'],
  copy=['catalog:PLUMBER_QUOTE_OFFER', 'catalog:PLUMBER_QUOTE_OFFER_NAMELESS', 'catalog:LINK_OPENS_WITH_DETAILS',
        'catalog:LINK_OPENS_READY', 'catalog:LINK_WHY_LONG_TAIL'])
N('i_multi', 'decision', S, 'Two or more info questions in one message?',
  code=[R + 'STEP 0: Multi', 'ResponseMixin.compose_multi_answer'],
  note='English only. Drops through when every price it would answer was already sent.')
N('i_multi_send', 'send', S, 'One reply answering every question',
  code=['ResponseMixin.compose_multi_answer'], copy=['ResponseMixin.compose_multi_answer'])
N('i_gallery', 'decision', S, 'Asks for pictures of our work?',
  code=[R + 'STEP 0a', W + '_explicitly_requests_photos'])
N('i_gallery_send', 'send', S, 'The whole previous-work gallery',
  code=[W + 'send_previous_work_photos', W + 'generate_photo_followup'],
  copy=[W + 'send_previous_work_photos', 'bot/controller_templates.py::photo_followup'])
N('i_piece', 'decision', S, 'Points at one portfolio piece?',
  code=[R + 'STEP 0b', 'bot/portfolio_catalog.py::match_portfolio_item'])
N('i_piece_send', 'send', S, 'That piece with its caption',
  code=[W + 'send_portfolio_item', 'bot/portfolio_catalog.py::build_item_caption'],
  note='The image goes with its caption, which is the piece title only (prices live in the catalogue, never invented).')
N('i_menu', 'decision', S, '"What can you show me?"',
  code=[R + 'STEP 0c', 'bot/portfolio_catalog.py::is_catalogue_menu_request'])
N('i_menu_send', 'send', S, 'Text menu of pieces',
  code=['bot/portfolio_catalog.py::catalogue_overview'], copy=['bot/portfolio_catalog.py::catalogue_overview'])
N('i_cat', 'decision', S, 'Products AND prices?',
  code=[R + 'STEP 0d', W + '_explicitly_requests_catalogue'])
N('i_cat_send', 'send', S, 'Catalogue images plus the price list',
  code=[W + 'send_catalogue_images', W + 'build_catalogue_price_text', W + 'catalogue_price_lines'],
  copy=[W + 'build_catalogue_price_text'])
N('i_photo', 'ai_decision', S, 'Photo request (explicit, or the classifier)?',
  code=[R + 'STEP 1:'])
N('i_photo_send', 'send', S, 'Work photos, with any definition asked alongside',
  code=[R + 'STEP 1:', W + '_definition_answer'], copy=[R + 'STEP 1:'])
N('i_budget', 'decision', S, 'Answering our price tie-down or budget ask?',
  code=[R + 'STEP 1a', 'ResponseMixin._last_assistant_was_price_tiedown', 'ResponseMixin._is_budget_decline', 'ResponseMixin._budget_figure'],
  note='The budget ladder (owner, 2026-09-19): tie-down, a no gets "how much were you hoping to invest?", their figure gets what fits. Never a discount.')
N('i_budget_ask', 'send', S, 'How much were you hoping to invest?',
  code=['ResponseMixin._build_budget_ask', 'ResponseMixin._handle_budget_objection'],
  copy=['ResponseMixin._build_budget_ask'])
N('i_budget_opts', 'send', S, 'Our options at or under their figure',
  code=['ResponseMixin._build_budget_options_reply'], copy=['ResponseMixin._build_budget_options_reply'])

E('i_here', 'i_here_send', 'yes')
E('i_here', 'i_link', 'no', 'no')
E('i_link', 'i_link_send', 'yes')
E('i_link', 'i_hes', 'no', 'no')
E('i_hes', 'i_hes_send', 'yes')
E('i_hes', 'i_choice', 'no', 'no')
E('i_choice', 'i_online', 'online')
E('i_choice', 'q_avail', 'a look', 'handoff')
E('i_choice', 'i_multi', 'neither / delay', 'no')
E('i_multi', 'i_multi_send', 'yes')
E('i_multi', 'i_gallery', 'no', 'no')
E('i_gallery', 'i_gallery_send', 'yes')
E('i_gallery', 'i_piece', 'no', 'no')
E('i_piece', 'i_piece_send', 'yes')
E('i_piece', 'i_menu', 'no', 'no')
E('i_menu', 'i_menu_send', 'yes')
E('i_menu', 'i_cat', 'no', 'no')
E('i_cat', 'i_cat_send', 'yes')
E('i_cat', 'i_photo', 'no', 'no')
E('i_photo', 'i_photo_send', 'yes')
E('i_photo', 'i_budget', 'no', 'no')
E('i_budget', 'i_budget_ask', 'no to tie-down')
E('i_budget', 'i_budget_opts', 'gave a figure')
E('i_budget', 'd_oos', 'neither', 'no')

# ═══ Delay, exit and complaint ══════════════════════════════════════════════
S = 'delay'
N('d_oos', 'step', S, 'Out-of-scope / delay / complaint handler',
  code=[R + 'STEP 1b', O + 'handle_out_of_scope'],
  note='The delay flow is sent with check=False: the model reader kept tidying the timeframe and email asks away.')
N('d_pitch', 'decision', S, 'Someone selling TO us?',
  code=[O + 'is_inbound_sales_pitch'])
N('d_pitch_send', 'send', S, 'Polite no-thanks to a sales pitch',
  code=[O + 'build_sales_pitch_reply'], copy=[O + 'build_sales_pitch_reply'])
N('d_pending', 'decision', S, 'Delay flow waiting on an answer? which step',
  code=[O + 'handle_out_of_scope#Step 1: pending states', O + '_read_pending'],
  note='[OOS_PENDING] in internal_notes: delay_timeframe, delay_confirm, delay_checkin, delay_email, delay_call_ok, or a clarification.')
N('d_cat', 'ai_decision', S, 'Category: in scope, delay, out of scope, complaint',
  code=[O + 'handle_out_of_scope#Step 2: classify', O + 'classify_message'])
N('d_has_tf', 'decision', S, 'Timeframe already in the message?',
  code=[O + 'handle_out_of_scope#Step 4: delay signal', O + '_message_has_timeframe'])
N('d_sub', 'ai_decision', S, 'Delay subtype?',
  code=[O + '_build_delay_reply', O + '_classify_delay_subtype'])
N('d_brush', 'send', S, 'Brush-off: no pressure, roughly when?',
  code=[O + "_build_delay_reply#if subtype == 'brush_off':"],
  copy=[O + "_build_delay_reply#if subtype == 'brush_off':"])
N('d_compare', 'send', S, 'Comparison shopping: past jobs plus a like-for-like tip',
  code=[O + "_build_delay_reply#if subtype == 'comparison_shopping':"],
  copy=[O + "_build_delay_reply#if subtype == 'comparison_shopping':"])
N('d_access', 'send', S, 'Access or keys: a check-in time',
  code=[O + '_build_access_checkin_reply'], copy=[O + '_build_access_checkin_reply'])
N('d_selfdefer', 'send', S, 'Self-initiated defer: ask the timeframe again',
  code=[O + '_reask_delay_timeframe'], copy=[O + '_reask_delay_timeframe'])
N('d_nudge', 'send', S, 'Other delay: the slow-lead timeframe ask',
  code=['bot/controller_templates.py::slow_lead_nudge', O + '_DELAY_SUBTYPE_REPLIES'],
  copy=['bot/controller_templates.py::slow_lead_nudge', 'bot/controller_templates.py::_timeframe_ask', O + '_DELAY_SUBTYPE_REPLIES'])
N('d_pend_tf', 'data', S, 'Pending: delay_timeframe',
  code=[O + '_write_pending', O + '_note_timeframe_asked'])
N('d_tf', 'decision', S, 'Timeframe answer: no date, near or far?',
  code=[O + '_handle_delay_timeframe_answer', O + '_compute_followup_date'],
  note='A vague timeframe ("month end") is an answer: a date is assumed inside it (bot/vague_dates), never asked again.')
N('d_near2', 'send', S, 'Near, "contact me on Monday": ask the email',
  code=[O + "_handle_delay_timeframe_answer#if _near and _english:"],
  copy=['catalog:NEAR_EMAIL_ASK', 'catalog:NEAR_WE_WILL_CALL'],
  note='Type 2 near-date lead. No email: the plumber calls them that morning (bot/near_date_call.py).')
N('d_near1', 'send', S, 'Near, "I will contact you": we wait',
  code=[O + '_near_they_will_contact'],
  copy=['catalog:NEAR_WAIT_FOR_THEM', 'catalog:NEAR_EMAIL_FOR_PORTFOLIO', 'catalog:NEAR_PORTFOLIO_EMAILED'],
  note='Type 1 near-date lead. No chasing; if the day passes with no word the plumber calls the morning after.')
N('d_near_book', 'send', S, 'Near and ready: say the day back, ask the time',
  code=[O + '_handle_delay_timeframe_answer#if _near and not _near_contact_with_email and not _self_defer:'],
  copy=[O + '_handle_delay_timeframe_answer#if _near and not _near_contact_with_email and not _self_defer:'])
N('d_ladder', 'data', S, 'Far: arm the job-date ladder (check back at job minus 7)',
  code=[O + '_ladder_delay_reply', 'bot/job_date_ladder.py::arm', O + '_store_delay_followup_date'],
  copy=[O + '_ladder_delay_reply'],
  note='Handoff brief Rule 1: the date they named is the JOB date; -7 and -3 touches and the plumber call at -2.')
N('d_has_email', 'decision', S, 'Email already on file?',
  code=[O + "_handle_delay_timeframe_answer#if getattr(appointment, 'customer_email', None):"])
N('d_confirm_send', 'send', S, 'Confirm the check-back date',
  copy=[O + "_handle_delay_timeframe_answer#if getattr(appointment, 'customer_email', None):"])
N('d_email_ask', 'send', S, 'Ask the email the portfolio goes to',
  code=[O + '_delay_email_ask'], copy=[O + '_delay_email_ask', 'catalog:BEST_EMAIL_ASK'])
N('d_email', 'decision', S, 'Email step answer: an email, WhatsApp, decline or unclear?',
  code=[O + '_handle_delay_email_answer', O + '_classify_email_step_reply'])
N('d_email_sent', 'email', S, 'Portfolio PDF emailed, check-back scheduled',
  code=[O + '_deliver_pdf_and_schedule_checkin'],
  copy=[O + '_handle_delay_email_answer', 'catalog:NEAR_EMAIL_THANKS'])
N('d_wa_portfolio', 'send', S, 'Portfolio on WhatsApp with the plumber handoff',
  code=[O + '_portfolio_on_whatsapp_ack', O + 'send_lead_magnet_on_whatsapp'],
  copy=['catalog:PORTFOLIO_HERE_ACK', 'catalog:PORTFOLIO_SENT_ACK', O + '_portfolio_on_whatsapp_ack'])
N('d_call_ask', 'send', S, 'Job-date lead with no email: may we call?',
  copy=['catalog:LADDER_CALL_ASK'],
  note='Asked, never announced (owner: no call without permission).')
N('d_call', 'decision', S, 'Answer to "may we call?"',
  code=[O + '_handle_call_permission_answer'])
N('d_call_send', 'send', S, 'Call confirmed, or respected no',
  copy=['catalog:LADDER_CALL_YES', 'catalog:LADDER_CALL_NO'])
N('d_choice_q', 'send', S, 'Unclear: email or WhatsApp?',
  code=[O + '_delivery_choice_question'], copy=[O + '_DELIVERY_CHOICE_QUESTION', O + '_DELIVERY_CHOICE_TIMEFRAME_TAIL'])
N('d_other_pending', 'send', S, 'Other pending answers (confirm, check-in, clarification)',
  code=[O + '_handle_delay_confirm_answer', O + '_handle_delay_checkin_answer', O + '_resolve_pending_clarification'],
  copy=[O + '_handle_delay_confirm_answer', O + '_handle_delay_checkin_answer'])
N('d_high', 'decision', S, 'High confidence?',
  code=[O + 'handle_out_of_scope#Step 5: HIGH confidence'])
N('d_oos_send', 'send', S, 'Out of scope: reframe to plumbing or redirect',
  code=[O + '_generate_clarifying_question', O + '_build_oos_reply'],
  copy=[O + '_build_oos_reply', O + '_fallback_clarifier', O + '_generate_clarifying_question'])
N('d_complaint', 'send', S, 'Complaint reply (and the team is told)',
  code=[O + '_build_complaint_reply'], copy=[O + '_build_complaint_reply'])
N('d_clarify', 'ai_write', S, 'Low confidence: one targeted clarifying question',
  code=[O + 'handle_out_of_scope#Step 6: LOW confidence', O + '_generate_clarifying_question'])

E('d_oos', 'd_pitch')
E('d_pitch', 'd_pitch_send', 'yes')
E('d_pitch', 'd_pending', 'no', 'no')
E('d_pending', 'd_tf', 'delay_timeframe')
E('d_pending', 'd_email', 'delay_email')
E('d_pending', 'd_call', 'delay_call_ok')
E('d_pending', 'd_other_pending', 'other')
E('d_pending', 'd_cat', 'none', 'no')
E('d_cat', 'pr_1c', 'in scope: carry on', 'handoff')
E('d_cat', 'd_has_tf', 'delay signal')
E('d_cat', 'd_high', 'out of scope / complaint')
E('d_has_tf', 'd_tf', 'yes')
E('d_has_tf', 'd_sub', 'no', 'no')
E('d_sub', 'd_brush', 'brush-off')
E('d_sub', 'd_compare', 'comparing quotes')
E('d_sub', 'd_access', 'access')
E('d_sub', 'd_selfdefer', "I'll let you know")
E('d_sub', 'd_nudge', 'busy / travel / money')
E('d_brush', 'd_pend_tf')
E('d_compare', 'd_pend_tf')
E('d_nudge', 'd_pend_tf')
E('d_selfdefer', 'd_pend_tf')
E('d_pend_tf', 'd_tf', 'their answer', 'loop')
E('d_tf', 'd_selfdefer', 'no date', 'no')
E('d_tf', 'd_near2', 'near, contact me')
E('d_tf', 'd_near1', "near, I'll contact you")
E('d_tf', 'd_near_book', 'near and ready')
E('d_tf', 'd_ladder', 'far (> 7 days)')
E('d_near_book', 'q_start', 'booking continues', 'handoff')
E('d_ladder', 'd_has_email')
E('d_has_email', 'd_confirm_send', 'yes')
E('d_has_email', 'd_email_ask', 'no', 'no')
E('d_email_ask', 'd_email', 'their answer', 'loop')
E('d_email', 'd_email_sent', 'an email')
E('d_email', 'd_wa_portfolio', 'WhatsApp / no')
E('d_email', 'd_choice_q', 'unclear')
E('d_wa_portfolio', 'd_call_ask', 'job-date lead, no email')
E('d_call_ask', 'd_call', 'their answer', 'loop')
E('d_call', 'd_call_send')
E('d_high', 'd_oos_send', 'out of scope')
E('d_high', 'd_complaint', 'complaint')
E('d_high', 'd_clarify', 'low', 'no')
E('d_near2', 'f_near', 'no email: plumber calls', 'handoff')
E('d_ladder', 'f_ladder', 'ladder armed', 'handoff')

# ═══ Pricing ═════════════════════════════════════════════════════════════════
S = 'pricing'
N('pr_1c', 'decision', S, 'Price asked on a highlighted photo of ours?',
  code=[R + 'STEP 1c', W + '_quoted_portfolio_item'])
N('pr_1c_send', 'send', S, "That photo's own price lines",
  code=[W + '_quoted_portfolio_price_reply', 'price_line_for_item'],
  note="The price lines are the tenant's own, stored on the photo; a photo with no price on file gets none.")
N('pr_1d', 'decision', S, 'Price question, and service, description and area known?',
  code=[R + 'STEP 1d', 'bot/price_guide.py::applies', 'bot/price_guide.py::three_fields'],
  note='English only. After STEP 1b, so a delay or exit still wins.')
N('pr_guide', 'send', S, 'Prices, then the price-guide PDF, then "a look or online?"',
  code=[W + '_start_price_guide', W + '_send_price_guide', W + '_price_guide_prices'],
  copy=['catalog:PRICE_GUIDE_INTRO', 'catalog:PRICE_GUIDE_ALREADY_SENT', 'catalog:PRICE_CHOICE_ASK',
        'catalog:PRICE_GUIDE_INTRO_SN', 'catalog:PRICE_GUIDE_ALREADY_SENT_SN', 'catalog:PRICE_CHOICE_ASK_SN'],
  note='The PDF only if the intro really went; the choice tag opens only once the question is out.')
N('pr_choice_tag', 'data', S, 'Pending: [PRICE_CHOICE_PENDING]',
  code=['bot/price_guide.py::read_choice'])
N('pr_skip', 'gate', S, 'Describing the project, carried-over intent, or mid-chat with no price ask?',
  code=[R + 'STEP 2:', W + '_is_unprompted_carryover_pricing'],
  note='Never volunteer a price: a lead naming a service while answering us is captured, not priced.')
N('pr_quote', 'decision', S, 'Asked for a QUOTE, not a figure?',
  code=['ResponseMixin._asks_for_quote', 'ResponseMixin._is_job_quote_request'],
  note='"A quote" leans to the free on-site look (the quote is delivered there); only how-much, price or cost gets chat prices.')
N('pr_quote_send', 'send', S, 'Job quote: we price it at a quick look',
  code=['ResponseMixin._build_job_quote_reply'], copy=['ResponseMixin._build_job_quote_reply'])
N('pr_multi', 'decision', S, 'A figure for several items, labour, or the scope on file?',
  code=['ResponseMixin._names_multiple_products', 'ResponseMixin._context_product_families'])
N('pr_multi_send', 'send', S, 'Combined approximate prices, supply and labour split',
  code=['ResponseMixin._build_combined_price_reply'], copy=['ResponseMixin._build_combined_price_reply'])
N('pr_single', 'decision', S, 'Priceable intent not already sent?',
  code=[W + '_has_sent_pricing_for_intent', W + '_mark_pricing_intent_sent'])
N('pr_single_send', 'send', S, 'Service price block, then the invest tie-down',
  code=['ResponseMixin.handle_service_inquiry', 'ResponseMixin._replacement_price_reply', 'ResponseMixin._price_tiedown'],
  copy=['ResponseMixin._price_tiedown', 'ResponseMixin._replacement_price_reply',
        'catalog:STARTING_PRICES_DISCLAIMER', 'catalog:STARTING_PRICES_DISCLAIMER_SN', 'catalog:MATERIALS_LABOUR_INCLUDED'],
  note='Every price shows materials and labour. A replacement is priced as the new fixture plus its mixer. A named tub prices only that tub.')
N('pr_2b', 'decision', S, 'Quote for a whole job, no product named?',
  code=[R + 'STEP 2b', W + '_routes_to_onsite_quote'])
N('pr_3', 'decision', S, 'Pricing objection wording?',
  code=[R + 'STEP 3:', W + 'detect_objection_type', W + '_is_genuine_pricing_question'])
N('pr_overview', 'send', S, 'Full pricing overview (or a recap once sent)',
  code=['ResponseMixin.generate_pricing_overview', 'ResponseMixin._pricing_overview_recap'],
  copy=['ResponseMixin.generate_pricing_overview', 'ResponseMixin._pricing_overview_recap'])
N('pr_3b', 'ai_decision', S, 'Asking something we already answered?',
  code=[R + 'STEP 3b', 'bot/repeated_question_detector.py::detect_repeated_question'])
N('pr_3b_send', 'ai_write', S, 'Repeat clarification',
  code=['bot/repeated_question_detector.py::generate_repeat_clarification'],
  copy=['bot/repeated_question_detector.py::generate_repeat_clarification'])
N('pr_3c', 'decision', S, 'A new build described?',
  code=[R + 'STEP 3c', 'ResponseMixin._new_build_confirmation'])
N('pr_3c_send', 'send', S, 'Confirm the new build back',
  code=['ResponseMixin._new_build_confirmation', 'ResponseMixin._new_build_confirm_question'],
  copy=['ResponseMixin._new_build_confirm_question'],
  note='A new build is noted with no yes/no when the area is not in yet: the area question goes out instead (owner, 2026-09-23). First contact is greeted.')

E('pr_1c', 'pr_guide', 'three fields known')
E('pr_1c', 'pr_1c_send', 'photo has a price')
E('pr_1c', 'pr_1d', 'no', 'no')
E('pr_1d', 'pr_guide', 'yes')
E('pr_guide', 'pr_choice_tag')
E('pr_choice_tag', 'i_choice', 'next turn', 'loop')
E('pr_1d', 'pr_skip', 'no', 'no')
E('pr_skip', 'pr_2b', 'skip pricing')
E('pr_skip', 'pr_quote', 'price may apply', 'no')
E('pr_quote', 'pr_quote_send', 'yes')
E('pr_quote', 'pr_multi', 'no', 'no')
E('pr_multi', 'pr_multi_send', 'yes')
E('pr_multi', 'pr_single', 'no', 'no')
E('pr_single', 'pr_single_send', 'yes')
E('pr_single', 'pr_2b', 'no / sent', 'no')
E('pr_2b', 'pr_quote_send', 'yes')
E('pr_2b', 'pr_3', 'no', 'no')
E('pr_3', 'pr_overview', 'yes')
E('pr_3', 'pr_3b', 'no', 'no')
E('pr_3b', 'pr_3b_send', 'yes')
E('pr_3b', 'pr_3c', 'no', 'no')
E('pr_3c', 'pr_3c_send', 'yes')
E('pr_3c', 'q_start', 'no', 'no')

# ═══ Qualification and booking ══════════════════════════════════════════════
S = 'qualify'
N('q_start', 'step', S, 'generate_response (STEP 4)',
  code=[R + 'STEP 4:', 'ResponseMixin.generate_response'],
  note='The main qualification flow. Its own branches run top to bottom; the first to answer wins.')
N('q_emailcap', 'decision', S, 'Post-booking email capture pending?',
  code=[G + 'EMAIL CAPTURE (post-booking)', 'StateMixin._handle_email_capture'])
N('q_delay', 'decision', S, 'Delay signal active?',
  code=[G + 'DELAY SIGNAL ACTIVE'])
N('q_exit', 'decision', S, 'First-time delay or exit signal?',
  code=[G + 'FIRST-TIME DELAY / EXIT SIGNAL'])
N('q_direct', 'decision', S, 'A direct question first?',
  code=[G + 'DIRECT QUESTION FIRST'])
N('q_budget', 'decision', S, 'A no to our budget tie-down?',
  code=[G + "BUDGET OBJECTION (a 'no'"])
N('q_materials', 'decision', S, 'Asking for a materials list?',
  code=[G + 'MATERIALS REQUEST'])
N('q_bareack', 'decision', S, 'Bare "ok" at the service-type stage?',
  code=[G + 'Bare ack ("ok"'])
N('q_oos', 'decision', S, 'Out of scope, delay or complaint (second net)?',
  code=[G + 'OUT-OF-SCOPE / DELAY / COMPLAINT HANDLER'])
N('q_size', 'decision', S, 'A size or spec question?',
  code=[G + 'PRODUCT SIZE / SPEC QUESTION'])
N('q_catalogue', 'decision', S, 'Catalogue or product list?',
  code=[G + 'CATALOGUE / PRODUCT LIST REQUEST'])
N('q_pricing', 'decision', S, 'Product pricing, before booking?',
  code=[G + 'SERVICE / PRODUCT PRICING INQUIRIES'])
N('q_plan', 'decision', S, 'Plan upload flow?',
  code=[G + 'PLAN UPLOAD FLOW', 'PlanUploadMixin.handle_plan_upload_flow'])
N('q_planfar', 'decision', S, 'Plan lead, timeline beyond the week?',
  code=[G + 'PLAN LEAD WHOSE TIMELINE IS BEYOND THE WEEK'],
  note='Far-out plan leads are nurtured, never pushed for a day.')
N('q_confirmed', 'decision', S, 'Confirmed and complete?',
  code=[G + 'CONFIRMED + COMPLETE'],
  note='Respond in context, never go silent, never re-pitch the visit.')
N('q_alt', 'decision', S, 'Picking one of the alternative times we listed?',
  code=[G + 'ALTERNATIVE TIME SELECTION', 'BookingMixin.process_alternative_time_selection', 'BookingMixin.book_appointment_with_selected_time'])
N('q_extract', 'ai_write', S, 'Extract everything in the message',
  code=[G + 'STEP 2: EXTRACT ALL AVAILABLE INFO'],
  note='Service, description, area, day and time. Partial inputs ("Sunday") normalise to a date-time.')
N('q_planlater', 'decision', S, '"I will send the plan later"?',
  code=[G + 'PLAN LATER RESPONSE', 'PlanUploadMixin.handle_plan_later_response'])
N('q_update', 'data', S, 'Update the appointment with what was extracted',
  code=[G + 'STEP 3: UPDATE APPOINTMENT', 'ExtractionMixin.update_appointment_with_extracted_data'])
N('q_excluded', 'decision', S, 'Excluded area?',
  code=[G + 'EXCLUDED AREA'],
  note='Only a short decline list is out of area; the service is mobile across Zimbabwe.')
N('q_excluded_send', 'send', S, 'Out-of-area decline',
  copy=[G + 'EXCLUDED AREA'])
N('q_resched', 'decision', S, 'A confirmed lead asking to reschedule?',
  code=[G + 'STEP 4: RESCHEDULE CHECK', 'RescheduleMixin.handle_reschedule_request_with_ai'])
N('q_resched_send', 'send', S, 'Reschedule reply',
  code=['RescheduleMixin._build_reschedule_confirmation', 'RescheduleMixin._build_reschedule_unavailable_reply', 'RescheduleMixin._build_reschedule_clarification'],
  copy=['RescheduleMixin._build_reschedule_confirmation', 'RescheduleMixin._build_reschedule_unavailable_reply', 'RescheduleMixin._build_reschedule_clarification'])
N('q_garage', 'decision', S, 'Garage or outbuilding scope question?',
  code=[G + 'GARAGE / OUTBUILDING'])
N('q_ready', 'decision', S, 'Ready to book? every field in, slot valid',
  code=[G + 'STEPS 5 & 6', 'BookingMixin.smart_booking_check', 'ExtractionMixin._job_is_known'])
N('q_book', 'data', S, 'Book: status confirmed, diary, calendar',
  code=['BookingMixin.book_appointment'],
  note='The only place status becomes confirmed from the chat. strip_unbacked_confirmation stops any other reply claiming a booking.')
N('q_confirm_msg', 'send', S, 'Booking confirmation (queued, deterministic)',
  code=['NotificationMixin.send_confirmation_message', 'NotificationMixin._build_confirmation_message'],
  copy=['NotificationMixin._build_confirmation_message'])
N('q_team', 'notify', S, 'Team and plumber told of the booking',
  code=['NotificationMixin.notify_team'])
N('q_name', 'send', S, 'Ask the name for the booking',
  code=['BookingMixin._name_ask_after_booking'], copy=['catalog:NAME_ASK_AFTER_BOOKING'])
N('q_slot_bad', 'send', S, 'Slot not available: alternatives',
  code=[G + "if reason in ('closed_day', 'saturday_closed'):", 'AvailabilityMixin.get_availability_error_message'],
  copy=[G + "if reason in ('closed_day', 'saturday_closed'):",
        'catalog:SUGGEST_ANOTHER_SLOT', 'catalog:WHICH_WORKS_BETTER', 'catalog:WHICH_TIME_WORKS',
        'catalog:TIME_UNAVAILABLE_SHORT', 'catalog:TIME_UNAVAILABLE', 'catalog:TIME_UNAVAILABLE_SUGGEST',
        'catalog:SLOT_UNAVAILABLE', 'catalog:TIME_IN_THE_PAST', 'catalog:TIME_ALREADY_BOOKED', 'catalog:TOO_FAR_AHEAD',
        'catalog:AVAILABILITY_CHECK_FAILED', 'catalog:CHOOSE_ANOTHER_DAY', 'catalog:EMERGENCY_OFFER'])
N('q_else', 'decision', S, 'Several prices / a commitment / an FB price / a flow answer / a quote / a standalone question?',
  code=[G + "_asks_figure = self._asks_price_figure(", 'ResponseMixin._is_purchase_commitment', 'ResponseMixin._is_standalone_question'])
N('q_standalone', 'send', S, 'Answer the standalone question, then a soft nudge',
  code=['ResponseMixin._answer_standalone_question', 'ResponseMixin._get_soft_booking_nudge'],
  copy=['ResponseMixin._get_soft_booking_nudge', 'ResponseMixin._facebook_price_confirm_reply'])
N('q_next', 'decision', S, 'Next question in the order',
  code=['ExtractionMixin.get_next_question_to_ask', 'ExtractionMixin.next_question_for_turn'],
  note='service_type, project_description, area, availability_date, availability_time, then name once booked. The plan path asks description, area, timeline instead. A repair is described by its service type.')
N('q_contextual', 'ai_write', S, 'generate_contextual_response',
  code=['ResponseMixin.generate_contextual_response'],
  note='Uses the semantic duplicate-question detector before any qualification question.')
N('q_retry', 'decision', S, 'Asked this question before? (retry count)',
  code=['StateMixin._get_question_retry_count'])
N('q_script', 'send', S, 'First ask: the exact approved script',
  code=['ResponseMixin._get_first_pass_question', 'bot/photo_ask.py::area_ask'],
  copy=['ResponseMixin._get_first_pass_question', 'catalog:AREA_ASK_AFTER_NO',
        'catalog:AREA_ASK_AFTER_JOB', 'catalog:AREA_ASK_PLAIN', 'catalog:TIME_ASK_TWO_SLOTS',
        'catalog:AREA_ASK_WHEREABOUTS', 'catalog:DESCRIBE_THE_JOB', 'catalog:OTHER_WORK_WHILE_THERE'],
  note='Script first, vary on retry.')
N('q_opener', 'send', S, "The owner's opener",
  code=['bot/views/plumbot/response_mixin.py::build_cold_opener'],
  copy=['catalog:OPENER_INFO', 'catalog:OPENER_HELLO', 'catalog:OPENER_INFO_SN', 'catalog:OPENER_HELLO_SN'],
  note='One line: what needs doing, one room or a few. {room} comes from the ad the lead clicked.')
N('q_avail', 'send', S, 'Availability ask: two real slots',
  code=['ResponseMixin._availability_ask', 'ResponseMixin._scripted_availability_ask', 'bot/availability_ask.py::compose_availability_ask'],
  copy=['ResponseMixin._scripted_availability_ask', 'bot/availability_ask.py::compose_availability_ask'],
  note='A day AND a time in one message, slots picked by code (visit_slots), never the model. DeepSeek writes it only when the diary is an awkward shape, fenced.')
N('q_retry_send', 'ai_write', S, 'Retry: rephrase as two choices, then shorter',
  code=['ResponseMixin._generate_retry_response', 'ResponseMixin._hardcoded_retry_fallback'],
  copy=['ResponseMixin._generate_retry_response', 'ResponseMixin._hardcoded_retry_fallback'])
N('q_complete', 'end', S, 'Complete: no reply')
N('q_fallback', 'send', S, "Didn't catch that",
  code=[G + "if not reply or not str(reply).strip():"], copy=[G + "if not reply or not str(reply).strip():", 'catalog:DROPPED_MESSAGE'])

_Q_CHAIN = ['q_emailcap', 'q_delay', 'q_exit', 'q_direct', 'q_budget', 'q_materials', 'q_bareack',
            'q_oos', 'q_size', 'q_catalogue', 'q_pricing', 'q_plan', 'q_planfar', 'q_confirmed', 'q_alt']
E('q_start', _Q_CHAIN[0])
for _a, _b in zip(_Q_CHAIN, _Q_CHAIN[1:]):
    E(_a, _b, 'no', 'no')
E('q_alt', 'q_extract', 'no', 'no')
E('q_alt', 'q_book', 'picked one')
E('q_oos', 'd_oos', 'yes', 'handoff')
E('q_plan', 'pv_plan_req', 'plan path', 'handoff')
E('q_extract', 'q_planlater')
E('q_planlater', 'q_update', 'no', 'no')
E('q_update', 'q_excluded')
E('q_excluded', 'q_excluded_send', 'yes')
E('q_excluded', 'q_resched', 'no', 'no')
E('q_resched', 'q_resched_send', 'yes')
E('q_resched', 'q_garage', 'no', 'no')
E('q_garage', 'q_ready', 'no', 'no')
E('q_ready', 'q_book', 'yes')
E('q_book', 'q_confirm_msg')
E('q_book', 'q_team')
E('q_book', 'q_name')
E('q_ready', 'q_slot_bad', 'slot not free')
E('q_ready', 'q_else', 'not ready', 'no')
E('q_else', 'q_standalone', 'standalone question')
E('q_else', 'pr_multi_send', 'several prices', 'handoff')
E('q_else', 'pr_quote_send', 'a quote', 'handoff')
E('q_else', 'q_contextual', 'anything else', 'no')
E('q_contextual', 'q_next')
E('q_next', 'q_retry', 'a field is missing')
E('q_next', 'q_complete', 'complete')
E('q_retry', 'q_opener', 'first ask, service type')
E('q_retry', 'q_avail', 'first ask, date')
E('q_retry', 'q_script', 'first ask, other')
E('q_retry', 'q_retry_send', 'asked before')
E('q_contextual', 'q_fallback', 'empty', 'no')

# ═══ Outbound chain ═════════════════════════════════════════════════════════
S = 'outbound'
N('ob_chain', 'trigger', S, 'finalise_outbound(reply)',
  code=[W + 'finalise_outbound', R + 'HANDLER D'],
  note='Every reply goes through this chain, in this order. A new send path that skips it skips every stripper at once.')
N('ob_repeat', 'decision', S, 'A scripted reply that repeats a recent message?',
  code=['bot/utils.py::repeats_recent_reply'],
  note='A repeat is read in context by the model even when the caller passed check=False: a scripted message is never sent again just because the flow is in a loop.')
N('ob_reader', 'ai_write', S, 'The model reads it back (verify_and_refine)',
  code=['bot/response_check.py::verify_and_refine', 'bot/response_check.py::_fences_hold', 'bot/response_check.py::_priced_reply_kept'],
  note='A reader, not a guard: it may only correct. No new figure, promise word or language, no growth past 1.6x; a priced draft keeps every figure and its close. Fails open.')
N('ob_memory', 'step', S, 'Memory check: drop questions already answered',
  code=['bot/views/plumbot/response_mixin.py::strip_known_questions'])
N('ob_unbacked', 'gate', S, 'Strip any booking claim the row does not back',
  code=['bot/views/plumbot/response_mixin.py::strip_unbacked_confirmation'],
  note='We never tell a customer they have an appointment the row does not have. A question is never a claim. Inert on a confirmed lead.')
N('ob_budget', 'step', S, 'Blunt budget question becomes the tie-down',
  code=['bot/views/plumbot/response_mixin.py::soften_budget_question'])
N('ob_fee', 'step', S, 'Fee tenant: free-visit wording replaced',
  code=['bot/views/plumbot/response_mixin.py::strip_free_visit_claims'])
N('ob_free_once', 'step', S, 'The free visit is said ONCE',
  code=['bot/views/plumbot/response_mixin.py::strip_repeat_free_visit'])
N('ob_note', 'step', S, 'Visit price stated once, with the availability ask',
  code=['bot/views/plumbot/response_mixin.py::ensure_visit_price_note'],
  copy=['TenantConfig.visit_price_note'])
N('ob_photo', 'step', S, 'Photo or plan ask before the area question (once)',
  code=['bot/photo_ask.py::add_photo_ask', 'bot/photo_ask.py::photo_line'],
  copy=['catalog:PHOTO_ASK_EXISTING', 'catalog:PHOTO_ASK_NEW_SPOT', 'catalog:PHOTO_ASK_NEW_BUILD', 'catalog:PHOTO_ASK_UNCLEAR'])
N('ob_we', 'step', S, 'Speak as WE, never "the plumber"',
  code=['bot/utils.py::speak_as_we'])
N('ob_dash', 'step', S, 'No dash punctuation',
  code=['bot/utils.py::strip_dashes'])
N('ob_one_q', 'step', S, 'One question per message',
  code=['bot/utils.py::enforce_single_question'])
N('ob_log', 'data', S, 'The transcript records what was SENT',
  code=['Appointment.replace_draft_assistant_turns'],
  note='The draft run is dropped and the final text logged; a turn with a WAMID is never dropped.')
N('ob_delay', 'wait', S, 'Human reply delay (1 to 5 minutes)',
  code=[W + 'get_random_delay'])
N('ob_cancel', 'decision', S, 'A newer message arrived during the wait?',
  code=[W + 'delayed_response'],
  note='_pending_send_events: a newer inbound cancels the pending send and the batch re-runs with the latest context.')
N('ob_send', 'send', S, 'Sent on WhatsApp; the WAMID is stamped',
  code=[W + 'delayed_response', 'Appointment.attach_message_id'],
  note='The stamped WAMID is what makes a later quoted reply resolvable. The split marker sends an ack and a question as two messages.')
N('ob_direct', 'step', S, 'Direct senders use _finalised_for_send',
  code=[W + '_finalised_for_send'],
  note='For paths that write to the wire themselves (the photo path). Returns empty when the chain leaves nothing to say.')

_OB = ['ob_chain', 'ob_repeat', 'ob_reader', 'ob_memory', 'ob_unbacked', 'ob_budget', 'ob_fee',
       'ob_free_once', 'ob_note', 'ob_photo', 'ob_we', 'ob_dash', 'ob_one_q', 'ob_log', 'ob_delay', 'ob_cancel']
for _a, _b in zip(_OB, _OB[1:]):
    E(_a, _b)
E('ob_cancel', 'ob_send', 'no')
E('ob_cancel', 'ro_start', 'yes: re-run the batch', 'loop')
E('ob_direct', 'ob_chain')
E('q_contextual', 'ob_chain', 'reply ready', 'handoff')
E('i_gallery_send', 'ob_direct', 'photo lead-in and follow-up', 'handoff')

# ═══ Follow-ups and crons ═══════════════════════════════════════════════════
S = 'followups'
N('f_cron', 'trigger', S, 'Follow_Ups cron (every 7 min): send_followups',
  code=[F + 'handle', 'bot/cron_health.py::beat'],
  note='Railway cron via start.sh and PLUMBOT_CRON. The heartbeat is recorded first so a dead cron is reported, not discovered.')
N('f_sched', 'send', S, 'Staff-scheduled follow-ups that are due',
  code=['bot/management/commands/send_scheduled_followups.py::dispatch_due_scheduled_followups'],
  note='Run whatever the hour: staff chose these exact times.')
N('f_near', 'notify', S, 'Near-date lead: plumber call email that morning',
  code=['bot/near_date_call.py::run_tick', 'bot/near_date_call.py::build_brief'])
N('f_window', 'decision', S, 'Inside the contact window (08:03 to 20:33)?',
  code=[F + '_in_contact_window'])
N('f_out', 'end', S, 'Outside hours: stop')
N('f_ghost', 'loop', S, 'Nudge delay-flow ghosts (4 per step)',
  code=[F + '_nudge_delay_flow_ghosts'], copy=[F + '_DELAY_NUDGE_MESSAGES'])
N('f_parked', 'loop', S, 'Nudge parked leads',
  code=[F + '_nudge_parked_leads'], copy=[F + '_nudge_parked_leads'])
N('f_react', 'loop', S, 'Delayed leads: check back on the agreed date',
  code=[F + '_process_delayed_reactivations'], copy=[F + '_process_delayed_reactivations'],
  note='At the time the lead named ("Monday evening" is 18:00). On WhatsApp when inside the free window, else email in the email windows.')
N('f_ladder', 'loop', S, 'Job-date ladder: -7 and -3 touches, -2 plumber call',
  code=[F + '_process_job_date_ladder', 'bot/job_date_ladder.py::touch_message', 'bot/job_date_ladder.py::touch_email', 'bot/job_date_ladder.py::send_call_brief'],
  copy=['bot/job_date_ladder.py::touch_message', 'catalog:LADDER_EMAIL_FIRST', 'catalog:LADDER_EMAIL_SECOND', 'catalog:LADDER_GET_QUOTE_BUTTON'])
N('f_elig', 'gate', S, 'Eligible? not paused, stopped, synthetic; 4-touch cap; 4h floor',
  code=[F + '_get_eligible_leads', F + '_is_ready_for_followup', 'bot/management/commands/send_followups.py::touches_since_last_reply', F + '_exclude_suppressed_states'],
  note='FOUR is a ceiling per silence and FOUR HOURS a floor between touches. [FOLLOWUPS_OFF] and [STOP_REQUESTED] stop every proactive path. Synthetic-key leads never get proactive WhatsApp.')
N('f_handoff_q', 'decision', S, 'A handoff lead? (delay signal, or all three fields)',
  code=['bot/management/commands/send_followups.py::handoff_eligible', 'bot/management/commands/send_followups.py::handoff_sent_since_last_reply'],
  note='TWO follow-ups, not four: the second is the plumber handoff, and it is the LAST text until the lead replies.')
N('f_handoff', 'send', S, 'The plumber handoff: link and number at the bottom',
  code=[F + '_handoff_touch', 'bot/plumber_link.py::handoff_message'],
  copy=['catalog:HANDOFF_LEAVE_IT_HERE', 'catalog:HANDOFF_WHO_HANDLES_QUOTES', 'catalog:HANDOFF_QUOTES_HERE',
        'catalog:HANDOFF_NUMBER_OF', 'catalog:HANDOFF_NUMBER', 'catalog:WHENEVER_YOURE_READY',
        'catalog:HANDOFF_NUMBER_OF_SN', 'catalog:HANDOFF_NUMBER_SN'])
N('f_ai', 'ai_write', S, 'Contextual follow-up written by DeepSeek',
  code=[F + '_ai_message'], copy=[F + '_ai_message'])
N('f_template', 'send', S, "Fallback: the owner's April 2026 scripts",
  code=[F + '_template_message'], copy=[F + '_template_message'],
  note="The owner's own copy. Never reworded unasked (pinned by the 'owner script' cases in TEST 0).")
N('f_email_cron', 'trigger', S, 'Email_Follow_Ups cron (every 5 min)',
  code=['bot/management/commands/send_scheduled_followups.py::Command.handle'],
  note='Also runs the platform billing reminders and the hourly unanswered sweep.')
N('f_sweep', 'loop', S, 'Unanswered sweep: reply to any message left unanswered',
  code=['bot/unanswered_sweep.py::answer_unanswered', 'bot/unanswered_sweep.py::unanswered_leads'],
  note='A reply dies with the old server during a deploy; hourly, messages 15 min to 23 h old are run through the router as one turn.')
N('f_inbound_email', 'step', S, 'Answer customer email replies (IMAP)',
  code=['bot/management/commands/process_inbound_emails.py::Command.handle'])
N('f_email_reply', 'ai_write', S, 'Email reply written by Plumbot',
  code=['bot/management/commands/process_inbound_emails.py::_generate_plumbot_email_reply'],
  copy=['bot/management/commands/process_inbound_emails.py::_build_acknowledgement_reply'])
N('f_jobrem', 'send', S, 'Job reminders',
  code=['bot/management/commands/send_job_reminders.py::Command.handle', 'bot/management/commands/send_job_reminders.py::Command.send_job_reminder'],
  copy=['bot/management/commands/send_job_reminders.py::Command.send_job_reminder'])
N('f_summary', 'notify', S, '24h unconfirmed-lead summaries',
  code=['bot/management/commands/summarize_unconfirmed_leads.py::Command.handle'])
N('f_rem', 'trigger', S, 'Reminders cron (every 5 min): send_reminders',
  code=['bot/management/commands/send_reminders.py::Command.handle'])
N('f_rem_send', 'send', S, 'Customer reminders: booking, morning, 2 hours, 30 minutes',
  code=['bot/management/commands/send_reminders.py::_deliver_customer_reminder'],
  copy=['bot/management/commands/send_reminders.py::_msg_2days', 'bot/management/commands/send_reminders.py::_msg_1day',
        'bot/management/commands/send_reminders.py::_msg_morning', 'bot/management/commands/send_reminders.py::_msg_2hours'])
N('f_rem_plumber', 'notify', S, 'Plumber reminders (morning, next day)',
  code=['bot/management/commands/send_reminders.py::_msg_plumber_morning', 'bot/management/commands/send_reminders.py::_msg_plumber_next_day', 'bot/management/commands/send_reminders.py::_msg_plumber_2hours'])

E('f_cron', 'f_sched')
E('f_sched', 'f_near')
E('f_near', 'f_window')
E('f_window', 'f_out', 'no', 'no')
E('f_window', 'f_ghost', 'yes')
E('f_ghost', 'f_parked')
E('f_parked', 'f_react')
E('f_react', 'f_ladder')
E('f_ladder', 'f_elig')
E('f_elig', 'f_handoff_q', 'eligible')
E('f_handoff_q', 'f_handoff', 'yes, 2nd touch')
E('f_handoff_q', 'f_ai', 'no / 1st touch', 'no')
E('f_ai', 'f_template', 'failed or fenced out', 'no')
E('f_email_cron', 'f_sweep')
E('f_sweep', 'ro_start', 'as one batched turn', 'handoff')
E('f_email_cron', 'f_inbound_email')
E('f_inbound_email', 'f_email_reply')
E('f_email_cron', 'f_jobrem')
E('f_email_cron', 'f_summary')
E('f_email_cron', 'pv_tick', 'post-visit tick', 'handoff')
E('f_email_cron', 'pv_plan_tick', 'plan tick', 'handoff')
E('f_email_cron', 'pl_billrem', 'billing reminders', 'handoff')
E('f_rem', 'f_rem_send')
E('f_rem', 'f_rem_plumber')

# ═══ Visit, quote and plan path ═════════════════════════════════════════════
S = 'postvisit'
N('pv_tick', 'trigger', S, 'send_post_visit_followups (Email_Follow_Ups)',
  code=['bot/management/commands/send_post_visit_followups.py::Command.handle', 'bot/post_visit.py::run_post_visit_tick'])
N('pv_due', 'decision', S, 'A visit that ended and needs its report?',
  code=['bot/post_visit.py::due_visits', 'bot/post_visit.py::is_due_for_report', 'bot/post_visit.py::too_stale_to_open'],
  note='The backlog guard (POST_VISIT_BACKLOG_DAYS) leaves a stale, untouched visit alone.')
N('pv_report', 'email', S, 'Plumber emailed the debrief form (35 min after)',
  code=['bot/post_visit.py::_tick_open_report', 'bot/post_visit.py::form_url'])
N('pv_form', 'staff', S, 'Plumber fills the site-visit form',
  code=['bot/views/post_visit.py::site_visit_form', 'bot/post_visit.py::apply_submission'],
  note='The outcome radio is the gate; job notes carry into the quote screen.')
N('pv_quote', 'staff', S, 'Build the quote (editor, templates)',
  code=['CreateQuotationView', 'EditQuotationView'])
N('pv_quote_send', 'email', S, 'Quote sent: email PDF or WhatsApp handoff',
  code=['bot/views/quotations.py::send_quotation', 'bot/views/quotations.py::quotation_whatsapp_handoff', 'bot/views/post_visit.py::send_quotation_email', 'bot/views/quotations.py::mark_quotation_sent'])
N('pv_seq', 'loop', S, 'Quote follow-up sequence to the lead',
  code=['bot/post_visit.py::_tick_sequence', 'bot/post_visit.py::_send_ask', 'bot/post_visit.py::_send_confirmation', 'bot/post_visit.py::start_quote_followups'],
  copy=['bot/post_visit.py::_send_ask', 'bot/post_visit.py::_send_confirmation'],
  note='A quote being raised arms the same sequence. lead_is_suppressed decides whether we may message at all.')
N('pv_cold', 'data', S, 'No answer: marked cold, handed back',
  code=['bot/post_visit.py::_mark_cold'])
N('pv_plan_req', 'data', S, 'PlanQuoteRequest opened',
  code=['bot/plan_quote.py::ensure_request'],
  note='A lead who sends a real plan needs no site visit.')
N('pv_plan_tick', 'trigger', S, 'send_plan_quote_followups (Email_Follow_Ups)',
  code=['bot/management/commands/send_plan_quote_followups.py::Command.handle', 'bot/plan_quote.py::run_plan_quote_tick'])
N('pv_plan_email', 'email', S, 'Plumber emailed the job and the plan',
  code=['bot/plan_quote.py::_tick_one'],
  note='An hour after the plan if not booked, or once description, area and timeline are in.')
N('pv_plan_form', 'staff', S, 'Plumber taps "I have sent the quote"',
  code=['bot/views/plan_quote.py::plan_quote_form', 'bot/plan_quote.py::apply_plumber_form'])
N('pv_plan_check', 'send', S, 'An hour later: did the quote come through?',
  code=['bot/plan_quote.py::_send_quote_check'], copy=['catalog:PLAN_QUOTE_CHECK'])
N('pv_proposal', 'email', S, 'Future visit penciled in: confirm at -7 and -4 days',
  code=['bot/visit_proposal.py::run_visit_proposal_tick', 'bot/visit_proposal.py::_send_lead_checkin', 'bot/visit_proposal.py::_send_plumber_checkin'],
  copy=['bot/visit_proposal.py::_send_lead_checkin', 'bot/visit_proposal.py::_send_plumber_checkin'],
  note='send_visit_checkins is not in PLUMBOT_CRON yet.')
N('pv_proposal_ans', 'decision', S, 'Lead taps yes or no',
  code=['bot/views/visit_proposal.py::visit_proposal_answer', 'bot/visit_proposal.py::apply_lead_answer'])
N('pv_book', 'data', S, 'Yes: the visit is booked',
  code=['bot/visit_proposal.py::_book_the_visit'])
N('pv_phone', 'staff', S, 'Phone quote: plumber fills a one-tap form',
  code=['bot/views/phone_quote.py::phone_quote_start', 'bot/views/phone_quote.py::phone_quote_form'])

E('pv_tick', 'pv_due')
E('pv_due', 'pv_report', 'yes')
E('pv_report', 'pv_form', 'plumber opens the link')
E('pv_form', 'pv_quote', 'quote needed')
E('pv_quote', 'pv_quote_send')
E('pv_quote_send', 'pv_seq', 'arms the sequence')
E('pv_tick', 'pv_seq')
E('pv_seq', 'pv_cold', 'no reply', 'no')
E('pv_plan_tick', 'pv_plan_email')
E('pv_plan_req', 'pv_plan_email')
E('pv_plan_email', 'pv_plan_form')
E('pv_plan_form', 'pv_plan_check')
E('pv_proposal', 'pv_proposal_ans')
E('pv_proposal_ans', 'pv_book', 'yes')
E('in_plan', 'pv_plan_req', 'plan path', 'handoff')
E('pv_phone', 'pv_quote', 'quote needed')

# ═══ Dashboard operations ═══════════════════════════════════════════════════
S = 'ops'
N('op_dash', 'staff', S, 'Dashboard: diary and the day at a glance',
  code=['DashboardView'])
N('op_inbox', 'staff', S, 'Lead inbox (date window, search)',
  code=['ConversationsView', 'bot/lead_search.py::filter_leads'])
N('op_detail', 'staff', S, 'Lead page: chat, details, quotes, email',
  code=['ConversationDetailView', 'bot/views/appointments.py::conversation_live'])
N('op_pause', 'staff', S, 'Pause or resume the bot',
  code=['bot/views/followups.py::pause_chatbot', 'bot/views/followups.py::resume_chatbot'],
  note='Sets chatbot_paused, which the router reads first.')
N('op_intercept', 'staff', S, 'Intercept the bot reply in flight',
  code=['bot/views/appointments.py::intercept_bot_reply', W + 'request_intercept'])
N('op_manual', 'staff', S, 'Send a manual WhatsApp or follow-up',
  code=['bot/views/followups.py::send_followup'],
  note='Logged as [MANUAL FOLLOW-UP]; not counted by the 4-touch cap.')
N('op_pdf', 'staff', S, 'Send the portfolio PDF, a PDF or an image',
  code=['bot/views/followups.py::send_portfolio_pdf_to_lead', 'bot/views/followups.py::send_pdf_to_lead', 'bot/views/followups.py::send_image_to_lead'])
N('op_handoff', 'staff', S, 'Send the plumber handoff',
  code=['bot/views/followups.py::send_plumber_handoff_to_lead'])
N('op_edit', 'staff', S, 'Edit details, date and time',
  code=['bot/views/appointments.py::update_appointment'])
N('op_confirm', 'staff', S, 'Confirm, cancel, unbook or complete the visit',
  code=['bot/views/appointments.py::confirm_appointment', 'bot/views/appointments.py::cancel_appointment', 'bot/views/appointments.py::unbook_appointment', 'bot/views/appointments.py::complete_lead_appointment'],
  note='Mutations are POST only and return you to the screen you pressed them on.')
N('op_schedule', 'staff', S, 'Schedule a follow-up or reminder',
  code=['bot/views/followups.py::schedule_followup', 'bot/views/followups.py::schedule_reminder'])
N('op_inactive', 'staff', S, 'Mark inactive / stop follow-ups',
  code=['bot/views/followups.py::mark_lead_inactive'])
N('op_run_check', 'staff', S, 'Run the follow-up check now',
  code=['bot/views/followups.py::manual_followup_check'],
  note='Runs send_followups inside the web service; it does not make a dead cron look alive.')
N('op_jobs', 'staff', S, 'Schedule a job, update its status',
  code=['bot/views/jobs.py::schedule_job', 'bot/views/jobs.py::update_job_status'])
N('op_calendar', 'staff', S, 'Calendar',
  code=['CalendarView'])
N('op_gallery', 'staff', S, 'Gallery: upload past-work photos',
  code=['bot/views/gallery.py::gallery_upload'],
  note='What the proof step and the gallery sends draw from.')
N('op_offer', 'staff', S, 'Offer and prices',
  code=['bot/views/offer.py::offer_save'])
N('op_settings', 'staff', S, 'Settings: calendar, AI, email health',
  code=['bot/views/settings_views.py::settings_view', 'bot/views/settings_views.py::email_settings_view'])

E('op_inbox', 'op_detail')
E('op_dash', 'op_detail')
E('op_detail', 'op_pause')
E('op_detail', 'op_intercept')
E('op_detail', 'op_manual')
E('op_detail', 'op_pdf')
E('op_detail', 'op_handoff')
E('op_detail', 'op_edit')
E('op_detail', 'op_confirm')
E('op_detail', 'op_schedule')
E('op_detail', 'op_inactive')
E('op_detail', 'op_jobs')
E('op_pause', 'ro_paused', 'chatbot_paused', 'handoff')
E('op_intercept', 'ob_cancel', 'cancels the pending send', 'handoff')
E('op_schedule', 'f_sched', 'dispatched when due', 'handoff')
E('op_run_check', 'f_cron', 'same command', 'handoff')
E('op_confirm', 'pv_due', 'visit ends', 'handoff')
E('op_detail', 'pv_quote', 'Quotes tab', 'handoff')
E('op_detail', 'pv_phone', 'phone quote', 'handoff')
E('op_gallery', 'ro_proof', 'photos used', 'handoff')

# ═══ Platform and billing ═══════════════════════════════════════════════════
S = 'platform'
N('pl_console', 'staff', S, 'Platform console: tenants',
  code=['bot/views/platform.py::platform_console', 'bot/views/platform.py::platform_create_tenant'])
N('pl_config', 'staff', S, 'Tenant config: prices, hours, plumber, fee',
  code=['bot/views/platform.py::platform_tenant_config_edit', 'bot/tenant_config.py::get_config'],
  note='No Homebase value reaches another tenant: every figure, name and place resolves through the lead\'s own tenant; absent means omit.')
N('pl_magnet', 'staff', S, 'Regenerate the lead magnet PDF',
  code=['bot/views/platform.py::platform_tenant_lead_magnet_regenerate', 'bot/lead_magnet.py::build_lead_magnet_pdf'])
N('pl_intake', 'staff', S, 'Client intake form',
  code=['bot/views/platform.py::intake_form', 'bot/views/platform.py::platform_review_intake'])
N('pl_invoice', 'staff', S, 'Invoices: create, email, record payments',
  code=['bot/views/billing.py::billing_invoice_new', 'bot/views/billing.py::billing_invoice_email', 'bot/views/billing.py::billing_payment_add'])
N('pl_billrem', 'email', S, 'Billing reminders to tenants',
  code=['bot/billing_reminders.py::run_billing_reminders'])

E('pl_console', 'pl_config')
E('pl_console', 'pl_magnet')
E('pl_console', 'pl_intake')
E('pl_console', 'pl_invoice')
E('pl_invoice', 'pl_billrem', 'reminders when due')
E('pl_magnet', 'd_wa_portfolio', 'the portfolio PDF', 'handoff')
E('pl_config', 'pr_single_send', 'price rows', 'handoff')


# ═══ Example messages for the ladder ════════════════════════════════════════
# One short, typical customer message per rung, shown beside it so a reader
# sees what trips it without reading the resolver. Illustrative only: the
# resolvers and the TEST 0 cases are the truth about what matches.
_EXAMPLES = {
    'ro_postack': 'Thanks!  (after booking)',
    'ro_stop': 'please stop messaging me',
    'ro_email': 'my email is jane@example.com',
    'ro_contact': '[contact card] call my husband about it',
    'ro_area': 'Bluffhill',
    'p_sc': 'also a toilet',
    'p_vc': "no that's all",
    'p_detail': 'no  (to "can you tell me a bit more?")',
    'p_lock': 'no  (to "Want me to lock in a time?")',
    'p_slot': 'neither works',
    'p_nb': "no, it's a renovation",
    'p_pivot': 'only next month',
    'p_list': 'how much for everything on the list?',
    'p_faq': 'where are you located?',
    'i_here': 'just send it here',
    'i_link': 'what is this link? is it a scam?',
    'i_hes': "can't you just quote?",
    'i_choice': 'online first please',
    'i_multi': 'where are you based and how much?',
    'i_gallery': 'can I see your work?',
    'i_piece': 'the black bathtub one',
    'i_menu': 'what can you show me?',
    'i_cat': 'send your products and prices',
    'i_photo': 'can I have a pic',
    'i_budget': 'not really  (to the invest tie-down)',
    'd_oos': "I'm travelling, will get back to you",
    'pr_1c': 'this one how much?  (replying to our photo)',
    'pr_1d': 'how much for all of it?',
    'pr_skip': 'I want my whole bathroom redone',
    'pr_2b': 'May I get a quote for a four bedroomed house',
    'pr_3': "that's expensive",
    'pr_3b': 'how much did you say it was?',
    'pr_3c': 'I want to build a new house',
    'q_start': 'Tuesday 10am',
    'q_emailcap': 'jane@example.com  (after we asked)',
    'q_delay': 'maybe next month',
    'q_exit': "I'll let you know",
    'q_direct': 'do you do geysers?',
    'q_budget': 'no  (to the invest tie-down)',
    'q_materials': 'can you send a materials list?',
    'q_bareack': 'ok',
    'q_size': 'how big are your tubs?',
    'q_catalogue': 'what products do you have?',
    'q_pricing': 'how much is a vanity?',
    'q_plan': '[a PDF plan]',
    'q_confirmed': 'what should I prepare?  (booked lead)',
    'q_alt': 'the second one',
    'q_extract': 'Borrowdale, Friday morning',
    'q_planlater': "I'll send the plan later",
    'q_excluded': "I'm in Bulawayo",
    'q_resched': 'can we move it to Thursday?',
    'q_garage': 'is the garage included?',
    'q_ready': 'Friday 9am works',
}
for _n in NODES:
    _n['example'] = _EXAMPLES.get(_n['id'], '')


# ═══ The lead's journey ═════════════════════════════════════════════════════
# The level the business decides at: the stages a lead moves through. `lane`
# is 'main' for the path to a sale and 'side' for where a lead goes when they
# step off it. `marker` says how the system knows a lead is there (the field
# or tag to look at on the lead page). `nodes` are the bot's moves at that
# stage, most important first; every id must exist (bot/test_flow_map.py).
JOURNEY = [
    {'id': 'j_first', 'lane': 'main', 'title': 'First message',
     'blurb': 'A new chat opens, usually from a Facebook ad.',
     'marker': 'A new lead row; the CTWA ad referral when it came from an ad.',
     'nodes': ['in_webhook', 'in_wamid', 'in_log', 'q_opener', 'in_audio', 'ro_contact', 'in_media_ack'],
     'next': [('j_job', 'they say what needs doing')]},
    {'id': 'j_job', 'lane': 'main', 'title': 'Describing the job',
     'blurb': 'We learn what needs doing and how many rooms.',
     'marker': 'Service type and project description empty or being filled.',
     'nodes': ['q_opener', 'p_sc', 'p_sc_more', 'p_sc_adv', 'p_detail', 'p_nb', 'pr_3c', 'p_faq', 'q_extract', 'q_script'],
     'next': [('j_area', 'job known')]},
    {'id': 'j_area', 'lane': 'main', 'title': 'Area and proof',
     'blurb': 'We ask where they are and show two or three jobs like theirs.',
     'marker': 'Customer area empty; previous-work photos not yet sent.',
     'nodes': ['ro_area', 'ro_area_excl', 'ob_photo', 'ro_proof_gate', 'ro_proof', 'i_gallery_send', 'q_script'],
     'next': [('j_price', 'asks a price'), ('j_offer', 'area known'), ('j_stop', 'out of area')]},
    {'id': 'j_price', 'lane': 'main', 'title': 'Talking price',
     'blurb': 'They ask what it costs. Can happen at any stage.',
     'marker': 'A price question; sent_pricing_intents; [PRICE_CHOICE_PENDING].',
     'nodes': ['pr_1d', 'pr_guide', 'i_choice', 'i_online', 'pr_1c', 'pr_quote_send', 'pr_single_send',
               'pr_multi_send', 'i_budget', 'i_budget_ask', 'i_budget_opts', 'i_hes', 'i_hes_send', 'p_list_send'],
     'next': [('j_offer', 'a quick look'), ('j_online', 'quote online')]},
    {'id': 'j_offer', 'lane': 'main', 'title': 'Visit offered',
     'blurb': 'We offer two real slots for a quick look at the space.',
     'marker': 'Next question is the date or time; the availability ask sent.',
     'nodes': ['q_avail', 'ob_note', 'p_slot', 'p_lock', 'p_pivot', 'ro_move_send', 'q_slot_bad', 'q_alt', 'q_ready'],
     'next': [('j_booked', 'picks a slot'), ('j_delay', 'not now'), ('j_online', 'hesitates')]},
    {'id': 'j_booked', 'lane': 'main', 'title': 'Booked',
     'blurb': 'The visit is in the diary; we ask their name.',
     'marker': 'Status confirmed with a scheduled date and time.',
     'nodes': ['q_book', 'q_confirm_msg', 'q_team', 'q_name', 'q_resched', 'q_confirmed', 'f_rem', 'f_rem_send', 'op_confirm'],
     'next': [('j_visited', 'visit happens')]},
    {'id': 'j_visited', 'lane': 'main', 'title': 'Visit done',
     'blurb': 'The plumber has been; the debrief form goes out.',
     'marker': 'The visit time has passed; a site-visit report exists.',
     'nodes': ['pv_tick', 'pv_due', 'pv_report', 'pv_form'],
     'next': [('j_quoted', 'quote raised')]},
    {'id': 'j_quoted', 'lane': 'main', 'title': 'Quote sent',
     'blurb': 'The quote is out and the follow-up sequence runs.',
     'marker': 'A quotation marked sent; the quote sequence started.',
     'nodes': ['pv_quote', 'pv_quote_send', 'pv_seq', 'pv_phone'],
     'next': [('j_won', 'accepted'), ('j_cold', 'no answer')]},
    {'id': 'j_won', 'lane': 'main', 'title': 'Job won',
     'blurb': 'The job is scheduled and reminded.',
     'marker': 'A job scheduled against the lead.',
     'nodes': ['op_jobs', 'f_jobrem'],
     'next': []},
    {'id': 'j_delay', 'lane': 'side', 'title': 'Not ready yet',
     'blurb': 'They defer. We get a timeframe, then an email for the portfolio.',
     'marker': '[DELAY_SIGNAL]; [OOS_PENDING] delay_*; a check-back date.',
     'nodes': ['d_oos', 'd_sub', 'd_brush', 'd_compare', 'd_nudge', 'd_tf', 'd_near1', 'd_near2', 'd_ladder',
               'd_email_ask', 'd_email', 'd_wa_portfolio', 'd_call_ask', 'f_ghost', 'f_react', 'f_ladder', 'f_near'],
     'next': [('j_offer', 'the date comes'), ('j_online', 'wants a quote first')]},
    {'id': 'j_online', 'lane': 'side', 'title': 'Online quote',
     'blurb': 'Handed to the plumber on WhatsApp to quote from photos or a plan.',
     'marker': 'The plumber link sent ([HESITATION_ONLINE_OFFERED] or the link tag).',
     'nodes': ['i_online', 'i_hes_send', 'i_link', 'i_link_send', 'f_handoff', 'op_handoff'],
     'next': [('j_quoted', 'plumber quotes')]},
    {'id': 'j_plan', 'lane': 'side', 'title': 'Sent a plan',
     'blurb': 'A drawing arrives, so the quote comes off the plan, no visit.',
     'marker': 'plan_status set; a PlanQuoteRequest open.',
     'nodes': ['in_is_plan', 'in_plan', 'q_plan', 'q_planlater', 'pv_plan_req', 'pv_plan_tick', 'pv_plan_email',
               'pv_plan_form', 'pv_plan_check'],
     'next': [('j_quoted', 'quote sent')]},
    {'id': 'j_quiet', 'lane': 'side', 'title': 'Gone quiet',
     'blurb': 'No reply. Up to four touches, or two with the plumber handoff.',
     'marker': 'The last turn is ours; follow-up count and touches since their reply.',
     'nodes': ['f_cron', 'f_elig', 'f_handoff_q', 'f_ai', 'f_template', 'f_handoff', 'f_parked', 'f_sweep'],
     'next': [('j_job', 'they reply')]},
    {'id': 'j_stop', 'lane': 'side', 'title': 'Stopped or out of area',
     'blurb': 'They asked us to stop, or we cannot travel there.',
     'marker': '[STOP_REQUESTED]; an excluded city; [FOLLOWUPS_OFF].',
     'nodes': ['ro_stop', 'ro_stop_send', 'q_excluded', 'q_excluded_send', 'd_oos_send', 'op_inactive'],
     'next': []},
    {'id': 'j_cold', 'lane': 'side', 'title': 'Cold',
     'blurb': 'The quote sequence ended without an answer.',
     'marker': 'The quote marked cold; the lead handed back to staff.',
     'nodes': ['pv_cold', 'op_inactive', 'op_manual'],
     'next': []},
]
