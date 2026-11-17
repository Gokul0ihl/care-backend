# Merges the access-control-portal branch's emr migration with care-modification's.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("emr", "0091_role_assignment_expires_at"),
        ("emr", "0092_token_booking_extra_slots"),
    ]

    operations = []
