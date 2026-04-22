# Handoff: Plumbot Admin — Full UI Redesign

## Overview
This is a complete redesign of the Plumbot plumbing appointment management system — a Django admin app that manages WhatsApp-driven appointments, priority leads, follow-ups, job scheduling, and quotations.

The redesign modernises the UI from a heavy purple-gradient aesthetic to a clean WhatsApp-inspired design language: dark teal navigation, green accents, white card surfaces, and a responsive layout that works equally well on desktop (sidebar nav) and mobile (bottom tab bar).

---

## About the Design Files
The files in this bundle are **design references built in HTML/React** — interactive prototypes showing the intended look, layout, and behaviour. The task is to **recreate these designs inside the existing Django + Jinja2/Django template environment**, replacing the current `base.html` and page templates while keeping all existing URL routing, form actions, template tags, and backend logic intact.

A ready-to-use `base.html` replacement is included (`base_new.html`). It is a drop-in for your existing `templates/base.html` — all `{% block %}` names are preserved.

---

## Fidelity
**High-fidelity.** The prototype (`Plumbot Redesign.html`) shows final colors, typography, spacing, component styles, and interactions. Recreate as pixel-closely as possible using the design tokens listed below. The `base_new.html` file is production-ready and only needs the `{% url %}` tags corrected for your project.

---

## Design Tokens

### Colors
```css
:root {
  --color-header:      #075E54;   /* nav/header bg — dark teal */
  --color-header-dark: #054d44;   /* darker header variant */
  --color-teal:        #128C7E;   /* primary interactive */
  --color-green:       #25D366;   /* success / confirmed / accent */
  --color-green-bg:    #DCF8C6;   /* chat bubble (bot) */
  --color-green-soft:  #d1fae5;   /* confirmed status bg */
  --color-amber:       #f59e0b;
  --color-amber-bg:    #fef3c7;   /* pending status bg */
  --color-red:         #ef4444;
  --color-red-bg:      #fee2e2;   /* cancelled / hot lead bg */
  --color-blue:        #3b82f6;
  --color-blue-bg:     #dbeafe;   /* delayed status bg */
  --color-bg:          #F0F2F5;   /* page background */
  --color-surface:     #ffffff;
  --color-text:        #111827;
  --color-muted:       #6b7280;
  --color-border:      rgba(0,0,0,0.08);
}
```

### Typography
- **Font stack:** `-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif`
- Page title (TopBar): `17px / 600`
- Section headings: `13px / 700 / uppercase / letter-spacing: 0.6px`
- List item name: `15px / 600`
- List item sub: `12px / 400 / var(--color-muted)`
- Pills/badges: `11px / 700`
- Nav labels: `10px / 700 (active) / 400 (inactive)`

### Spacing
- Page padding: `16px`
- Card inner padding: `12–16px`
- List item padding: `12px 16px`
- Gap between sections: `8px` (margin-bottom on cards)
- Border radius — cards: `12px`, pills: `20px`, avatars: `50%`, time blocks: `10px`

### Shadows
- Cards: `none` (border: `1px solid #e5e7eb` instead)
- Detail panel: `none`
- Bottom nav: `border-top: 1px solid #e5e7eb`

---

## Layout System

### Desktop (≥ 768px)
```
┌──────────────┬──────────────────────┬──────────────────────┐
│  SideNav     │  List / Page Panel   │  Detail Panel        │
│  220px       │  420px (when detail  │  flex: 1             │
│  #075E54 bg  │  is open), else flex │  (appointment chat)  │
└──────────────┴──────────────────────┴──────────────────────┘
```
- SideNav always visible, 220px wide, `#075E54` background
- When an appointment is selected: left panel locks to 420px, right panel shows detail
- When nothing selected: content fills full width, right area shows empty-state prompt

### Mobile (< 768px)
```
┌─────────────────────────────┐
│  TopBar  (#075E54)          │
├─────────────────────────────┤
│  Page content (scrollable)  │
├─────────────────────────────┤
│  BottomNav  (white, 60px)   │
└─────────────────────────────┘
```
- Detail view replaces current screen (full screen), back button in header
- Bottom nav hidden when detail is open

---

## Components

### TopBar
Dark teal header bar present on every screen.
```
height: auto, padding: 12px 16px
background: #075E54
Left: optional back arrow (←, white, 20px)
Center: title (white, 17px/600) + subtitle (rgba(255,255,255,0.7), 12px)
Right: slot for action buttons
```

### SideNav (desktop)
```
width: 220px
background: #075E54
Brand block: padding 20px 16px, "Plumbot" white 18px/700, subtitle rgba(255,255,255,0.5) 12px
Nav items: padding 10px 12px, border-radius 8px
  Active:   background rgba(255,255,255,0.15), text white/600
  Inactive: text rgba(255,255,255,0.65)/400
Badge: background #25D366, white, 10px/700, border-radius 10px
```

