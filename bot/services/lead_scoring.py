from bot.models import LeadStatus


def _is_completed(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


# The 4 required fields in the booking flow, in order of progression.
# timeline and property_type are stored passively if volunteered, but never
# gate progress — so they don't factor into the score.
REQUIRED_FLOW_FIELDS = [
    'project_type',      # Step 1 — which service?
    'has_plan',          # Step 2 — plan upload or site visit?
    'customer_area',     # Step 3 — which suburb?
    'scheduled_datetime', # Step 4 — confirmed appointment
]


def _count_completed_fields(lead) -> int:
    count = 0
    for field in REQUIRED_FLOW_FIELDS:
        value = getattr(lead, field, None)
        # has_plan is a boolean — treat False as completed (customer answered)
        if field == 'has_plan':
            if value is not None:
                count += 1
        elif _is_completed(value):
            count += 1
    return count


def calculate_lead_score(lead):
    """
    Pure scoring function with no side effects.
    Returns (score, classification).

    Scoring is based on the 4 required flow fields:
        project_type, has_plan, customer_area, scheduled_datetime

    A confirmed scheduled_datetime always yields VERY_HOT (100).
    """
    # Booked = immediately very hot regardless of other fields
    if getattr(lead, 'scheduled_datetime', None) is not None:
        return 100, LeadStatus.VERY_HOT

    completed = _count_completed_fields(lead)
    # 4 fields × 25 pts each = 100 max (scheduled_datetime handled above)
    score = completed * 25

    if completed == 0:
        classification = LeadStatus.COLD
    elif completed == 1:
        classification = LeadStatus.COLD
    elif completed == 2:
        classification = LeadStatus.WARM
    elif completed == 3:
        classification = LeadStatus.HOT
    else:
        # completed == 4 but no scheduled_datetime shouldn't happen,
        # but guard it anyway
        classification = LeadStatus.VERY_HOT

    return score, classification


def refresh_lead_score(lead, persist=True):
    score, classification = calculate_lead_score(lead)
    lead.lead_score = score
    lead.lead_status = classification
    # Chatbot is only paused manually — never auto-paused here
    if persist:
        lead.save(update_fields=['lead_score', 'lead_status'])
    return score, classification