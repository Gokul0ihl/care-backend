"""Possible existing records for a patient about to be registered at the desk.

The desk form asks before saving whether the person is already registered,
matched on any of:

- phone: the same mobile number (a desk "No phone" number never matches);
- identity: the same government ID type and last four characters (all the
  desk stores of an ID), with the same first name or birth year, since four
  characters alone match about 1 in 10,000 patients; or the same passport
  number, which is stored in full;
- name_birth: the same name with the same date of birth, or the same birth
  year when either side is only an age.

Only patients the caller can view are returned, and restricted patients only
to staff who may see their identity.
"""

from django.db.models import Q
from django.utils import timezone

from care.emr.models import Patient
from care.security.authorization import AuthorizationController
from hmis.contact import is_desk_phone
from hmis.restricted import can_see_identity, is_restricted
from hmis.services import display_numbers

PER_RULE = 20
MAX_RESULTS = 10


def birth_years(spec):
    """The birth years the form's date of birth or age allows: an age of N
    today is born in one of two years."""
    if spec.date_of_birth:
        return {spec.date_of_birth.year}
    if spec.age is not None:
        year = timezone.localdate().year - spec.age
        return {year - 1, year}
    return set()


def birth_filter(spec):
    years = birth_years(spec)
    if spec.date_of_birth:
        # The exact date, or an estimate saved from an age in that year.
        return Q(date_of_birth=spec.date_of_birth) | Q(
            meta__date_of_birth_approximate=True, year_of_birth__in=years
        )
    if years:
        return Q(year_of_birth__in=years)
    return None


def name_filter(spec):
    name = " ".join(spec.name.split())
    query = Q(name__iexact=name) if name else Q(pk__in=[])
    first, last = spec.first_name.strip(), spec.last_name.strip()
    if first and last:
        # A record saved with or without the middle name.
        query |= Q(first_name__iexact=first, last_name__iexact=last)
    return query if name or (first and last) else None


def rules(spec):
    """(reason, filter) for each rule the form has enough for."""
    found = []
    phone = spec.phone_number.strip()
    if phone and not is_desk_phone(phone):
        found.append(("phone", Q(phone_number=phone)))
    identity = []
    if spec.identity_card_type and spec.identity_number_last4:
        corroboration = Q(pk__in=[])
        if spec.first_name.strip():
            corroboration |= Q(first_name__iexact=spec.first_name.strip())
        if years := birth_years(spec):
            corroboration |= Q(year_of_birth__in=years)
        identity.append(
            Q(
                identity_card_type=spec.identity_card_type,
                identity_number_last4=spec.identity_number_last4.upper(),
            )
            & corroboration
        )
    if spec.passport_number:
        identity.append(Q(passport_number=spec.passport_number.upper()))
    if identity:
        query = identity[0]
        for other in identity[1:]:
            query |= other
        found.append(("identity", query))
    name, birth = name_filter(spec), birth_filter(spec)
    if name is not None and birth is not None:
        found.append(("name_birth", name & birth))
    return found


def find_matches(user, spec):
    reasons = {}
    patients = {}
    for reason, query in rules(spec):
        for patient in Patient.objects.filter(query).order_by("-modified_date")[
            :PER_RULE
        ]:
            patients[patient.id] = patient
            reasons.setdefault(patient.id, []).append(reason)
    visible = [
        patient
        for patient in patients.values()
        if AuthorizationController.call("can_view_patient_obj", user, patient)
        and not (is_restricted(patient) and not can_see_identity(user, patient))
    ]
    # Most reasons first, then the most recently updated.
    visible.sort(key=lambda patient: patient.modified_date, reverse=True)
    visible.sort(key=lambda patient: len(reasons[patient.id]), reverse=True)
    visible = visible[:MAX_RESULTS]
    numbers = display_numbers([patient.id for patient in visible])
    return [
        serialize_match(patient, reasons[patient.id], numbers) for patient in visible
    ]


def serialize_match(patient, reasons, numbers):
    return {
        "id": str(patient.external_id),
        "name": patient.name,
        "gender": patient.gender,
        "date_of_birth": patient.date_of_birth,
        "date_of_birth_approximate": bool(
            (patient.meta or {}).get("date_of_birth_approximate")
        ),
        "year_of_birth": patient.year_of_birth,
        "phone_number": patient.phone_number,
        "identity_card_type": patient.identity_card_type,
        "identity_number_last4": patient.identity_number_last4,
        "patient_number": numbers.get(patient.id),
        "reasons": reasons,
    }
