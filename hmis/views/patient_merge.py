"""Merging duplicate patient records."""

from django.db import transaction
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from care.emr.locks.billing import PatientCreateLock
from care.emr.models import Patient
from care.emr.resources.patient.spec import PatientRetrieveSpec
from care.utils.lock import ObjectLocked
from care.utils.shortcuts import get_object_or_404
from hmis.constants import DocumentCategories
from hmis.merge import (
    can_merge,
    merge_patients,
    merge_preview,
)
from hmis.services import attach_documents, staged_documents
from hmis.specs import (
    PatientMergeSpec,
)
from hmis.views.base import (
    EmergencyAPIView,
)


class PatientMergeView(EmergencyAPIView):
    """Merge a duplicate (source) into the record that is kept (target).
    GET previews it; POST merges. Both need merge authority (can_merge)."""

    def get_pair(self, source_id, target_id, lock=False):
        for value in (source_id, target_id):
            if not value:
                raise ValidationError("source and target are required")
        patients = Patient.objects.all()
        if lock:
            # A stable order, so two merges of the same pair can't deadlock.
            patients = patients.select_for_update().order_by("id")
            list(patients.filter(external_id__in=[source_id, target_id]))
        source = get_object_or_404(patients, external_id=source_id)
        target = get_object_or_404(patients, external_id=target_id)
        if not can_merge(self.request.user, source, target):
            raise PermissionDenied("You do not have permission to merge these records")
        return source, target

    def get(self, request):
        source, target = self.get_pair(
            request.query_params.get("source"), request.query_params.get("target")
        )
        return Response(merge_preview(source, target))

    def post(self, request):
        spec = PatientMergeSpec.model_validate(request.data)
        lock = PatientCreateLock()
        try:
            lock.acquire()
        except ObjectLocked as e:
            raise ValidationError(
                "Another registration is being saved, try again in a moment"
            ) from e
        try:
            with transaction.atomic():
                source, target = self.get_pair(spec.source, spec.target, lock=True)
                consent = staged_consent(request.user, spec.consent_document)
                merge = merge_patients(
                    source, target, spec.choices, spec.reason, request.user
                )
                # Filed after the merge, so it isn't counted among the moved records.
                attach_documents([consent], target)
                merge.consent_document = consent
                merge.save(update_fields=["consent_document"])
                transaction.on_commit(lock.release)
        except BaseException:
            lock.release()
            raise
        target.refresh_from_db()
        return Response(
            {
                "patient": PatientRetrieveSpec.serialize(target).to_json(),
                "moved": merge.moved,
            }
        )


def staged_consent(user, document_id):
    """The signed merge consent this user uploaded for the merge, not yet filed on a patient."""
    try:
        (document,) = staged_documents(user, [document_id])
    except ValidationError as e:
        raise ValidationError("Upload the signed merge consent first") from e
    if document.category != DocumentCategories.merge_consent:
        raise ValidationError("Upload the signed merge consent first")
    return document
