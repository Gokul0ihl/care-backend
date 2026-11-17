from django.db import models

from care.utils.models.base import BaseModel


class AccessRequestStatus(models.TextChoices):
    pending = "pending"
    approved = "approved"
    rejected = "rejected"
    cancelled = "cancelled"


class AccessRequest(BaseModel):
    """
    Someone asks for a role in a facility department, for themselves or (as a
    facility admin) for a colleague. A super admin approves it, which creates
    the FacilityOrganizationUser, or rejects it.
    """

    user = models.ForeignKey(
        "users.User", on_delete=models.CASCADE, related_name="access_requests"
    )
    requested_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, related_name="+"
    )
    role = models.ForeignKey("security.RoleModel", on_delete=models.CASCADE)
    facility_organization = models.ForeignKey(
        "emr.FacilityOrganization", on_delete=models.CASCADE, related_name="+"
    )
    expires_at = models.DateTimeField(null=True, blank=True)
    reason = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=AccessRequestStatus.choices,
        default=AccessRequestStatus.pending,
        db_index=True,
    )
    reviewed_by = models.ForeignKey(
        "users.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_comment = models.TextField(blank=True, default="")
    # The role assignment created when the request was approved.
    assignment = models.ForeignKey(
        "emr.FacilityOrganizationUser",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    class Meta:
        ordering = ["-created_date"]
