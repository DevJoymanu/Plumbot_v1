"""Finding a lead by typing what you remember about them.

ONE resolver, so every screen that offers a lead search finds the same leads
from the same words: the conversations inbox, the priority-leads board and the
quote editors' lead picker. Three separate searches would each have their own
idea of which fields count, and a lead that turns up on one page and not
another reads as missing data rather than as a filter.

Two rules make it "simple" rather than merely present:

  * A phone number is matched on its DIGITS. People type the number the way
    they read it off a phone — "077 123 4567", "+263 77 123 4567" — and the
    stored value is "whatsapp:+263771234567". Matching the string as typed
    found nothing, which reads as "we have no such lead".
  * A local leading 0 is dropped before matching, because 0771234567 and
    +263771234567 are the same person.
"""
import re

from django.db.models import Q

#: Everything a person might remember about a lead. project_type is in here
#: because "the bathroom job in Borrowdale" is how people actually describe one.
LEAD_SEARCH_FIELDS = (
    'customer_name',
    'phone_number',
    'customer_area',
    'customer_email',
    'project_type',
)


def phone_digits(query: str) -> str:
    """The digits of a typed number, in the form the stored one carries.

    A local 0 prefix is dropped: 0771234567 and +263771234567 are the same
    number, and the second is what a WhatsApp lead is stored as.
    """
    digits = re.sub(r'\D', '', query or '')
    if len(digits) > 6 and digits.startswith('0'):
        digits = digits.lstrip('0')
    return digits


def lead_search_q(query: str):
    """The Q for a typed search, or None when there is nothing to search for."""
    query = (query or '').strip()
    if not query:
        return None

    clause = Q()
    for field in LEAD_SEARCH_FIELDS:
        clause |= Q(**{f'{field}__icontains': query})

    # A number typed with spaces, brackets or a local 0 still has to find the
    # lead it belongs to.
    digits = phone_digits(query)
    if len(digits) >= 4:
        clause |= Q(phone_number__icontains=digits)
    return clause


def filter_leads(queryset, query):
    """Narrow a lead queryset to a typed search. No query means no change."""
    clause = lead_search_q(query)
    return queryset if clause is None else queryset.filter(clause)
