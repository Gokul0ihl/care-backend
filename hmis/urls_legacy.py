"""The patient app server's endpoints under their old /api/care_emergency/
prefix, until that server calls /api/hmis/. Other clients use hmis.urls."""

from django.urls import path

from hmis.views import AppPatientDetailView, AppPatientView, PatientNumberView

urlpatterns = [
    path("app/patients/", AppPatientView.as_view(), name="legacy-app-patients"),
    path(
        "app/patients/<str:app_user_id>/",
        AppPatientDetailView.as_view(),
        name="legacy-app-patient",
    ),
    path(
        "patients/<uuid:patient_id>/uhid/",
        PatientNumberView.as_view(),
        name="legacy-patient-uhid",
    ),
]
