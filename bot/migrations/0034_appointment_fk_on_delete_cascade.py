from django.db import migrations


# (child_table, child_column) for every FK that references bot_appointment
# with Django on_delete=CASCADE. Mirrors the model definitions; assigned_plumber
# (SET_NULL -> auth_user) is intentionally excluded.
APPOINTMENT_CASCADE_FKS = [
    ('bot_conversationmessage', 'appointment_id'),
    ('bot_scheduledfollowup', 'appointment_id'),
    ('bot_appointmentnote', 'appointment_id'),
    ('bot_appointmentreminder', 'appointment_id'),
    ('bot_quotation', 'appointment_id'),
    ('bot_job', 'site_visit_id'),
    ('bot_appointment', 'parent_site_visit_id'),
]


def _build_sql(on_delete_clause):
    """Build a PL/pgSQL block that recreates each appointment FK with the
    given ON DELETE behaviour. The existing constraint is found by table/column
    rather than by name, so Django's hash-suffixed constraint names don't matter.
    """
    pairs = ',\n            '.join(
        "ARRAY['%s','%s']" % (table, column)
        for table, column in APPOINTMENT_CASCADE_FKS
    )
    return """
DO $$
DECLARE
    pairs TEXT[][] := ARRAY[
            %s
    ];
    p TEXT[];
    existing_name TEXT;
    new_name TEXT;
BEGIN
    FOREACH p SLICE 1 IN ARRAY pairs LOOP
        SELECT con.conname INTO existing_name
        FROM pg_constraint con
        JOIN pg_class rel ON rel.oid = con.conrelid
        JOIN pg_attribute att ON att.attrelid = con.conrelid
                              AND att.attnum = ANY(con.conkey)
        JOIN pg_class fref ON fref.oid = con.confrelid
        WHERE con.contype = 'f'
          AND rel.relname = p[1]
          AND att.attname = p[2]
          AND fref.relname = 'bot_appointment'
        LIMIT 1;

        IF existing_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %%I DROP CONSTRAINT %%I', p[1], existing_name);
            new_name := p[1] || '_' || p[2] || '_appt_fk';
            EXECUTE format(
                'ALTER TABLE %%I ADD CONSTRAINT %%I FOREIGN KEY (%%I) '
                'REFERENCES bot_appointment(id) %s DEFERRABLE INITIALLY DEFERRED',
                p[1], new_name, p[2]
            );
        END IF;
        existing_name := NULL;
    END LOOP;
END $$;
""" % (pairs, on_delete_clause)


class Migration(migrations.Migration):

    dependencies = [
        ('bot', '0033_scheduledfollowup_template_key_and_more'),
    ]

    operations = [
        migrations.RunSQL(
            sql=_build_sql('ON DELETE CASCADE'),
            reverse_sql=_build_sql('ON DELETE NO ACTION'),
        ),
    ]
