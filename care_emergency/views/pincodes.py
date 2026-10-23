"""PIN code directory lookup."""

import re

from rest_framework import status
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from care_emergency.models import (
    PostOffice,
)
from care_emergency.pincodes import lookup as lookup_pincode
from care_emergency.views.base import (
    EmergencyAPIView,
)


def pincode_response(pincode):
    if not re.fullmatch(r"[1-9]\d{5}", pincode):
        raise ValidationError("Enter a 6-digit PIN code.")
    place = lookup_pincode(pincode)
    if place is None:
        loaded = PostOffice.objects.exists()
        detail = (
            "PIN code not found." if loaded else "The PIN directory hasn't been loaded."
        )
        return Response(
            {"detail": detail, "directory_loaded": loaded},
            status=status.HTTP_404_NOT_FOUND,
        )
    return Response(place)


class PincodeView(EmergencyAPIView):
    """India Post's district, state and post offices for a PIN (D17, M01).
    404 when the PIN isn't listed or the directory isn't loaded yet; callers
    fall back to a live lookup either way."""

    def get(self, request, pincode):
        return pincode_response(pincode)
