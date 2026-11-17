"""Phase 4 tools on /api/v1/access_admin/ for super admins:

bulk_import/            add many users (and a first role each) from a CSV file
bulk_import_template/   the CSV header plus one example row
bulk_assign/            give or remove one role for many users at once
export_users/           users and their roles as CSV
export_audit/           the audit log as CSV
matrix/                 permissions granted per role, by permission context
security_policy/        read and change the organisation-wide security policy
"""

import csv
import datetime
import io
import logging
import uuid

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from pydantic import UUID4, BaseModel, Field
from pydantic import ValidationError as PydanticError
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from care.emr.api.viewsets.user_access import user_role_assignments
from care.emr.models.organization import FacilityOrganization, FacilityOrganizationUser
from care.emr.resources.user.spec import UserCreateSpec
from care.facility.models import Facility
from care.security.access_audit import assignment_value, record_access_event
from care.security.models import (
    AccessAuditAction,
    AccessAuditEvent,
    PermissionModel,
    RoleModel,
    RolePermission,
    SecurityPolicy,
)
from care.security.policy import default_expiry, forget_cached_policy
from care.users.models import User

logger = logging.getLogger(__name__)

IMPORT_COLUMNS = [
    "first_name",
    "last_name",
    "username",
    "email",
    "password",
    "phone_number",
    "gender",
    "facility",
    "department",
    "role",
    "expires_on",
]
IMPORT_EXAMPLE = [
    "Neha",
    "Kulkarni",
    "dr-neha",
    "neha.k@example.org",
    "Opd-Neha-2026",
    "+919800000001",
    "female",
    "Main Campus",
    "OPD",
    "Doctor",
    "",
]
MAX_IMPORT_ROWS = 500
MAX_BULK_USERS = 200
MAX_EXPORT_ROWS = 10000


