"""POH outreach events (health camps, patient education sessions, CME,
webinars) and camp-mode patient capture. See hmis_fe docs/outreach-camps.md."""

import enum

from care.security.permissions.constants import Permission, PermissionContext
from care.security.roles.role import (
    ADMIN_ROLE,
    ADMINISTRATOR,
    CAMP_STAFF_ROLE,
    DOCTOR_ROLE,
    FACILITY_ADMIN_ROLE,
    NURSE_ROLE,
    STAFF_ROLE,
)


class OutreachPermissions(enum.Enum):
    can_manage_outreach_event = Permission(
        "Can create and edit outreach events",
        "Create events, edit drafts and send them for approval",
        PermissionContext.FACILITY,
        [FACILITY_ADMIN_ROLE, ADMINISTRATOR, ADMIN_ROLE, STAFF_ROLE],
    )
    can_approve_outreach_event = Permission(
        "Can approve outreach events",
        "Approve or reject events sent for approval",
        PermissionContext.FACILITY,
        [FACILITY_ADMIN_ROLE, ADMINISTRATOR, ADMIN_ROLE],
    )
    can_capture_camp_participant = Permission(
        "Can capture people at a camp",
        "Use camp mode to capture people at an approved event",
        PermissionContext.FACILITY,
        [
            CAMP_STAFF_ROLE,
            FACILITY_ADMIN_ROLE,
            ADMINISTRATOR,
            ADMIN_ROLE,
            STAFF_ROLE,
            DOCTOR_ROLE,
            NURSE_ROLE,
        ],
    )
