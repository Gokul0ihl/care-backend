"""Emergency registration, the emergency encounters and conversion."""

from django.db import transaction
from django_filters import rest_framework as filters
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.viewsets import GenericViewSet

from care.emr.api.viewsets.base import emr_exception_handler
from care.emr.api.viewsets.encounter import EncounterViewSet
from care.emr.api.viewsets.patient import PatientViewSet
from care.emr.models import Encounter, Patient
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.security.authorization import AuthorizationController
from care.utils.shortcuts import get_object_or_404
from care.utils.time_util import care_now
from care_emergency.constants import (
    TRIAGE_PRIORITY,
    PatientNumberSeries,
)
from care_emergency.serializers import (
    serialize_emergency,
)
from care_emergency.services import (
    allocate_mlc_number,
    allocate_patient_number,
    attach_patient_number,
    emergency_desk_phone,
    emergency_encounters,
    is_emergency_desk_phone,
    issue_patient_number,
    unit_code,
)
from care_emergency.specs import (
    EmergencyRegisterSpec,
    EmergencyUpdateSpec,
    MedicoLegalMixin,
)
from care_emergency.views.base import (
    EmergencyAPIView,
    can,
    care_view,
    get_facility,
)


class RegisterView(EmergencyAPIView):
    """Creates the patient and the emergency encounter (Care's create, then
    the emergency columns) in one transaction, so a failure never leaves an
    orphan patient."""

    def post(self, request):
        spec = EmergencyRegisterSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_create_encounter_obj", request.user, facility):
            raise PermissionDenied("You do not have permission to create encounter")
        desk_phone = emergency_desk_phone(facility)
        unit_code(facility)

        with transaction.atomic():
            patient_body = {**spec.patient, "phone_number": desk_phone}
            unknown_number = None
            if spec.is_unidentified:
                # Staff see unidentified patients as "Unknown <n>", n being the
                # day's sequence in UNK-<unit>-<yyyymmdd>-<nnn>.
                unknown_number, sequence = allocate_patient_number(
                    PatientNumberSeries.unknown, facility
                )
                patient_body["name"] = f"Unknown {sequence}"
            patient_data = care_view(PatientViewSet, request, "create").handle_create(
                patient_body
            )
            patient = Patient.objects.get(external_id=patient_data["id"])
            if unknown_number:
                # Before the encounter, so arrival doesn't issue a UHID.
                attach_patient_number(
                    patient, PatientNumberSeries.unknown, unknown_number
                )
            encounter_data = care_view(
                EncounterViewSet, request, "create"
            ).handle_create(
                {
                    "patient": str(patient.external_id),
                    "facility": str(facility.external_id),
                    "organizations": [str(x) for x in spec.organizations],
                    "status": "in_progress",
                    "encounter_class": "emer",
                    "priority": TRIAGE_PRIORITY[spec.triage],
                    "hospitalization": {"admit_source": "emd"},
                    "period": {"start": care_now().isoformat()},
                }
            )
            encounter = Encounter.objects.select_related(
                "patient", "facility", "created_by"
            ).get(external_id=encounter_data["id"])
            brought_by = spec.brought_by
            columns = {
                "triage": spec.triage,
                "is_medico_legal": spec.is_medico_legal,
                "mlc_nature": spec.mlc_nature,
                "police_station": spec.police_station.strip(),
                "distinguishing_marks": spec.distinguishing_marks.strip(),
                "chief_complaint": spec.chief_complaint,
                "mode_of_arrival": spec.mode_of_arrival,
                "is_unidentified": spec.is_unidentified,
                "brought_by_name": brought_by.name.strip() if brought_by else "",
                "brought_by_phone": (
                    (brought_by.phone_number or "") if brought_by else ""
                ),
                "brought_by_relationship": (
                    brought_by.relationship if brought_by else ""
                ),
                "mlc_number": (
                    allocate_mlc_number(facility) if spec.is_medico_legal else None
                ),
            }
            for field, value in columns.items():
                setattr(encounter, field, value)
            encounter.save(update_fields=list(columns))
            patient.refresh_from_db()
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()

        return Response(
            {
                "patient": patient_data,
                "encounter": encounter_data,
                "emergency": serialize_emergency(encounter),
            }
        )


class EmergencyEncounterFilters(filters.FilterSet):
    triage = filters.CharFilter(field_name="triage")
    is_medico_legal = filters.BooleanFilter(field_name="is_medico_legal")
    is_unidentified = filters.BooleanFilter(field_name="is_unidentified")
    mlc_number = filters.CharFilter(field_name="mlc_number", lookup_expr="iexact")
    chief_complaint = filters.CharFilter(field_name="chief_complaint")
    mode_of_arrival = filters.CharFilter(field_name="mode_of_arrival")
    patient = filters.UUIDFilter(field_name="patient__external_id")