def _csv_response(filename, header, rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    writer.writerows(rows)
    response = HttpResponse(buffer.getvalue(), content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


def _pydantic_messages(error: PydanticError):
    messages = []
    for item in error.errors():
        field = ".".join(
            str(part) for part in item.get("loc", ()) if part != "__root__"
        )
        message = str(item.get("msg", "")).removeprefix("Value error, ")
        messages.append(f"{field}: {message}" if field else message)
    return messages


def _match(queryset, value, name_field="name"):
    """Finds one object by external id or (case-insensitive) name."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        found = queryset.filter(external_id=uuid.UUID(value)).first()
    except ValueError:
        found = None  # not an id; match by name below
    if found:
        return found
    matches = list(queryset.filter(**{f"{name_field}__iexact": value})[:2])
    return matches[0] if len(matches) == 1 else None


def _parse_day(value):
    value = (value or "").strip()
    if not value:
        return None
    day = datetime.date.fromisoformat(value)
    end = datetime.datetime.combine(day, datetime.time(23, 59, 59))
    end = timezone.make_aware(end)
    if end <= timezone.now():
        raise ValueError("expires_on must be a future date")
    return end


class BulkAssignRequest(BaseModel):
    usernames: list[str] = Field(min_length=1, max_length=MAX_BULK_USERS)
    action: str = Field(pattern="^(assign|remove)$")
    facility_organization: UUID4
    role: UUID4 | None = None
    expires_at: datetime.datetime | None = None
    reason: str = Field("", max_length=1000)


class SecurityPolicyUpdate(BaseModel):
    mfa_required_for_superusers: bool = False
    mfa_required_role_ids: list[UUID4] = []
    auto_deactivate_after_days: int | None = Field(None, ge=7, le=3650)
    max_session_hours: int | None = Field(None, ge=1, le=24 * 30)
    approval_required_for_high_privilege: bool = False
    temporary_role_ids: list[UUID4] = []
    temporary_role_days: int = Field(30, ge=1, le=3650)


POLICY_FIELDS = list(SecurityPolicyUpdate.model_fields)


def serialize_policy(policy):
    data = {}
    for field in POLICY_FIELDS:
        value = getattr(policy, field)
        data[field] = [str(v) for v in value] if isinstance(value, list) else value
    data["updated_by"] = policy.updated_by.username if policy.updated_by else None
    data["modified_date"] = policy.modified_date
    return data


class AccessAdminToolsMixin:
    """Mixed into AccessAdminViewSet, which already limits it to super admins."""

    # ---------------- Bulk import ----------------
    @action(detail=False, methods=["GET"])
    def bulk_import_template(self, request, *args, **kwargs):
        return _csv_response(
            "users-import-template.csv", IMPORT_COLUMNS, [IMPORT_EXAMPLE]
        )

    def _read_import_rows(self, request):
        upload = request.FILES.get("file")
        if upload is not None:
            if upload.size > 2 * 1024 * 1024:
                raise ValidationError("The file is larger than 2 MB")
            text = upload.read().decode("utf-8-sig")
            reader = csv.DictReader(io.StringIO(text))
            missing = {
                "first_name",
                "username",
                "email",
                "password",
                "phone_number",
            } - set(reader.fieldnames or [])
            if missing:
                message = f"The file is missing columns: {', '.join(sorted(missing))}"
                raise ValidationError(message)
            rows = list(reader)
        else:
            rows = request.data.get("rows")
            if not isinstance(rows, list):
                raise ValidationError("Send a CSV file as 'file', or JSON 'rows'")
        if not rows:
            raise ValidationError("The file has no rows")
        if len(rows) > MAX_IMPORT_ROWS:
            message = f"Import at most {MAX_IMPORT_ROWS} rows at a time"
            raise ValidationError(message)
        return [
            {key.strip(): (value or "").strip() for key, value in row.items() if key}
            for row in rows
        ]

    def _check_import_row(self, row, seen):
        """Returns (spec, facility_org, role, expires_at, errors) for one row."""
        errors = []
        user_data = {
            "first_name": row.get("first_name", ""),
            "last_name": row.get("last_name", ""),
            "username": row.get("username", ""),
            "email": row.get("email", ""),
            "password": row.get("password") or None,
            "phone_number": row.get("phone_number", ""),
            "gender": (row.get("gender") or "other").lower(),
            "role_orgs": [],
        }
        spec = None
        if not user_data["password"]:
            errors.append("password: required, the user signs in with it")
        try:
            spec = UserCreateSpec(**user_data)
        except PydanticError as e:
            errors.extend(_pydantic_messages(e))
        for key in ("username", "email", "phone_number"):
            value = user_data[key].lower()
            if value and value in seen[key]:
                errors.append(f"{key}: repeated in this file")
            seen[key].add(value)

        organization = role = expires_at = None
        if row.get("role") or row.get("facility"):
            organization, role, expires_at = self._check_import_access(row, errors)
        return spec, organization, role, expires_at, errors

    def _check_import_access(self, row, errors):
        """Returns (facility_org, role, expires_at) for a row's role columns."""
        organization = expires_at = None
        facility = _match(Facility.objects.all(), row.get("facility"))
        if facility is None:
            errors.append("facility: not found (use the facility name or id)")
        else:
            departments = FacilityOrganization.objects.filter(facility=facility)
            if row.get("department"):
                organization = _match(departments, row.get("department"))
                if organization is None:
                    errors.append(
                        f"department: not found in {facility.name} (use its name or id)"
                    )
            else:
                organization = departments.filter(org_type="root").first()
                if organization is None:
                    errors.append(f"department: {facility.name} has no root")
        role = _match(RoleModel.objects.filter(is_archived=False), row.get("role"))
        if role is None:
            errors.append("role: not found (use the role name or id)")
        try:
            expires_at = _parse_day(row.get("expires_on"))
        except ValueError as e:
            errors.append(
                "expires_on: use YYYY-MM-DD" if "Invalid" in str(e) else str(e)
            )
        return organization, role, expires_at

    @action(
        detail=False,
        methods=["POST"],
        parser_classes=[MultiPartParser, FormParser, JSONParser],
    )
    def bulk_import(self, request, *args, **kwargs):
        """
        Checks every row first. With dry_run=true nothing is saved. Otherwise
        each valid row is created on its own, so one bad row does not stop the rest.
        """
        rows = self._read_import_rows(request)
        dry_run = str(
            request.data.get("dry_run", request.query_params.get("dry_run", ""))
        ).lower() in ("1", "true", "yes")
        seen = {"username": set(), "email": set(), "phone_number": set()}
        results = []
        created = 0
        for index, row in enumerate(rows, start=2):  # row 1 is the header
            spec, organization, role, expires_at, errors = self._check_import_row(
                row, seen
            )
            result = {
                "row": index,
                "username": row.get("username", ""),
                "role": role.name if role else None,
                "department": organization.name if organization else None,
            }
            if errors:
                results.append({**result, "status": "error", "errors": errors})
                continue
            if dry_run:
                results.append({**result, "status": "valid", "errors": []})
                continue
            try:
                with transaction.atomic():
                    user = spec.de_serialize()
                    user.created_by = request.user
                    user.save()
                    record_access_event(
                        AccessAuditAction.user_created,
                        request.user,
                        target_user=user,
                        new_value={"username": user.username, "source": "bulk_import"},
                    )
                    if organization and role:
                        assignment = FacilityOrganizationUser.objects.create(
                            organization=organization,
                            user=user,
                            role=role,
                            expires_at=expires_at or default_expiry(role),
                            created_by=request.user,
                            updated_by=request.user,
                        )
                        record_access_event(
                            AccessAuditAction.role_assigned,
                            request.user,
                            target_user=user,
                            role=role,
                            scope=organization,
                            new_value=assignment_value(assignment),
                        )
                results.append({**result, "status": "created", "errors": []})
                created += 1
            except IntegrityError as e:
                results.append({**result, "status": "error", "errors": [str(e)]})
        failed = sum(1 for r in results if r["status"] == "error")
        if created:
            record_access_event(
                AccessAuditAction.users_imported,
                request.user,
                new_value={"created": created, "failed": failed, "rows": len(rows)},
            )
        return Response(
            {
                "dry_run": dry_run,
                "rows": len(rows),
                "valid": sum(1 for r in results if r["status"] in ("valid", "created")),
                "created": created,
                "failed": failed,
                "results": results,
            }
        )

    # ---------------- Bulk assign ----------------
    @action(detail=False, methods=["POST"])
    def bulk_assign(self, request, *args, **kwargs):
        from care.security.api.viewsets.access_requests import (
            check_no_conflicting_assignment,
        )
        from care.security.tasks import is_last_root_member

        data = BulkAssignRequest(**request.data)
        organization = FacilityOrganization.objects.filter(
            external_id=data.facility_organization
        ).first()
        if organization is None:
            raise ValidationError("Department not found")
        role = None
        if data.role:
            role = RoleModel.objects.filter(external_id=data.role).first()
            if role is None:
                raise ValidationError("Role not found")
        if data.action == "assign" and role is None:
            raise ValidationError("Choose the role to give")
        if data.expires_at and data.expires_at <= timezone.now():
            raise ValidationError("Expiry must be in the future")

        users = {
            u.username.lower(): u
            for u in User.objects.filter(username__in=data.usernames)
        }
        results = []
        for username in dict.fromkeys(data.usernames):
            user = users.get(username.lower())
            if user is None:
                results.append(
                    {
                        "username": username,
                        "status": "error",
                        "message": "User not found",
                    }
                )
                continue
            try:
                with transaction.atomic():
                    message = (
                        self._bulk_give(
                            request,
                            user,
                            organization,
                            role,
                            data,
                            check_no_conflicting_assignment,
                        )
                        if data.action == "assign"
                        else self._bulk_remove(
                            request, user, organization, role, data, is_last_root_member
                        )
                    )
                status_name = "skipped" if message.startswith("Skipped") else "done"
                results.append(
                    {"username": username, "status": status_name, "message": message}
                )
            except ValidationError as e:
                detail = e.detail[0] if isinstance(e.detail, list) else e.detail
                results.append(
                    {"username": username, "status": "error", "message": str(detail)}
                )
        done = sum(1 for r in results if r["status"] == "done")
        if done:
            record_access_event(
                AccessAuditAction.roles_bulk_assigned,
                request.user,
                role=role,
                scope=organization,
                new_value={
                    "action": data.action,
                    "done": done,
                    "usernames": [
                        r["username"] for r in results if r["status"] == "done"
                    ],
                },
                reason=data.reason,
            )
        return Response(
            {
                "done": done,
                "skipped": sum(1 for r in results if r["status"] == "skipped"),
                "failed": sum(1 for r in results if r["status"] == "error"),
                "results": results,
            }
        )

    def _bulk_give(self, request, user, organization, role, data, check_conflict):
        expires_at = data.expires_at if "expires_at" in data.model_fields_set else None
        existing = check_conflict(user, organization)
        if existing:
            if existing.role_id == role.id and existing.expires_at == expires_at:
                return "Skipped: already has this role here"
            old_value = assignment_value(existing)
            existing.role = role
            existing.expires_at = expires_at or default_expiry(role)
            existing.updated_by = request.user
            existing.save()
            record_access_event(
                AccessAuditAction.role_changed,
                request.user,
                target_user=user,
                role=role,
                scope=organization,
                old_value=old_value,
                new_value=assignment_value(existing),
                reason=data.reason,
            )
            return "Role changed"
        assignment = FacilityOrganizationUser.objects.create(
            organization=organization,
            user=user,
            role=role,
            expires_at=expires_at or default_expiry(role),
            created_by=request.user,
            updated_by=request.user,
        )
        record_access_event(
            AccessAuditAction.role_assigned,
            request.user,
            target_user=user,
            role=role,
            scope=organization,
            new_value=assignment_value(assignment),
            reason=data.reason,
        )
        return "Role given"

    def _bulk_remove(
        self, request, user, organization, role, data, is_last_root_member
    ):
        assignments = FacilityOrganizationUser.objects.filter(
            user=user, organization=organization
        ).select_related("role", "organization")
        if role:
            assignments = assignments.filter(role=role)
        assignment = assignments.first()
        if assignment is None:
            return "Skipped: has no matching role here"
        if is_last_root_member(assignment):
            raise ValidationError(
                "Cannot remove the last person from the facility root"
            )
        assignment.deleted = True
        assignment.updated_by = request.user
        assignment.save(update_fields=["deleted", "updated_by", "modified_date"])
        record_access_event(
            AccessAuditAction.role_revoked,
            request.user,
            target_user=user,
            role=assignment.role,
            scope=organization,
            old_value=assignment_value(assignment),
            reason=data.reason,
        )
        return "Role removed"

    # ---------------- Exports ----------------
    @action(detail=False, methods=["GET"])
    def export_users(self, request, *args, **kwargs):
        from care.emr.api.viewsets.user import UserFilter

        queryset = UserFilter(
            request.query_params, queryset=User.objects.filter(deleted=False)
        ).qs.order_by("first_name", "last_name")
        search = request.query_params.get("search")
        if search:
            queryset = queryset.filter(
                Q(first_name__icontains=search)
                | Q(last_name__icontains=search)
                | Q(username__icontains=search)
            )
        rows = []
        for user in queryset[:MAX_EXPORT_ROWS]:
            roles = "; ".join(
                "{role} @ {where}{until}".format(
                    role=a["role"]["name"],
                    where=(
                        f"{a['facility']['name']} / {a['organization']['name']}"
                        if a["facility"]
                        else a["organization"]["name"]
                    ),
                    until=f" (until {a['expires_at']:%Y-%m-%d})"
                    if a["expires_at"]
                    else "",
                )
                for a in user_role_assignments(user)
            )
            rows.append(
                [
                    user.username,
                    user.first_name,
                    user.last_name,
                    user.email,
                    user.phone_number,
                    "yes" if user.is_active else "no",
                    "yes" if user.is_superuser else "no",
                    "yes" if user.is_mfa_enabled() else "no",
                    user.last_login.isoformat() if user.last_login else "",
                    roles,
                ]
            )
        return _csv_response(
            f"users-{timezone.now():%Y%m%d}.csv",
            [
                "username",
                "first_name",
                "last_name",
                "email",
                "phone",
                "active",
                "super_admin",
                "two_factor",
                "last_sign_in",
                "roles",
            ],
            rows,
        )

    @action(detail=False, methods=["GET"])
    def export_audit(self, request, *args, **kwargs):
        from care.security.api.viewsets.access_admin import AccessAuditFilter

        queryset = AccessAuditFilter(
            request.query_params,
            queryset=AccessAuditEvent.objects.select_related("facility").order_by(
                "-created_date"
            ),
        ).qs[:MAX_EXPORT_ROWS]
        rows = [
            [
                event.created_date.isoformat(),
                event.actor_username or "System",
                event.action,
                event.target_username,
                event.role_name,
                event.facility.name if event.facility else "",
                event.scope_name,
                event.old_value,
                event.new_value,
                event.reason,
            ]
            for event in queryset
        ]
        return _csv_response(
            f"access-audit-{timezone.now():%Y%m%d}.csv",
            [
                "when",
                "actor",
                "action",
                "target_user",
                "role",
                "facility",
                "scope",
                "old_value",
                "new_value",
                "reason",
            ],
            rows,
        )

    # ---------------- Matrix ----------------
    @action(detail=False, methods=["GET"])
    def matrix(self, request, *args, **kwargs):
        """Per role, how many permissions it has in each permission context."""
        totals = {}
        for context in PermissionModel.objects.values_list("context", flat=True):
            totals[context] = totals.get(context, 0) + 1
        granted = {}
        for role_id, context in RolePermission.objects.values_list(
            "role_id", "permission__context"
        ):
            granted.setdefault(role_id, {}).setdefault(context, 0)
            granted[role_id][context] += 1
        roles = RoleModel.objects.filter(is_archived=False).order_by(
            "-is_system", "name"
        )
        return Response(
            {
                "contexts": [
                    {"context": c, "total": n} for c, n in sorted(totals.items())
                ],
                "roles": [
                    {
                        "id": str(role.external_id),
                        "name": role.name,
                        "is_system": role.is_system,
                        "granted": {
                            c: granted.get(role.id, {}).get(c, 0) for c in totals
                        },
                    }
                    for role in roles
                ],
            }
        )

    # ---------------- Security policy ----------------
    @action(detail=False, methods=["GET", "PUT"])
    def security_policy(self, request, *args, **kwargs):
        policy = SecurityPolicy.get()
        if request.method == "GET":
            return Response(serialize_policy(policy))
        data = SecurityPolicyUpdate(**request.data)
        role_ids = set(data.mfa_required_role_ids) | set(data.temporary_role_ids)
        known = set(
            RoleModel.objects.filter(external_id__in=role_ids).values_list(
                "external_id", flat=True
            )
        )
        if role_ids - known:
            raise ValidationError("Some of the chosen roles do not exist")
        before = serialize_policy(policy)
        with transaction.atomic():
            for field in POLICY_FIELDS:
                setattr(policy, field, getattr(data, field))
            policy.updated_by = request.user
            policy.save()
            after = serialize_policy(policy)
            changed = [f for f in POLICY_FIELDS if before[f] != after[f]]
            if changed:
                record_access_event(
                    AccessAuditAction.policy_updated,
                    request.user,
                    old_value={f: before[f] for f in changed},
                    new_value={f: after[f] for f in changed},
                )
        forget_cached_policy()
        return Response(after)
