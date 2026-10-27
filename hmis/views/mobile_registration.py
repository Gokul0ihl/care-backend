"""Public OTP-proof registration; protected self-profile updates live at users/me."""

from rest_framework import status

from hmis.mobile_registration import (
    RegistrationFields,
    complete_registration,
    registration_facilities,
    validated,
)
from hmis.views.mobile import PublicMobileView, app_user, ok, phone_from
from hmis.views.mobile_appointments import CatalogueQuery, page_data


class RegistrationFacilitiesView(PublicMobileView):
    def get(self, request):
        params = validated(CatalogueQuery, request.query_params.dict())
        query = registration_facilities()
        if params.get("search"):
            query = query.filter(name__icontains=params["search"])
        return ok(
            page_data(
                query.order_by("name", "id"),
                params,
                lambda facility: {
                    "id": str(facility.external_id),
                    "name": facility.name,
                    "address": facility.address,
                },
            )
        )


class CompleteRegistrationView(PublicMobileView):
    def post(self, request):
        phone = phone_from(request)
        data = validated(RegistrationFields, request.data)
        patient, account, _session, tokens = complete_registration(data, phone)
        return ok(
            {**tokens, "user": app_user(patient, account.id)},
            "Registration completed",
            status.HTTP_201_CREATED,
        )