### BottomNav (mobile)
```
height: 60px, background: white, border-top: 1px solid #e5e7eb
5 tabs: Home | Appointments | Leads | Follow-ups | More
Active indicator: 3px bar at top of tab, color #128C7E
Active label: #128C7E / 700; Inactive: #6b7280 / 400
Badge: #25D366 circle, positioned top-right of icon
```

### Avatar
Round circle, initials (first 2 words), size varies (40–50px typical).
Color assigned by `name.charCodeAt(0) % colors.length` from palette:
`['#128C7E','#075E54','#25D366','#0ea5e9','#8b5cf6','#f59e0b','#ef4444']`

### StatusPill
```
confirmed → bg: #d1fae5, color: #059669, label: "Booked"
pending   → bg: #fef3c7, color: #d97706, label: "Pending"
cancelled → bg: #fee2e2, color: #ef4444, label: "Cancelled"
delayed   → bg: #dbeafe, color: #3b82f6, label: "Delayed"
Font: 11px / 700, padding: 3px 8px, border-radius: 20px
```

### HeatPill (lead temperature)
```
very_hot  → bg: #fee2e2, color: #ef4444
hot       → bg: #fef3c7, color: #d97706
warm      → bg: #d1fae5, color: #059669
luke_warm → bg: #dbeafe, color: #3b82f6
cold      → bg: #f3f4f6, color: #6b7280
```

### ScoreDot
Conic-gradient circle (36px), score number inside (9px/700).
Color: ≥80 → red, ≥60 → amber, ≥40 → green, else muted.

### AppointmentListRow (WhatsApp chat list style)
```
padding: 12px 16px, display: flex, gap: 12px
Left:   Avatar (50px)
Center: Name (15px/600) + service·area (12px/muted) + last message preview (12px, #9ca3af)
Right:  Time (11px/muted) + heat dot (10px circle) + StatusPill
Hover:  background #f9fafb
Divider: 1px #f0f0f0, margin: 0 16px (not full-width)
```

### Chat Bubbles (Appointment Detail → Chat tab)
```
Container background: #E5DDD5 with subtle dot pattern SVG
Customer (user):  bubble left, bg #ffffff, border-radius: 0 10px 10px 10px
Bot (assistant):  bubble right, bg #DCF8C6, border-radius: 10px 0 10px 10px
Bubble padding: 8px 12px, max-width: 75%, box-shadow: 0 1px 2px rgba(0,0,0,0.1)
Sender label: 11px/700, Customer=#128C7E, Bot=#555
Content: 13px, line-height 1.45
Timestamp: 10px, #9ca3af, text-align right
```

### ScheduleSection (Dashboard collapsible)
```
Section header: full-width button, padding 12px 16px
  Left accent bar: 3px wide, color varies by section
  Title: 13px/700/uppercase/letterSpacing 0.6px
  Count badge: 11px/700, bg = accent color at 13% opacity
  Chevron: rotates 90° open / 0° collapsed
Row: same as AppointmentListRow but with time-block instead of last message
Time block: 46×46px, border-radius 10px, bg green-soft (confirmed) or amber-bg (pending)
```

---

## Screens

### 1. Dashboard (`/`)
**Purpose:** At-a-glance overview of today, tomorrow, this week, hot leads, follow-ups.

**Layout (top → bottom):**
1. TopBar — "Plumbot" + current date
2. Alert banner (conditional) — red bottom border, fire emoji, "N priority leads need attention" → links to Priority Leads
3. Stats strip — 5 tiles: Today's Appts | Today's Jobs | Hot Leads | Follow-ups | This Week
   - Each tile: number (22px/800), label (10px/muted), colored bg
4. ScheduleSection: "Today's Appointments" (teal accent)
5. ScheduleSection: "Tomorrow" (purple `#8b5cf6` accent)
6. ScheduleSection: "Later This Week" (amber accent)
7. ScheduleSection: "Jobs This Week" (red accent)
8. Pending Follow-ups section (3 rows max) → "See all" link
9. Quick Actions 2×2 grid

**Template variables needed:**
`todays_confirmed_appointments`, `tomorrows_confirmed_appointments`, `this_week_appointments`, `week_jobs`, `hot_lead_count`, `followups`, `stats`

---

### 2. Appointments List (`/appointments/`)
**Purpose:** Browse, search, filter all appointments.

**Layout:**
1. TopBar — "Appointments" + total count
2. SearchBar — white pill, inside teal header area
3. Status filter tabs — All | Booked | Pending | Cancelled | Delayed
   - Active tab: colored text + colored count badge + 2px bottom border
4. List of AppointmentListRows

**Template variables:** `appointments`, `status_counts`, `selected_status_filter`, `selected_response_age`

---

### 3. Appointment Detail (`/appointments/<pk>/`)
**Purpose:** View/edit customer info, read/send conversation, manage quotations.

