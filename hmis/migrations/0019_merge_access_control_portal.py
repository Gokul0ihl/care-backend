# Merges the access-control-portal branch's hmis migrations (platform admin,
# branding) with care-modification's outreach migrations.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("hmis", "0015_branding"),
        ("hmis", "0018_outreach_event_end_date"),
    ]

    operations = []
