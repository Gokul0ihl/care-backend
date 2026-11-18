"""The patient registration timeline and audit trail (docs/registration-audit.md).

Every registration path calls record_patient_event() inside its own
transaction, so a registration and its audit row are saved together or not at
all. A row keeps who did it (user, their roles at the facility), where
(facility), when (created_date) and what (action, channel, the patient's
number, the names of the fields an edit changed). It never keeps field values:
the patient record holds those, and the trail is read by people who may not
be allowed to see them.
"""

from django.db.models import Q

from hmis.models import PatientEvent, PatientEventActions, PatientEventChannels
from hmis.services import display_numbers

PATIENT_ROLE = "Patient"
SUPERUSER_ROLE = "Super admin"
SERVICE_ROLE = "Service account"

# Bookkeeping that every save touches, and secrets: never reported as changed.
IGNORED_FIELDS = {
    "id",
    "external_id",
    "created_date",
    "modified_date",
    "created_by",
    "updated_by",
    "history",
    "meta",
    "instance_identifiers",
    "facility_identifiers",
    "quick_form_token",
    "quick_form_expires_at",
}


def user_roles(user, facility):
    """The names of the roles the user holds at the facility (any of its
    departments), or what kind of account it is when it holds none there."""
    from care.emr.models.organization import FacilityOrganizationUser

    if user is None or not user.is_authenticated:
        return [PATIENT_ROLE]
    assignments = FacilityOrganizationUser.objects.filter(user=user)
    if facility is not None:
        assignments = assignments.filter(organization__facility=facility)
    names = sorted(set(assignments.values_list("role__name", flat=True)))
    if names:
        return names
    if getattr(user, "is_service_account", False):
        return [SERVICE_ROLE]
    if user.is_superuser:
        return [SUPERUSER_ROLE]
    return []


def field_snapshot(patient):
    """The patient's own columns, to tell which ones an edit changed."""
    values = {}
    for field in patient._meta.concrete_fields:  # noqa: SLF001
        if field.name not in IGNORED_FIELDS:
            values[field.name] = getattr(patient, field.attname)
    allergies = getattr(patient, "desk_allergies", None)
    if allergies is not None:
        values["allergies"] = sorted(
            allergies.values_list("substance", "reaction", "severity")
        )
    return values


def changed_fields(before, after):
    return sorted(key for key in after if before.get(key) != after[key])


def record_patient_event(
    patient,
    action,
    channel,
    user=None,
    facility=None,
    *,
    details=None,
):
    """Appends one row. user None means the patient acted on their own record."""
    if user is not None and not user.is_authenticated:
        user = None
    number = display_numbers([patient.id]).get(patient.id) or {}
    return PatientEvent.objects.create(
        patient=patient,
        action=action,
        channel=channel,
        user=user,
        username=user.username if user else "",
        user_full_name=(user.full_name or "").strip() if user else "",
        roles=user_roles(user, facility),
        facility=facility,
        facility_name=facility.name if facility else "",
        patient_number=number.get("value") or "",
        details=details or {},
    )


def timeline_patients(patient, hops=10):
    """The patient and every record merged into it (and into those), so the
    kept record's timeline also shows the duplicates' registrations."""
    from hmis.models import PatientMerge

    ids = {patient.id}
    frontier = {patient.id}
    for _ in range(hops):
        sources = (
            set(
                PatientMerge.objects.filter(target_id__in=frontier).values_list(
                    "source_id", flat=True
                )
            )
            - ids
        )
        if not sources:
            break
        ids |= sources
        frontier = sources
    return ids


def serialize_patient_event(event):
    user = event.user
    return {
        "id": event.id,
        "action": event.action,
        "action_label": PatientEventActions(event.action).label,
        "channel": event.channel,
        "channel_label": PatientEventChannels(event.channel).label,
        "patient": str(event.patient.external_id),
        "patient_number": event.patient_number,
        "user": {
            "id": str(user.external_id) if user else None,
            "username": event.username,
            "full_name": event.user_full_name,
        }
        if event.username
        else None,
        "roles": event.roles,
        "facility": {
            "id": str(event.facility.external_id) if event.facility else None,
            "name": event.facility_name,
        }
        if event.facility_name
        else None,
        "details": event.details,
        "created_date": event.created_date,
    }


def search_events(queryset, value):
    return queryset.filter(
        Q(username__icontains=value)
        | Q(user_full_name__icontains=value)
        | Q(patient_number__icontains=value)
        | Q(facility_name__icontains=value)
    )
