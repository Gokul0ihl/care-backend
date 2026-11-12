# Outreach is health camps (CAMP) and patient education sessions (PES) only
# (HM-239). Any CME or webinar event left over is cancelled and soft-deleted so
# it drops out of every list and lookup, then the CME and online-only columns
# go. The rows themselves stay in the table.

from django.db import migrations


def retire_cme_and_webinars(apps, schema_editor):
    OutreachEvent = apps.get_model("hmis", "OutreachEvent")
    OutreachEvent.objects.filter(event_type__in=["CME", "WEB"]).update(
        status="cancelled", deleted=True
    )


class Migration(migrations.Migration):
    dependencies = [
        ("hmis", "0014_outreach_event_questionnaire"),
    ]

    operations = [
        migrations.RunPython(retire_cme_and_webinars, migrations.RunPython.noop),
        migrations.RemoveField(model_name="outreachevent", name="cme_audience"),
        migrations.RemoveField(
            model_name="outreachevent", name="cme_accreditation_body"
        ),
        migrations.RemoveField(model_name="outreachevent", name="cme_credit_hours"),
        migrations.RemoveField(model_name="outreachevent", name="platform"),
        migrations.RemoveField(model_name="outreachevent", name="join_link"),
    ]
