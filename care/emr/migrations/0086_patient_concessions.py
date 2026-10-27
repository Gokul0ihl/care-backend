from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("emr", "0085_fold_care_emergency")]
    operations = [
        migrations.AddField(
            model_name="patient",
            name="concessions",
            field=models.JSONField(default=list, blank=True),
        ),
    ]
