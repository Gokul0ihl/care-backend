"""The patient mobile app's routes, at /api/hmis/mobile/. They keep the app's
own auth paths and envelope. Appointment routes use the HMIS UUID contract
documented in docs/mobile-appointments.md in the frontend repo. A trailing
slash is accepted too."""

from django.urls import re_path

from hmis.views.mobile import (
    FcmTokenView,
    LogoutView,
    MeView,
    RefreshView,
    SendOtpView,
    VerifyOtpView,
)
from hmis.views.mobile_appointments import (
    MobileAppointmentDetailView,
    MobileAppointmentDoctorsView,
    MobileAppointmentFacilitiesView,
    MobileAppointmentListCreateView,
    MobileAppointmentSlotsView,
)


def route(path, view, name):
    return re_path(rf"^api/v1/{path}/?$", view.as_view(), name=name)


urlpatterns = [
    route(
        "appointments/facilities",
        MobileAppointmentFacilitiesView,
        "mobile-appointment-facilities",
    ),
    route(
        r"appointments/facilities/(?P<facility_id>[0-9a-f-]{36})/doctors",
        MobileAppointmentDoctorsView,
        "mobile-appointment-doctors",
    ),
    route(
        r"appointments/facilities/(?P<facility_id>[0-9a-f-]{36})/doctors/(?P<doctor_id>[0-9a-f-]{36})/slots",
        MobileAppointmentSlotsView,
        "mobile-appointment-slots",
    ),
    route("appointments", MobileAppointmentListCreateView, "mobile-appointments"),
    route(
        r"appointments/(?P<booking_id>[0-9a-f-]{36})",
        MobileAppointmentDetailView,
        "mobile-appointment-detail",
    ),
    route("auth/login/send_otp", SendOtpView, "mobile-send-otp"),
    route("auth/login/verify_otp", VerifyOtpView, "mobile-verify-otp"),
    route("auth/refresh", RefreshView, "mobile-refresh"),
    route("auth/logout", LogoutView, "mobile-logout"),
    route("users/me", MeView, "mobile-me"),
    route("users/me/fcm-token", FcmTokenView, "mobile-fcm-token"),
]
