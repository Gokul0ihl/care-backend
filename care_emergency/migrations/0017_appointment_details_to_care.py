"""The desk details now live on Care's own TokenBooking, AppointmentSequence
and Availability (the fork's emr 0084). Copy what the plugin held, then drop
its tables."""

from django.db import migrations

BOOKING_FIELDS = [
    "appointment_number",
    "consultation_type",
    "visit_purpose",
    "booking_channel",
    "reminder_due_at",
    "reminder_sent_at",
    "guidance_area",
    "guidance_since",
    "chief_complaint",
]


def forward(apps, schema_editor):
    AppointmentDetails = apps.get_model("care_emergency", "AppointmentDetails")
    OldSequence = apps.get_model("care_emergency", "AppointmentSequence")
    WalkInReserve = apps.get_model("care_emergency", "WalkInReserve")
    TokenBooking = apps.get_model("emr", "TokenBooking")
    NewSequence = apps.get_model("emr", "AppointmentSequence")
    Availability = apps.get_model("emr", "Availability")

    for details in AppointmentDetails.objects.iterator():
        TokenBooking.objects.filter(pk=details.booking_id).update(
            **{field: getattr(details, field) for field in BOOKING_FIELDS}
        )
    for sequence in OldSequence.objects.all():
        NewSequence.objects.update_or_create(
            year=sequence.year, defaults={"last_value": sequence.last_value}
        )
    for reserve in WalkInReserve.objects.all():
        Availability.objects.filter(pk=reserve.availability_id).update(
            walk_in_every=reserve.walk_in_every
        )


def backward(apps, schema_editor):
    AppointmentDetails = apps.get_model("care_emergency", "AppointmentDetails")
    OldSequence = apps.get_model("care_emergency", "AppointmentSequence")
    WalkInReserve = apps.get_model("care_emergency", "WalkInReserve")
    TokenBooking = apps.get_model("emr", "TokenBooking")
    NewSequence = apps.get_model("emr", "AppointmentSequence")
    Availability = apps.get_model("emr", "Availability")

    # A rescheduled booking keeps a copy of the number its successor holds;
    # the plugin's number was unique, so only the successor goes back.
    bookings = (
        TokenBooking.objects.filter(appointment_number__isnull=False)
        .exclude(status="rescheduled")
        .select_related("token_slot__resource")
    )
    for booking in bookings.iterator():
        AppointmentDetails.objects.create(
            booking_id=booking.pk,
            facility_id=booking.token_slot.resource.facility_id,
            **{field: getattr(booking, field) for field in BOOKING_FIELDS},
        )
    for sequence in NewSequence.objects.all():
        OldSequence.objects.create(year=sequence.year, last_value=sequence.last_value)
    for availability in Availability.objects.filter(walk_in_every__isnull=False):
        WalkInReserve.objects.create(
            availability_id=availability.pk,
            walk_in_every=availability.walk_in_every,
        )


class Migration(migrations.Migration):
    dependencies = [
        ("care_emergency", "0016_app_patient_link"),
        ("emr", "0084_appointment_desk_details"),
    ]

    operations = [
        migrations.RunPython(forward, backward),
        migrations.DeleteModel(name="AppointmentDetails"),
        migrations.DeleteModel(name="AppointmentSequence"),
        migrations.DeleteModel(name="WalkInReserve"),
    ]
