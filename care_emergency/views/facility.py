"""A facility's emergency desk number and unit code (Facility columns)."""

from django.http import Http404
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.facility.models import Facility
from care_emergency.serializers import (
    serialize_desk,
    serialize_unit,
)
from care_emergency.specs import (
    DeskWriteSpec,
    UnitWriteSpec,
)
from care_emergency.views.base import (
    EmergencyAPIView,
    can,
    get_facility,
)


class DeskView(EmergencyAPIView):
    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not (
            can("can_create_encounter_obj", request.user, facility)
            or can("can_update_facility_obj", request.user, facility)
        ):
            raise PermissionDenied("You do not have access to this facility")
        if not facility.emergency_desk_phone:
            raise Http404("No emergency desk number for this facility")
        return Response(serialize_desk(facility))

    def put(self, request):
        spec = DeskWriteSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_update_facility_obj", request.user, facility):
            raise PermissionDenied("Only facility admins can set the desk number")
        facility.emergency_desk_phone = spec.phone_number
        facility.save(update_fields=["emergency_desk_phone", "modified_date"])
        return Response(serialize_desk(facility))


class UnitView(EmergencyAPIView):
    def get(self, request):
        facility = get_facility(request.query_params.get("facility"))
        if not (
            can("can_create_encounter_obj", request.user, facility)
            or can("can_update_facility_obj", request.user, facility)
        ):
            raise PermissionDenied("You do not have access to this facility")
        if not facility.unit_code:
            raise Http404("No unit code for this facility")
        return Response(serialize_unit(facility))

    def put(self, request):
        spec = UnitWriteSpec.model_validate(request.data)
        facility = get_facility(spec.facility)
        if not can("can_update_facility_obj", request.user, facility):
            raise PermissionDenied("Only facility admins can set the unit code")
        if facility.unit_code and facility.unit_code != spec.code:
            # Issued numbers carry the code, so changing it would split the unit's series.
            raise ValidationError("The unit code can't be changed once set")
        if (
            Facility.objects.filter(unit_code=spec.code)
            .exclude(id=facility.id)
            .exists()
        ):
            raise ValidationError("Another facility already uses this unit code")
        if not facility.unit_code:
            facility.unit_code = spec.code
            facility.save(update_fields=["unit_code", "modified_date"])
        return Response(serialize_unit(facility))
