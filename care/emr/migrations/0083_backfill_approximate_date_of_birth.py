import datetime

from django.db import migrations


def backfill(apps, schema_editor):
    # v12.1 D05: patients registered by age before approximate dates of birth
    # have only a year of birth. Give them 1 July of that year, flagged as an
    # estimate, like set_date_of_birth does for new ones.
    Patient = apps.get_model("emr", "Patient")
    patients = Patient.objects.filter(
        date_of_birth__isnull=True, year_of_birth__isnull=False
    ).only("id", "year_of_birth", "meta")
    for patient in patients.iterator():
        patient.date_of_birth = datetime.date(patient.year_of_birth, 7, 1)
        patient.meta = {**(patient.meta or {}), "date_of_birth_approximate": True}
        patient.save(update_fields=["date_of_birth", "meta"])


class Migration(migrations.Migration):
    dependencies = [
        ("emr", "0082_remove_encounter_encounter_class_history"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
