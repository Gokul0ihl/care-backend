"""care_emergency's API views, one module per area. The names are
re-exported here so `from care_emergency.views import X` keeps working."""

from care_emergency.views.api_keys import (
    ApiKeyRevokeView,
    ApiKeyView,
    check_key_admin,
    serialize_api_key,
    username_error,
)
from care_emergency.views.app import (
    AppPatientDetailView,
    AppPatientView,
    abha_matches,
    app_patient_body,
    serialize_app_patient,
)
from care_emergency.views.base import (
    EmergencyAPIView,
    can,
    can_on_resource,
    care_view,
    get_facility,
)
from care_emergency.views.documents import (
    DocumentView,
    StagedDocumentView,
)
from care_emergency.views.emergency import (
    ConvertView,
    EmergencyEncounterFilters,
    EmergencyEncounterViewSet,
    RegisterView,
)
from care_emergency.views.facility import (
    DeskView,
    UnitView,
)
from care_emergency.views.lab import (
    LabTariffView,
    LabTestDetailView,
    LabTestView,
    can_read_lab_catalogue,
)
from care_emergency.views.newborn import (
    NewbornRegistrationView,
)
from care_emergency.views.patient_contact import (
    PatientContactView,
)
from care_emergency.views.patient_merge import (
    PatientMergeView,
)
from care_emergency.views.patient_numbers import (
    PatientNumberSearchView,
    PatientNumberView,
    PatientSummaryView,
    issue_number_blocked,
)
from care_emergency.views.pincodes import (
    PincodeView,
    pincode_response,
)
from care_emergency.views.quick_registration import (
    QUICK_PATIENT_RELATED,
    PatientQuickRegistrationView,
    QuickFormLinkView,
    QuickFormPincodeView,
    QuickFormThrottle,
    QuickFormView,
    QuickRegistrationView,
    quick_form_link,
    quick_patients,
    send_quick_form_link,
)
from care_emergency.views.registration import (
    DESK_PATIENT_RELATED,
    PatientRegistrationView,
    RegistrationView,
    desk_patient,
    registration_columns,
    save_desk_registration,
)
from care_emergency.views.restricted import (
    BreakGlassView,
    RestrictionView,
    StatutoryFlagView,
)

__all__ = [
    "care_view",
    "get_facility",
    "can",
    "can_on_resource",
    "EmergencyAPIView",
    "DeskView",
    "UnitView",
    "PatientNumberView",
    "issue_number_blocked",
    "PatientNumberSearchView",
    "PatientSummaryView",
    "RegisterView",
    "EmergencyEncounterFilters",
    "EmergencyEncounterViewSet",
    "ConvertView",
    "registration_columns",
    "DESK_PATIENT_RELATED",
    "save_desk_registration",
    "desk_patient",
    "RegistrationView",
    "PatientRegistrationView",
    "NewbornRegistrationView",
    "QUICK_PATIENT_RELATED",
    "quick_patients",
    "quick_form_link",
    "send_quick_form_link",
    "QuickRegistrationView",
    "PatientQuickRegistrationView",
    "QuickFormLinkView",
    "QuickFormThrottle",
    "QuickFormView",
    "QuickFormPincodeView",
    "PatientMergeView",
    "PatientContactView",
    "DocumentView",
    "StagedDocumentView",
    "RestrictionView",
    "StatutoryFlagView",
    "BreakGlassView",
    "pincode_response",
    "PincodeView",
    "can_read_lab_catalogue",
    "LabTestView",
    "LabTestDetailView",
    "LabTariffView",
    "serialize_api_key",
    "username_error",
    "check_key_admin",
    "ApiKeyView",
    "ApiKeyRevokeView",
    "abha_matches",
    "app_patient_body",
    "serialize_app_patient",
    "AppPatientView",
    "AppPatientDetailView",
]
