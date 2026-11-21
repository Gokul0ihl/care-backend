# Merges the opd-vitals branch's hmis migration (OPD vitals station) with
# care-modification's VIP, patient-event and mobile-appointment migrations.

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("hmis", "0020_opd_vitals"),
        ("hmis", "0023_mobile_appointment_change"),
    ]

    operations = []
