"""OPD vitals station: stations, the station queue, walk-ins and the vitals
save (OPD01-08). docs/opd-vitals-to-rx.md explains the design."""

from django.db import IntegrityError, transaction
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.api.viewsets.encounter import EncounterViewSet
from care.emr.models import Encounter, Patient
from care.emr.models.patient import PatientIdentifier
from care.users.models import User
from care.utils.shortcuts import get_object_or_404
from hmis import opd_vitals
from hmis.constants import PATIENT_NUMBER_SYSTEMS, OpdStage, PatientNumberSeries
from hmis.models import OpdStation, OpdVisit
from hmis.opd import (
    age_in_years,
    call_next,
    open_visit,
    priority_reasons,
    sort_queue,
    station_for,
    station_visits,
)
from hmis.restricted import mask_patient
from hmis.specs import (
    OpdStationCreateSpec,
    OpdStationNurseSpec,
    OpdStationUpdateSpec,
    OpdVitalsSaveSpec,
    OpdWalkInSpec,
)
from hmis.views.base import EmergencyAPIView, can, care_view, get_facility

# Recently finished patients shown under a station's queue.
DONE_SHOWN = 20
# The queue shows only the last digits of a patient's mobile.
PHONE_DIGITS_SHOWN = 4


def _can_work(user, facility):
    return can("can_create_encounter_obj", user, facility) or can(
        "can_update_facility_obj", user, facility
    )


def _require_work(user, facility):
    if not _can_work(user, facility):
        raise PermissionDenied("You do not have access to this facility's OPD")


def _user(user):
    if user is None:
        return None
    return {"id": str(user.external_id), "name": user.get_full_name() or user.username}


def _masked_phone(phone):
    phone = phone or ""
    if len(phone) < PHONE_DIGITS_SHOWN:
        return ""
    return f"•••••{phone[-PHONE_DIGITS_SHOWN:]}"


def _uhids(patients):
    system = PATIENT_NUMBER_SYSTEMS[PatientNumberSeries.uhid]
    rows = PatientIdentifier.objects.filter(
        patient__in=patients, config__config__system=system
    ).values_list("patient_id", "value")
    return dict(rows)


def serialize_visit(visit, user, uhids=None):
    patient = visit.encounter.patient
    mapping = {
        "id": str(patient.external_id),
        "name": patient.name,
        "gender": patient.gender,
        "age": age_in_years(patient),
        "phone": _masked_phone(patient.phone_number),
        "uhid": (uhids or {}).get(patient.id),
    }
    mask_patient(mapping, patient, user)
    return {
        "id": str(visit.external_id),
        "queue_number": visit.queue_number,
        "stage": visit.stage,
        "stage_label": OpdStage(visit.stage).label,
        "patient": mapping,
        "encounter": str(visit.encounter.external_id),
        "doctor": _user(visit.doctor),
        "station": visit.station.code if visit.station_id else None,
        "is_walk_in": not visit.booking_id,
        "priority": priority_reasons(visit),
        "arrived_at": visit.arrived_at,
        "called_at": visit.called_at,
        "vitals_done_at": visit.vitals_done_at,
    }


def serialize_station(station):
    return {
        "id": str(station.external_id),
        "code": station.code,
        "name": station.name,
        "is_central": station.is_central,
        "is_active": station.is_active,
        "doctors": [_user(d) for d in station.doctors.all().order_by("first_name")],
        "nurse": _user(station.nurse),
        "nurse_status": station.nurse_status,
    }


def serialize_record(record):
    return {
        "id": str(record.external_id),
        "visit": str(record.visit.external_id),
        "questionnaire_response": str(record.questionnaire_response.external_id),
        "status": record.status,
        "replaces": str(record.replaces.external_id) if record.replaces_id else None,
        "sources": record.sources,
        **record.summary,
        "created_date": record.created_date,
    }


def _station(station_id):
    return get_object_or_404(
        OpdStation.objects.select_related("facility", "nurse"),
        external_id=station_id,
        deleted=False,
    )


def _doctors(facility, ids):
    doctors = list(User.objects.filter(external_id__in=ids))
    if len(doctors) != len(set(ids)):
        raise ValidationError({"doctors": "Unknown doctor"})
    return doctors


class OpdStationView(EmergencyAPIView):
    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        _require_work(request.user, facility)
        stations = OpdStation.objects.filter(facility=facility, deleted=False)
        if request.query_params.get("active") == "true":
            stations = stations.filter(is_active=True)
        stations = stations.select_related("nurse").prefetch_related("doctors")
        return Response(
            {"results": [serialize_station(s) for s in stations.order_by("code")]}
        )

    def post(self, request):
        spec = OpdStationCreateSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_update_facility_obj", request.user, facility):
            raise PermissionDenied("Only facility admins can set up vitals stations")
        doctors = _doctors(facility, spec.doctors)
        try:
            with transaction.atomic():
                station = OpdStation.objects.create(
                    facility=facility,
                    code=spec.code,
                    name=spec.name.strip(),
                    is_central=spec.is_central,
                    is_active=spec.is_active,
                    created_by=request.user,
                    updated_by=request.user,
                )
                station.doctors.set(doctors)
        except IntegrityError as error:
            raise ValidationError(
                {"code": "This station code is already used"}
            ) from error
        return Response(serialize_station(station), status=201)


