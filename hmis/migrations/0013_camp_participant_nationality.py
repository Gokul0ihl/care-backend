from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("hmis", "0012_outreach_event_logins"),
    ]

    operations = [
        migrations.AddField(
            model_name="campparticipant",
            name="nationality",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
    ]