**Layout:**
- Desktop: right panel (flex: 1), no back button
- Mobile: full screen, back button

**Header:** Avatar + name + phone/area + Call button + WhatsApp button
**Status bar:** StatusPill + HeatPill + ScoreDot + "Bot paused" badge if applicable
**Tabs:** Chat | Details | Quotations

**Chat tab:**
- Message list with WhatsApp-style bubbles (see Chat Bubbles spec above)
- Bottom input: white rounded textarea + teal send button (circular, 44px)
- Send button turns teal when input has text, grey when empty

**Details tab:**
- White card: Customer Details (Name, Phone, Service, Area, Scheduled, Description)
- White card: Lead Management (Follow-up status, Admin notes)
- Action buttons row: Confirm (green) | Complete (teal) | Cancel (red-bg/red-text)

**Quotations tab:**
- List of quotations or empty state with "+ Create Quotation" button

---

### 4. Priority Leads (`/priority-leads/`)
**Purpose:** View leads grouped by temperature, call/WhatsApp directly.

**Layout:**
1. TopBar — "Priority Leads" + total count
2. SearchBar
3. Heat summary chips (horizontal scroll): Very Hot | Hot | Warm | Lukewarm | Cold — each with count badge
4. Collapsible sections per heat level, left border in heat color
5. Each row: Avatar + name/service/area + inline Call + WA buttons + ScoreDot

---

### 5. Follow-ups (`/followups/`)
**Purpose:** See all pending follow-up actions.

**Layout:**
1. TopBar — "Follow-ups" + count
2. List rows: Avatar + name + note + time-ago label
   - Urgent: red time label + "Needs attention" red pill

---

## Navigation

### Desktop SideNav items (in order):
1. Dashboard → `{% url 'dashboard' %}`
2. Appointments → `{% url 'appointments_list' %}`
3. Priority Leads → `{% url 'priority_leads' %}`
4. Follow-ups → `{% url 'followup_dashboard' %}`
5. Job Appointments → `{% url 'job_appointments_list' %}`
6. Templates → `{% url 'quotation_templates_list' %}`
7. New Quotation → `{% url 'standalone_quotation' %}`

### Active state:
Pass `active_nav` context variable from each view (e.g. `'dashboard'`, `'appointments'`).
In `base.html`: `{% if active_nav == 'dashboard' %}class="active"{% endif %}`

### Mobile BottomNav tabs:
Home | Appointments | Leads | Follow-ups | More
"More" expands to a page listing: Jobs, Templates, New Quote, Settings, Profile.

---

## Interactions & Behaviour

| Trigger | Behaviour |
|---|---|
| Click appointment row | Desktop: open right panel. Mobile: navigate to detail screen |
| Click heat chip | Scroll to / expand that section |
| Collapse section header | Toggle section open/closed, rotate chevron |
| Alert banner click | Navigate to Priority Leads |
| Send message (chat) | Append bubble, clear input, POST to `send_followup` URL |
| Pause/Resume chatbot | Toggle bot-paused badge in status bar |
| Back button (mobile) | Return to list screen |

---

## CSS Architecture

Use CSS custom properties (see Design Tokens above) defined in `base.html` `<style>` block.
Remove all existing gradient backgrounds. Replace with:
- Page background: `var(--color-bg)` (`#F0F2F5`)
- Cards: `background: white; border: 1px solid #e5e7eb; border-radius: 12px`
- No `box-shadow` on cards — borders only

The included `plumbot-variables.css` file contains all tokens ready to drop in.

---

## Files in This Package

| File | Purpose |
|---|---|
| `README.md` | This document |
| `Plumbot Redesign.html` | Interactive hi-fi prototype — primary visual reference |
| `plumbot-shared.jsx` | All shared React components (Avatar, StatusPill, nav, etc.) |
| `plumbot-screens.jsx` | All screen components |
| `plumbot-data.js` | Mock data used in prototype |
| `base_new.html` | Drop-in replacement for Django `base.html` |
| `plumbot-variables.css` | CSS custom properties — import in base.html |

---

## Implementation Notes for Claude Code

1. **Start with `base_new.html`** — replace `templates/base.html` with it. Fix the `{% url %}` tags if any names differ.
2. **Add `plumbot-variables.css`** to your static files and link it in `base_new.html`.
3. **Update each template** to remove inline gradient styles and use the new CSS variable classes.
4. **Add `active_nav` context** to each view's context dictionary so the nav highlights correctly.
5. **The chat/conversation section** in `appointment_detail.html` should be restyled to use the bubble layout — customer messages left (white bg), bot messages right (DCF8C6 bg).
6. **Mobile bottom nav** is pure CSS/HTML — no JS framework needed. Use `position: fixed; bottom: 0` with `padding-bottom: 60px` on the main content area.
7. **Font Awesome icons** are already used in the current templates — keep them, just restyle the containers.
