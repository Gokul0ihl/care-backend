"""Shared helpers for the care_emergency views."""

from rest_framework.exceptions import ValidationError
from rest_framework.views import APIView

from care.emr.api.viewsets.base import emr_exception_handler
from care.facility.models import Facility
from care.security.authorization import AuthorizationController
from care.utils.shortcuts import get_object_or_404


def care_view(viewset_class, request, action, **kwargs):
    """A Care viewset bound to this request, so its create/update logic
    (validation, authorization, locks, identifiers) runs unchanged."""
    return viewset_class(
        request=request, kwargs=kwargs, action=action, format_kwarg=None
    )


def get_facility(external_id):
    if not external_id:
        raise ValidationError("facility is required")
    return get_object_or_404(Facility, external_id=external_id)


def can(permission, user, obj):
    return AuthorizationController.call(permission, user, obj)


def can_on_resource(permission, user, resource):
    """Care's schedule and booking checks take the resource first."""
    return AuthorizationController.call(permission, resource, user)


class EmergencyAPIView(APIView):
    def get_exception_handler(self):
        return emr_exception_handler
