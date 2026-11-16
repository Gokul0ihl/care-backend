from django.db import migrations, models


def fill_end_date(apps, schema_editor):
    """Events saved before end_date existed were one-day events."""
    OutreachEvent = apps.get_model("hmis", "OutreachEvent")
    OutreachEvent.objects.filter(end_date__isnull=True).update(
        end_date=models.F("event_date")
    )


class Migration(migrations.Migration):
    dependencies = [
        ("hmis", "0017_camp_age_and_longer_names"),
    ]

    operations = [
        migrations.AddField(
            model_name="outreachevent",
            name="end_date",
            field=models.DateField(null=True),
        ),
        migrations.RunPython(fill_end_date, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="outreachevent",
            name="end_date",
            field=models.DateField(),
        ),
    ]
