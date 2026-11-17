"""Records access changes for the access portal's Audit Log.

Call record_access_event() from the code path that makes the change, inside
the same transaction, so a change and its audit row are saved together.
"""

from care.security.models.access_audit import AccessAuditEvent, AccessAuditScope


def _scope_fields(scope):
    """Scope is an Organization, a FacilityOrganization, or None."""
    from care.emr.models.organization import FacilityOrganization, Organization

    if isinstance(scope, FacilityOrganization):
        return {
            "scope_type": AccessAuditScope.facility_organization,
            "scope_external_id": scope.external_id,
            "scope_name": scope.name,
            "facility_id": scope.facility_id,
        }
    if isinstance(scope, Organization):
        return {
            "scope_type": AccessAuditScope.organization,
            "scope_external_id": scope.external_id,
            "scope_name": scope.name,
        }
    return {}


def record_access_event(
    action,
    actor,
    *,
    target_user=None,
    role=None,
    scope=None,
    old_value=None,
    new_value=None,
    reason="",
):
    fields = {
        "action": action,
        "actor": actor if actor and actor.is_authenticated else None,
        "actor_username": getattr(actor, "username", "") or "",
        "target_user": target_user,
        "target_username": target_user.username if target_user else "",
        "role": role,
        "role_name": role.name if role else "",
        "old_value": old_value or {},
        "new_value": new_value or {},
        "reason": reason or "",
    }
    if scope is not None:
        fields.update(_scope_fields(scope))
    elif role is not None and target_user is None:
        fields.update(
            {
                "scope_type": AccessAuditScope.role,
                "scope_external_id": role.external_id,
                "scope_name": role.name,
            }
        )
    elif target_user is not None:
        fields["scope_type"] = AccessAuditScope.user
    return AccessAuditEvent.objects.create(**fields)


def role_summary(role):
    if role is None:
        return {}
    return {"id": str(role.external_id), "name": role.name}


def assignment_value(assignment, role=None):
    """What an audit row stores about a role assignment."""
    expires_at = assignment.expires_at
    return {
        "role": role_summary(role or assignment.role),
        "expires_at": expires_at.isoformat()
        if hasattr(expires_at, "isoformat")
        else expires_at,
    }
