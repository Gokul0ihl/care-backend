"""POH's API views, one module per area. The names are
re-exported here so `from hmis.views import X` keeps working."""

from hmis.views.api_keys import (
    ApiKeyRevokeView,
    ApiKeyView,
    check_key_admin,
    serialize_api_key,
    username_error,
)
from hmis.views.app import (
    AppPatientDetailView,
    AppPatientView,
    abha_matches,
    app_patient_body,
    serialize_app_patient,
)
from hmis.views.base import (
    EmergencyAPIView,
    can,
    can_on_resource,
    care_view,
    get_facility,
)
from hmis.views.documents import (
    DocumentView,
    StagedDocumentView,
)
from hmis.views.emergency import (
    ConvertView,
    EmergencyEncounterFilters,
    EmergencyEncounterViewSet,
    RegisterView,
)
from hmis.views.facility import (
    DeskView,
    UnitView,
)
from hmis.views.lab import (
    LabTariffView,
    LabTestDetailView,
    LabTestView,
    can_read_lab_catalogue,
)
from hmis.views.newborn import (
    NewbornRegistrationView,
)
from hmis.views.patient_contact import (
    PatientContactView,
)
from hmis.views.patient_merge import (
    PatientMergeView,
)
from hmis.views.patient_numbers import (
    PatientNumberSearchView,
    PatientNumberView,
    PatientSummaryView,
    issue_number_blocked,
)
from hmis.views.pincodes import (
    PincodeView,
    pincode_response,
)
from hmis.views.quick_registration import (
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
from hmis.views.registration import (
    DESK_PATIENT_RELATED,
    PatientRegistrationView,
    RegistrationView,
    desk_patient,
    registration_columns,
    save_desk_registration,
)
from hmis.views.restricted import (
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
