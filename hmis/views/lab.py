"""The facility's lab test catalogue."""

from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from care.emr.models import ActivityDefinition
from care.utils.shortcuts import get_object_or_404
from hmis.lab_catalogue import (
    LabTestCreateSpec,
    LabTestWriteSpec,
    check_codes,
    facility_slug,
    lab_slug,
    lab_tariff,
    lab_test_row,
    lab_tests,
    save_lab_test,
)
from hmis.views.base import (
    EmergencyAPIView,
    can,
    get_facility,
)


def can_read_lab_catalogue(user, facility):
    return (
        can("can_list_facility_activity_definition", user, facility)
        or can("can_create_encounter_obj", user, facility)
        or can("can_update_facility_obj", user, facility)
    )


class LabTestView(EmergencyAPIView):
    """The facility's lab test catalogue (Settings → Lab tests, and the front desk's
    lab booking). ?status=active lists only the tests that can be booked."""

    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not can_read_lab_catalogue(request.user, facility):
            raise PermissionDenied("You do not have access to this facility")
        status_filter = request.query_params.get("status")
        return Response({"results": lab_tests(facility, status_filter)})

    def post(self, request):
        spec = LabTestCreateSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_write_facility_activity_definition", request.user, facility):
            raise PermissionDenied("Only facility admins can edit lab tests")
        check_codes(facility, spec)
        activity = save_lab_test(facility, spec, request.user)
        return Response(
            lab_test_row(facility, activity), status=status.HTTP_201_CREATED
        )


class LabTestDetailView(EmergencyAPIView):
    def put(self, request, code):
        spec = LabTestWriteSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_write_facility_activity_definition", request.user, facility):
            raise PermissionDenied("Only facility admins can edit lab tests")
        activity = get_object_or_404(
            ActivityDefinition,
            facility=facility,
            slug=facility_slug(facility, lab_slug(code)),
        )
        check_codes(facility, spec, activity)
        save_lab_test(facility, spec, request.user, activity)
        return Response(lab_test_row(facility, activity))


class LabTariffView(EmergencyAPIView):
    """Tariff rows (H2 diagnostics) a lab test can be priced from."""

    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not can("can_write_facility_activity_definition", request.user, facility):
            raise PermissionDenied("Only facility admins can edit lab tests")
        return Response({"results": lab_tariff(facility)})
