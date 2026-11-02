from django.db import migrations, models


def to_method(apps, schema_editor):
    # The boolean column became text ("true"/"false"). Before this change the Capture Id
    # dialog only took photos with the camera, so a captured ID is recorded as "camera".
    Patient = apps.get_model("emr", "Patient")
    Patient.objects.filter(capture_id_method="true").update(capture_id_method="camera")
    Patient.objects.exclude(
        capture_id_method__in=["camera", "scanner", "upload"]
    ).update(capture_id_method="number_typed")


def to_boolean(apps, schema_editor):
    Patient = apps.get_model("emr", "Patient")
    Patient.objects.filter(capture_id_method="number_typed").update(
        capture_id_method="false"
    )
    Patient.objects.exclude(capture_id_method="false").update(capture_id_method="true")


class Migration(migrations.Migration):
    dependencies = [
        ("emr", "0088_patient_capture_id_method"),
    ]

    operations = [
        migrations.AlterField(
            model_name="patient",
            name="capture_id_method",
            field=models.CharField(blank=True, default="number_typed", max_length=12),
        ),
        migrations.RunPython(to_method, to_boolean),
    ]
