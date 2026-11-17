"""OPD visit stages and vitals stations (OPD01-03): our layer over Care.

Care's queue is a Token in one doctor's TokenQueue for one day, with status
created → in progress → fulfilled. An OPD visit passes through several
stages (vitals station, doctor, then pharmacy and departments), and a vitals
station may serve one doctor or all of them. Rather than reshape Care's
Token, an OpdVisit row follows each outpatient encounter through the stages;
Care's encounter, booking and token stay as they are.

docs/opd-vitals-to-rx.md explains the design.
"""

from datetime import date

from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from hmis.constants import OpdStage

SENIOR_AGE = 60
WAITING = (OpdStage.waiting_vitals, OpdStage.at_vitals)


def age_in_years(patient, today=None):
    today = today or timezone.localdate()
    if patient.date_of_birth:
        born = patient.date_of_birth
        return (
            today.year - born.year - ((today.month, today.day) < (born.month, born.day))
        )
    if patient.year_of_birth:
        return today.year - patient.year_of_birth
    return None


def priority_tier(age, *, pregnant, has_booking):
    """OPD03: seniors and pregnant women first, then appointments, then
    walk-ins. Within a tier, earlier arrivals go first."""
    if (age is not None and age >= SENIOR_AGE) or pregnant:
        return 0
    if has_booking:
        return 1
    return 2


def priority_reasons(visit):
    reasons = []
    age = age_in_years(visit.encounter.patient)
    if age is not None and age >= SENIOR_AGE:
        reasons.append("senior")
    if visit.encounter.patient.pregnancy_status == "pregnant":
        reasons.append("pregnant")
    if visit.booking_id:
        reasons.append("appointment")
    return reasons


def sort_queue(visits):
    """The station's waiting list in calling order."""

    def key(visit):
        return (
            priority_tier(
                age_in_years(visit.encounter.patient),
                pregnant=visit.encounter.patient.pregnancy_status == "pregnant",
                has_booking=bool(visit.booking_id),
            ),
            visit.arrived_at,
            visit.queue_number,
        )

    return sorted(visits, key=key)


def station_for(facility, doctor):
    """The doctor's own station if one lists them, else the facility's
    central station, else none (the visit waits unassigned)."""
    from hmis.models import OpdStation

    stations = OpdStation.objects.filter(facility=facility, is_active=True)
    if doctor is not None:
        own = stations.filter(doctors=doctor).order_by("code").first()
        if own is not None:
            return own
    return stations.filter(is_central=True).order_by("code").first()


def _history(visit, stage, user):
    visit.stage_history = [
        *visit.stage_history,
        {
            "stage": stage,
            "at": timezone.now().isoformat(),
            "by": str(user.external_id) if user else None,
        },
    ]


def move(visit, stage, user, extra=()):
    visit.stage = stage
    _history(visit, stage, user)
    visit.updated_by = user
    visit.save(
        update_fields=["stage", "stage_history", "updated_by", "modified_date", *extra]
    )


def _booking_doctor(booking):
    resource = booking.token_slot.resource if booking else None
    return getattr(resource, "user", None) if resource else None


def open_visit(encounter, user=None, doctor=None):
    """The OpdVisit for an outpatient encounter, created at the first stage.
    The queue number is the next one at the facility today."""
    from hmis.models import OpdVisit

    existing = OpdVisit.objects.filter(encounter=encounter).first()
    if existing is not None:
        return existing
    booking = getattr(encounter, "appointment", None)
    doctor = doctor or _booking_doctor(booking)
    today = timezone.localdate()
    now = timezone.now()
    for _ in range(5):
        number = (
            OpdVisit.objects.filter(facility=encounter.facility, date=today).aggregate(
                n=Max("queue_number")
            )["n"]
            or 0
        ) + 1
        visit = OpdVisit(
            facility=encounter.facility,
            encounter=encounter,
            booking=booking,
            doctor=doctor,
            station=station_for(encounter.facility, doctor),
            date=today,
            queue_number=number,
            arrived_at=now,
            created_by=user,
            updated_by=user,
        )
        _history(visit, OpdStage.waiting_vitals, user)
        try:
            with transaction.atomic():
                visit.save()
            return visit
        except IntegrityError:
            continue  # another desk took the number; take the next one
    raise ValidationError("Couldn't issue a queue number; try again")


def station_visits(station, day: date | None = None):
    """Today's visits for a station. A central station also takes visits
    that have no station (their doctor had none when they arrived)."""
    from hmis.models import OpdVisit

    day = day or timezone.localdate()
    where = Q(station=station)
    if station.is_central:
        where |= Q(station__isnull=True)
    return OpdVisit.objects.filter(
        where, facility=station.facility, date=day
    ).select_related("encounter__patient", "doctor", "booking")


def call_next(station, user, quiet=False):
    """OPD03: calls the next waiting patient to the station. One patient at a
    station at a time. With quiet=True (after a save) an empty queue or a
    busy station returns None instead of an error."""
    with transaction.atomic():
        visits = station_visits(station).select_for_update(of=("self",))
        if visits.filter(stage=OpdStage.at_vitals).exists():
            if quiet:
                return None
            raise ValidationError("Finish the patient at the station first")
        waiting = sort_queue(list(visits.filter(stage=OpdStage.waiting_vitals)))
        if not waiting:
            if quiet:
                return None
            raise ValidationError("No one is waiting for this station")
        visit = waiting[0]
        visit.station = station
        visit.called_at = timezone.now()
        move(visit, OpdStage.at_vitals, user, extra=["station", "called_at"])
        return visit
