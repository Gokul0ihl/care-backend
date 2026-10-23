"""Desk documents."""

from django.conf import settings
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response

from care.emr.api.viewsets.file_upload import file_authorizer
from care.emr.models import Patient
from care.security.authorization import AuthorizationController
from care.utils.shortcuts import get_object_or_404
from care_emergency.constants import (
    DOCUMENT_MIME_TYPES,
)
from care_emergency.models import (
    PatientDocument,
)
from care_emergency.restricted import can_see_identity, is_restricted
from care_emergency.serializers import (
    serialize_document,
)
from care_emergency.services import (
    store_document,
)
from care_emergency.specs import (
    DocumentUploadSpec,
)
from care_emergency.views.base import (
    EmergencyAPIView,
)


class DocumentView(EmergencyAPIView):
    """Desk documents (D37, D43). POST is multipart: file, category, side,
    capture_method and, for an existing patient, patient. Without a patient
    the document is staged until a registration uses it."""

    parser_classes = [MultiPartParser]

    def get(self, request):
        patient = get_object_or_404(
            Patient, external_id=request.query_params.get("patient")
        )
        file_authorizer(request.user, "patient", str(patient.external_id), "read")
        if is_restricted(patient) and not can_see_identity(request.user, patient):
            raise PermissionDenied("This patient's documents are restricted")
        documents = PatientDocument.objects.filter(patient=patient).select_related(
            "file", "created_by"
        )
        return Response(
            [serialize_document(x) for x in documents.order_by("-created_date")]
        )

    def post(self, request):
        upload = request.FILES.get("file")
        if upload is None:
            raise ValidationError("Choose a file")
        # Blank optional multipart fields are left out so their defaults apply.
        spec = DocumentUploadSpec.model_validate(
            {
                key: request.data[key]
                for key in DocumentUploadSpec.model_fields
                if request.data.get(key) not in (None, "")
            }
        )
        if upload.size > settings.MAX_FILE_UPLOAD_SIZE * 1024 * 1024:
            err = f"Files can be up to {settings.MAX_FILE_UPLOAD_SIZE} MB"
            raise ValidationError(err)
        if upload.content_type not in DOCUMENT_MIME_TYPES:
            raise ValidationError("Upload a JPEG, PNG, WebP or PDF")
        patient = None
        if spec.patient:
            patient = get_object_or_404(Patient, external_id=spec.patient)
            file_authorizer(request.user, "patient", str(spec.patient), "write")
        elif not AuthorizationController.call("can_create_patient", request.user):
            raise PermissionDenied("You do not have permission to register patients")
        document = store_document(
            request.user,
            upload,
            spec.category,
            spec.side,
            spec.capture_method,
            patient,
            spec.source,
            spec.document_date,
        )
        return Response(serialize_document(document))


class StagedDocumentView(EmergencyAPIView):
    def delete(self, request, document_id):
        document = get_object_or_404(
            PatientDocument,
            external_id=document_id,
            patient__isnull=True,
            created_by=request.user,
        )
        file = document.file
        document.delete()
        file.files_manager.delete_object(file, quiet=True)
        file.delete()
        return Response(status=204)
