"""The patient mobile app's routes, at /api/hmis/mobile/. They keep the app's
own paths (api/v1/..., no trailing slash), so the app only changes its base
URL. A trailing slash is accepted too."""

from django.urls import re_path

from hmis.views.mobile import (
    FcmTokenView,
    LogoutView,
    MeView,
    RefreshView,
    SendOtpView,
    VerifyOtpView,
)


def route(path, view, name):
    return re_path(rf"^api/v1/{path}/?$", view.as_view(), name=name)


urlpatterns = [
    route("auth/login/send_otp", SendOtpView, "mobile-send-otp"),
    route("auth/login/verify_otp", VerifyOtpView, "mobile-verify-otp"),
    route("auth/refresh", RefreshView, "mobile-refresh"),
    route("auth/logout", LogoutView, "mobile-logout"),
    route("users/me", MeView, "mobile-me"),
    route("users/me/fcm-token", FcmTokenView, "mobile-fcm-token"),
]