class EmergencyEncounterViewSet(MedicoLegalMixin, GenericViewSet):
    """Emergency encounters: Care encounters with triage set."""

    lookup_field = "external_id"
    lookup_url_kwarg = "external_id"
    filter_backends = [filters.DjangoFilterBackend]
    filterset_class = EmergencyEncounterFilters

    def get_exception_handler(self):
        return emr_exception_handler

    def get_queryset(self):
        qs = (
            emergency_encounters()
            .select_related("patient", "facility", "created_by")
            .order_by("-created_date")
        )
        if self.action == "list":
            facility = get_facility(self.request.query_params.get("facility"))
            encounters = AuthorizationController.call(
                "get_filtered_encounters",
                Encounter.objects.all(),
                self.request.user,
                facility,
            )
            # serialize_emergency reads only Care's own patient columns.
            return qs.filter(facility=facility, id__in=encounters.values("id")).defer(
                *[f"patient__{x}" for x in Patient.list_deferred_fields()]
            )
        return qs

    def list(self, request, *args, **kwargs):
        page = self.paginate_queryset(self.filter_queryset(self.get_queryset()))
        return self.get_paginated_response([serialize_emergency(x) for x in page])

    def retrieve(self, request, *args, **kwargs):
        record = self.get_object()
        if not (
            can("can_view_encounter_obj", request.user, record)
            or can("can_view_patient_obj", request.user, record.patient)
        ):
            raise PermissionDenied("You do not have permission to view this encounter")
        return Response(serialize_emergency(record))

    def partial_update(self, request, *args, **kwargs):
        record = self.get_object()
        if not can("can_update_encounter_obj", request.user, record):
            raise PermissionDenied("You do not have permission to update encounter")
        spec = EmergencyUpdateSpec.model_validate(request.data)
        changed = {"mlc_nature", "police_station", "mlc_number", "updated_by"}
        for field in spec.model_fields_set - {"brought_by"}:
            value = getattr(spec, field)
            if value is None and field != "is_medico_legal":
                continue
            setattr(record, field, value.strip() if isinstance(value, str) else value)
            changed.add(field)
        if "brought_by" in spec.model_fields_set:
            changed |= {
                "brought_by_name",
                "brought_by_phone",
                "brought_by_relationship",
            }
            record.brought_by_name = (
                spec.brought_by.name.strip() if spec.brought_by else ""
            )
            record.brought_by_phone = (
                (spec.brought_by.phone_number or "") if spec.brought_by else ""
            )
            record.brought_by_relationship = (
                spec.brought_by.relationship if spec.brought_by else ""
            )
        try:
            self.check_medico_legal(
                record.is_medico_legal, record.mlc_nature, record.police_station
            )
        except ValueError as e:
            raise ValidationError(str(e)) from e
        if not record.is_medico_legal:
            record.mlc_nature = ""
            record.police_station = ""
        elif not record.mlc_number:
            record.mlc_number = allocate_mlc_number(record.facility)
        # An issued MLC number is kept even if the case is later marked
        # non-medico-legal, so numbers are never reused.
        record.updated_by = request.user
        # Only the emergency columns, so a concurrent edit of the encounter
        # through Care isn't overwritten.
        record.save(update_fields=[*changed, "modified_date"])
        return Response(serialize_emergency(record))


class ConvertView(EmergencyAPIView):
    """Turns an emergency registration into a normal patient: updates the
    patient through Care's own update path and replaces the desk number."""

    def post(self, request, patient_id):
        patient = get_object_or_404(Patient, external_id=patient_id)
        records = emergency_encounters().filter(
            patient=patient, converted_at__isnull=True
        )
        if not records.exists():
            raise ValidationError(
                "This patient has no emergency registration to convert"
            )
        if not request.data.get("phone_number"):
            raise ValidationError("Enter the patient's own phone number")

        with transaction.atomic():
            view = care_view(
                PatientViewSet, request, "update", external_id=str(patient_id)
            )
            patient_data = view.handle_update(patient, request.data)
            patient.refresh_from_db(fields=["phone_number"])
            if is_emergency_desk_phone(patient.phone_number):
                raise ValidationError(
                    "Use the patient's own number, not the emergency desk number"
                )
            facility = records.order_by("-created_date").first().facility
            unit_code(facility)
            records.update(
                converted_at=care_now(),
                converted_by=request.user,
                is_unidentified=False,
                updated_by=request.user,
            )
            # The UNK number stays on the record alongside the new UHID.
            issue_patient_number(patient, facility)
            patient.refresh_from_db()
            patient_data = PatientRetrieveSpec.serialize(patient).to_json()
        return Response(
            {
                "patient": patient_data,
                "emergency": [
                    serialize_emergency(x)
                    for x in emergency_encounters()
                    .filter(patient=patient)
                    .select_related("patient", "facility", "created_by")
                ],
            }
        )
