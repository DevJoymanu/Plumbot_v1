"""
bot/views/visit_proposal.py
===========================
The lead's one-tap answer to "is that day still good?" (spec §11.1).

This is the only place a future-dated visit becomes a real booking. Until the
lead taps yes it is a plan they nodded at in chat, and the diary is right not
to show it.

Public and token-gated, like the other two emailed forms. It is a GET, not a
POST, because it is a link in an email: there is no page to load first and
nothing to fill in. That means a mail client that pre-fetches links could
answer on the lead's behalf, so the page is written to be recoverable — a wrong
answer is a message away from being fixed, and the alternative, a form, loses
the one-tap property that makes people answer at all.
"""

import logging

from django.shortcuts import get_object_or_404, render

from ..models import VisitProposal
from ..visit_proposal import apply_lead_answer

logger = logging.getLogger(__name__)


def visit_proposal_answer(request, token, answer):
    """Yes or no to the penciled-in visit."""
    row = get_object_or_404(
        VisitProposal.objects.select_related('appointment', 'tenant'),
        token=token)

    wanted = (answer or '').strip().lower()
    if wanted not in ('yes', 'no'):
        # Not reachable through the URL pattern, but a hand-typed link should
        # show the state rather than a 500.
        wanted = ''

    already = not row.is_open
    if wanted and not already:
        try:
            apply_lead_answer(row, confirmed=(wanted == 'yes'))
        except Exception:
            # _book_the_visit re-raises if the diary write fails. Never tell a
            # customer their visit is booked when it is not.
            logger.exception('Could not record the visit answer for apt %s',
                             row.appointment_id)
            return render(request, 'bot/pages/visit_proposal_answer.html', {
                'row': row,
                'appointment': row.appointment,
                'state': 'error',
            }, status=500)

    row.refresh_from_db()
    return render(request, 'bot/pages/visit_proposal_answer.html', {
        'row': row,
        'appointment': row.appointment,
        'state': 'already' if already else row.state,
    })
