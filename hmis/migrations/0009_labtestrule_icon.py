from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hmis", "0008_labtestrule_after_meal"),
    ]

    operations = [
        migrations.AddField(
            model_name="labtestrule",
            name="icon",
            field=models.CharField(blank=True, default="", max_length=40),
        ),
    ]
