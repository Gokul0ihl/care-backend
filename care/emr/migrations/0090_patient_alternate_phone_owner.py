from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("emr", "0089_patient_capture_id_method_enum"),
    ]

    operations = [
        migrations.AddField(
            model_name="patient",
            name="alternate_phone_owner",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
    ]
