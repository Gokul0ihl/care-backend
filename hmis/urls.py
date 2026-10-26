from django.urls import include, path
from rest_framework.routers import SimpleRouter

from hmis.views import (
    ApiKeyRevokeView,
    ApiKeyView,
    AppPatientDetailView,
    AppPatientView,
    BreakGlassView,
    ConvertView,
    DeskView,
    DocumentView,
    EmergencyEncounterViewSet,
    LabTariffView,
    LabTestDetailView,
    LabTestView,
    PatientNumberView,
    PatientNumberSearchView,
    PatientContactView,
    PatientMergeView,
    PatientSummaryView,
    NewbornRegistrationView,
    PatientRegistrationView,
    PincodeView,
    PatientQuickRegistrationView,
    QuickFormLinkView,
    QuickFormPincodeView,
    QuickFormView,
    QuickRegistrationView,
    RegisterView,
    RegistrationView,
    RestrictionView,
    StagedDocumentView,
    StatutoryFlagView,
    UnitView,
)

router = SimpleRouter()
router.register(
    "emergency/encounters", EmergencyEncounterViewSet, basename="emergency-encounter"
)

urlpatterns = [
    path("mobile/", include("hmis.urls_mobile")),
    path("app/patients/", AppPatientView.as_view(), name="app-patients"),
    path(
        "app/patients/<str:app_user_id>/",
        AppPatientDetailView.as_view(),
        name="app-patient",
    ),
    path("api-keys/", ApiKeyView.as_view(), name="api-keys"),
    path(
        "api-keys/<uuid:key_id>/revoke/",
        ApiKeyRevokeView.as_view(),
        name="api-key-revoke",
    ),
    path(
        "patients/search/",
        PatientNumberSearchView.as_view(),
        name="patient-number-search",
    ),
    path(
        "patients/summary/",
        PatientSummaryView.as_view(),
        name="patient-summary",
    ),
    path("patients/merge/", PatientMergeView.as_view(), name="patient-merge"),
    path(
        "patients/<uuid:patient_id>/contact/",
        PatientContactView.as_view(),
        name="patient-contact",
    ),
    path("emergency/desk/", DeskView.as_view(), name="emergency-desk"),
    path("lab-tests/", LabTestView.as_view(), name="lab-tests"),
    path("lab-tests/tariff/", LabTariffView.as_view(), name="lab-tariff"),
    path("lab-tests/<str:code>/", LabTestDetailView.as_view(), name="lab-test"),
    path("facility/unit/", UnitView.as_view(), name="facility-unit"),
    path(
        "patients/<uuid:patient_id>/uhid/",
        PatientNumberView.as_view(),
        name="patient-uhid",
    ),
    path("emergency/register/", RegisterView.as_view(), name="emergency-register"),
    path(
        "patients/<uuid:patient_id>/convert/",
        ConvertView.as_view(),
        name="emergency-convert",
    ),
    path("registration/", RegistrationView.as_view(), name="desk-registration"),
    path(
        "registration/newborn/",
        NewbornRegistrationView.as_view(),
        name="newborn-register",
    ),
    path(
        "registration/<uuid:patient_id>/",
        PatientRegistrationView.as_view(),
        name="desk-registration-detail",
    ),
    path(
        "registration/quick/",
        QuickRegistrationView.as_view(),
        name="quick-registration",
    ),
    path(
        "registration/quick/form/<str:token>/",
        QuickFormView.as_view(),
        name="quick-registration-form",
    ),
    path(
        "registration/quick/form/<str:token>/pincode/<str:pincode>/",
        QuickFormPincodeView.as_view(),
        name="quick-registration-form-pincode",
    ),
    path(
        "registration/quick/<uuid:patient_id>/",
        PatientQuickRegistrationView.as_view(),
        name="quick-registration-detail",
    ),
    path(
        "registration/quick/<uuid:patient_id>/send-link/",
        QuickFormLinkView.as_view(),
        name="quick-registration-send-link",
    ),
    path("pincodes/<str:pincode>/", PincodeView.as_view(), name="pincode"),
    path("documents/", DocumentView.as_view(), name="desk-documents"),
    path(
        "documents/<uuid:document_id>/",
        StagedDocumentView.as_view(),
        name="desk-staged-document",
    ),
    path(
        "patients/<uuid:patient_id>/restriction/",
        RestrictionView.as_view(),
        name="patient-restriction",
    ),
    path(
        "patients/<uuid:patient_id>/restriction/flags/",
        StatutoryFlagView.as_view(),
        name="patient-statutory-flags",
    ),
    path(
        "patients/<uuid:patient_id>/restriction/break-glass/",
        BreakGlassView.as_view(),
        name="patient-break-glass",
    ),
    *router.urls,
]
