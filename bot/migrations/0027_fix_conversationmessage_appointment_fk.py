from django.db import migrations


DROP_APPOINTMENT_FK_SQL = """
DO $$
DECLARE fk_name text;
BEGIN
    SELECT con.conname
    INTO fk_name
    FROM pg_constraint con
    JOIN pg_class rel ON rel.oid = con.conrelid
    JOIN pg_attribute att
      ON att.attrelid = rel.oid
     AND att.attnum = ANY(con.conkey)
    WHERE rel.relname = 'bot_conversationmessage'
      AND con.contype = 'f'
      AND att.attname = 'appointment_id'
    LIMIT 1;

    IF fk_name IS NOT NULL THEN
        EXECUTE format('ALTER TABLE bot_conversationmessage DROP CONSTRAINT %I', fk_name);
    END IF;
END $$;
"""


ADD_CASCADE_FK_SQL = """
ALTER TABLE bot_conversationmessage
ADD CONSTRAINT bot_conversationmessage_appointment_id_fk
FOREIGN KEY (appointment_id)
REFERENCES bot_appointment(id)
ON DELETE CASCADE;
"""


ADD_NO_ACTION_FK_SQL = """
ALTER TABLE bot_conversationmessage
ADD CONSTRAINT bot_conversationmessage_appointment_id_fk
FOREIGN KEY (appointment_id)
REFERENCES bot_appointment(id)
ON DELETE NO ACTION;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("bot", "0026_remove_appointment_plan_followup_attempts_and_more"),
    ]

    operations = [
        migrations.RunSQL(
            sql=DROP_APPOINTMENT_FK_SQL + ADD_CASCADE_FK_SQL,
            reverse_sql=DROP_APPOINTMENT_FK_SQL + ADD_NO_ACTION_FK_SQL,
        ),
    ]
