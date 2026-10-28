from django.db import migrations

# The fixture facilities were named in capitals; the demo shows them as the real
# centres (post-testing sync PTS-25). Only these exact fixture names are renamed,
# so a facility an admin has named is never touched.
RENAMES = {
    "FACILITY WITH PATIENTS": "Lajpat Nagar",
    "SECONDARY FACILITY": "Faridabad",
}


def rename(apps, schema_editor):
    Facility = apps.get_model("facility", "Facility")
    for old, new in RENAMES.items():
        Facility.objects.filter(name=old).update(name=new)


def restore(apps, schema_editor):
    Facility = apps.get_model("facility", "Facility")
    for old, new in RENAMES.items():
        Facility.objects.filter(name=new).update(name=old)


class Migration(migrations.Migration):
    dependencies = [
        ("facility", "0488_fold_care_emergency"),
        ("hmis", "0006_mobileregistration_patientaccount_password"),
    ]

    operations = [migrations.RunPython(rename, restore)]
