from bot.models import LeadStatus


def _is_completed(value) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def calculate_lead_score(lead):
    """
    Pure scoring function with no side effects.
    Returns (score, classification).
    """
    if getattr(lead, 'scheduled_datetime', None) is not None:
        return 100, LeadStatus.VERY_HOT

    completed_fields = sum(
        [
            _is_completed(getattr(lead, 'project_type', None)),
            _is_completed(getattr(lead, 'property_type', None)),
            _is_completed(getattr(lead, 'customer_area', None)),
            _is_completed(getattr(lead, 'timeline', None)),
            _is_completed(getattr(lead, 'scheduled_datetime', None)),
        ]
    )
    score = completed_fields * 20

    if completed_fields <= 1:
        classification = LeadStatus.COLD
    elif completed_fields <= 3:
        classification = LeadStatus.WARM
    elif completed_fields == 4:
        classification = LeadStatus.HOT
    else:
        classification = LeadStatus.VERY_HOT

    return score, classification


def refresh_lead_score(lead, persist=True):
    score, classification = calculate_lead_score(lead)
    lead.lead_score = score
    lead.lead_status = classification
    if classification == LeadStatus.VERY_HOT:
        lead.chatbot_paused = True
    if persist:
        lead.save(update_fields=['lead_score', 'lead_status', 'chatbot_paused'])
    return score, classification