class OpdStationDetailView(EmergencyAPIView):
    def patch(self, request, station_id):
        station = _station(station_id)
        if not can("can_update_facility_obj", request.user, station.facility):
            raise PermissionDenied("Only facility admins can set up vitals stations")
        spec = OpdStationUpdateSpec.model_validate(request.data)
        fields = spec.model_dump(exclude_unset=True, exclude={"doctors"})
        try:
            with transaction.atomic():
                for key, value in fields.items():
                    setattr(
                        station, key, value.strip() if isinstance(value, str) else value
                    )
                station.updated_by = request.user
                station.save()
                if spec.doctors is not None:
                    station.doctors.set(_doctors(station.facility, spec.doctors))
        except IntegrityError as error:
            raise ValidationError(
                {"code": "This station code is already used"}
            ) from error
        return Response(serialize_station(station))


class OpdStationNurseView(EmergencyAPIView):
    """OPD02: who is on the station and their status. Any OPD staff member."""

    def put(self, request, station_id):
        station = _station(station_id)
        _require_work(request.user, station.facility)
        spec = OpdStationNurseSpec.model_validate(request.data)
        station.nurse = (
            get_object_or_404(User, external_id=spec.nurse) if spec.nurse else None
        )
        station.nurse_status = spec.nurse_status
        station.updated_by = request.user
        station.save(
            update_fields=["nurse", "nurse_status", "updated_by", "modified_date"]
        )
        return Response(serialize_station(station))


class OpdStationQueueView(EmergencyAPIView):
    """OPD03: today's queue for a station in calling order, the patient at
    the station, and those recently sent on to the doctor."""

    def get(self, request, station_id):
        station = _station(station_id)
        _require_work(request.user, station.facility)
        visits = list(station_visits(station))
        uhids = _uhids([v.encounter.patient for v in visits])
        current = next((v for v in visits if v.stage == OpdStage.at_vitals), None)
        waiting = sort_queue([v for v in visits if v.stage == OpdStage.waiting_vitals])
        done = sorted(
            (v for v in visits if v.vitals_done_at and v.station_id == station.id),
            key=lambda v: v.vitals_done_at,
            reverse=True,
        )[:DONE_SHOWN]
        return Response(
            {
                "station": serialize_station(station),
                "current": serialize_visit(current, request.user, uhids)
                if current
                else None,
                "waiting": [serialize_visit(v, request.user, uhids) for v in waiting],
                "done": [serialize_visit(v, request.user, uhids) for v in done],
            }
        )


class OpdStationCallNextView(EmergencyAPIView):
    def post(self, request, station_id):
        station = _station(station_id)
        _require_work(request.user, station.facility)
        visit = call_next(station, request.user)
        return Response(
            serialize_visit(visit, request.user, _uhids([visit.encounter.patient]))
        )


class OpdVisitView(EmergencyAPIView):
    """A walk-in: Care's encounter create (amb, in progress), which opens the
    visit at the vitals stage through the hmis_opd_visit signal."""

    def post(self, request):
        spec = OpdWalkInSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_create_encounter_obj", request.user, facility):
            raise PermissionDenied("You do not have permission to create encounter")
        patient = get_object_or_404(Patient, external_id=spec.patient)
        doctor = (
            get_object_or_404(User, external_id=spec.doctor) if spec.doctor else None
        )
        with transaction.atomic():
            data = care_view(EncounterViewSet, request, "create").handle_create(
                {
                    "patient": str(patient.external_id),
                    "facility": str(facility.external_id),
                    "organizations": [str(x) for x in spec.organizations],
                    "status": "in_progress",
                    "encounter_class": "amb",
                    "priority": "routine",
                    "period": {"start": timezone.now().isoformat()},
                }
            )
            encounter = Encounter.objects.get(external_id=data["id"])
            visit = open_visit(encounter, request.user, doctor)
            if doctor is not None and visit.doctor_id != doctor.id:
                visit.doctor = doctor
                visit.station = station_for(facility, doctor)
                visit.save(update_fields=["doctor", "station", "modified_date"])
        return Response(
            serialize_visit(visit, request.user, _uhids([patient])), status=201
        )


class OpdVitalsFormView(EmergencyAPIView):
    def get(self, request):
        return Response(opd_vitals.form())


class OpdVitalsView(EmergencyAPIView):
    def post(self, request):
        spec = OpdVitalsSaveSpec.model_validate(request.data)
        visit = get_object_or_404(
            OpdVisit.objects.select_related(
                "encounter__patient", "station", "facility"
            ),
            external_id=spec.visit,
        )
        record, next_visit, created = opd_vitals.save_vitals(request.user, spec, visit)
        visit.refresh_from_db()
        body = {
            **serialize_record(record),
            "visit": serialize_visit(visit, request.user),
            "next": serialize_visit(next_visit, request.user) if next_visit else None,
        }
        return Response(body, status=201 if created else 200)
