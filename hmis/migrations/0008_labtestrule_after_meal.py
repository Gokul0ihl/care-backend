from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hmis", "0007_rename_demo_facilities"),
    ]

    operations = [
        migrations.AddField(
            model_name="labtestrule",
            name="after_meal",
            field=models.BooleanField(default=False),
        ),
    ]
