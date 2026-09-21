"""Clear job descriptions that are chat, not a job.

WHAT: sets ``project_description`` to NULL where it carries no content word
("Ok thank ,I will let you", "Ok\\nNow you are talking").

WHY: those were stored before ``Appointment.save`` got its net (see
bot/job_text.py). A filled description means the flow never asks what the job
is, and the plumber-link message quoted it back as the lead's own words.
Checked against production on 2026-09-21: 2 of 285 descriptions matched, both
chat (leads 868 and 1217), and no real description did.

HOW: the rule is FROZEN here rather than imported, because a migration must
give the same answer however bot/job_text.py changes later. Update-only
through the historical model, so Appointment.save is not involved. Reverse is
a no-op: the cleared text was never a description.
"""

import re

from django.db import migrations

_CONTENTLESS = frozenset("""
hi hello hey ok okay alright cool sharp thanks thank noted yes no yep nope sure
hongu kwete ndatenda maita basa whenever anytime any time day today tomorrow
tonight morning afternoon evening week weekend month monday tuesday wednesday
thursday friday saturday sunday asap soon later now am pm mangwana nhasi manheru
mangwanani svondo muvhuro chipiri chitatu china chishanu mugovera nguva chero a
an the and but or of to for in at on is are was were be been do does did can
could will would should have has had i im me my we us our you your it its they
them their he she this that there here what when where who why how suits suit
works work fine good great nice please just still want need like think know get
got going go come send call text message then maybe also much many some side
not dont ill lets let choose chose pick decide up down over out anything
whatever mind sarudzai imi zvakanaka really very quite so well else talking
sounds sound perfect awesome excellent lovely amazing wow true right agreed agree
deal done correct exactly indeed understood understand see seen cheers bye yeah
yebo kk k lol haha hmm oh ohh ah thankyou thanx thx ty more reasonable fair
affordable expensive cheap price prices ehe eya aiwa zvaita mazvita tatenda
youre thats whats theres ive id weve theyre cant wont isnt
""".split())


def _describes_a_job(text):
    tokens = [t.replace("'", '') for t in re.findall(r"[a-z']+", (text or '').lower())]
    return any(t and t not in _CONTENTLESS for t in tokens)


def clear_chat_descriptions(apps, schema_editor):
    Appointment = apps.get_model('bot', 'Appointment')
    rows = (Appointment.objects.exclude(project_description__isnull=True)
            .exclude(project_description='').only('id', 'project_description'))
    ids = [row.id for row in rows if not _describes_a_job(row.project_description)]
    if ids:
        Appointment.objects.filter(id__in=ids).update(project_description=None)


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0092_alter_sentemail_category'),
    ]

    operations = [
        migrations.RunPython(clear_chat_descriptions, migrations.RunPython.noop),
    ]
