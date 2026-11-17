from datetime import timedelta

from django.urls import reverse
from django.utils import timezone
from rest_framework import status

from care.emr.models.organization import FacilityOrganizationUser
from care.security.models import (
    AccessAuditAction,
    AccessAuditEvent,
    RolePermission,
)
from care.security.tasks import expire_role_assignments_now
from care.utils.tests.base import CareAPITestBase


class Phase3TestBase(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.superuser = self.create_super_user(username="root_admin")
        self.user = self.create_user(username="nurse_asha")
        self.other = self.create_user(username="clerk_ravi")
        self.facility = self.create_facility(user=self.superuser)
        self.department = self.create_facility_organization(
            facility=self.facility, name="Emergency", org_type="dept"
        )
        self.nurse_role = self.create_role_with_permissions(
            ["can_read_facility", "can_list_user"], role_name="Test Nurse"
        )
        self.admin_role = self.create_role_with_permissions(
            ["can_read_facility", "can_create_user"], role_name="Test Admin"
        )
        self.client.force_authenticate(user=self.superuser)

    def last_event(self):
        return AccessAuditEvent.objects.order_by("-created_date").first()

    def users_url(self, pk=None):
        kwargs = {
            "facility_external_id": self.facility.external_id,
            "facility_organizations_external_id": self.department.external_id,
        }
        if pk:
            kwargs["external_id"] = pk
            return reverse("facility-organization-users-detail", kwargs=kwargs)
        return reverse("facility-organization-users-list", kwargs=kwargs)


class ExpiringAssignmentTest(Phase3TestBase):
    def test_assign_with_expiry_then_clear_it(self):
        expires = (timezone.now() + timedelta(days=10)).isoformat()
        response = self.client.post(
            self.users_url(),
            {
                "user": str(self.user.external_id),
                "role": str(self.nurse_role.external_id),
                "expires_at": expires,
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNotNone(response.json()["expires_at"])
        self.assertIsNotNone(self.last_event().new_value["expires_at"])
        assignment_id = response.json()["id"]

        # role_assignments shows the expiry
        data = self.client.get(
            reverse("users-role-assignments", kwargs={"username": "nurse_asha"})
        ).json()
        self.assertIsNotNone(data["results"][0]["expires_at"])

        response = self.client.put(
            self.users_url(assignment_id),
            {"role": str(self.nurse_role.external_id), "expires_at": None},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertIsNone(response.json()["expires_at"])
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.role_changed)
        self.assertIsNone(event.new_value["expires_at"])

    def test_past_expiry_rejected(self):
        response = self.client.post(
            self.users_url(),
            {
                "user": str(self.user.external_id),
                "role": str(self.nurse_role.external_id),
                "expires_at": (timezone.now() - timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_put_without_expiry_keeps_it(self):
        expires = timezone.now() + timedelta(days=5)
        assignment = FacilityOrganizationUser.objects.create(
            organization=self.department,
            user=self.user,
            role=self.nurse_role,
            expires_at=expires,
        )
        response = self.client.put(
            self.users_url(assignment.external_id),
            {"role": str(self.admin_role.external_id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        assignment.refresh_from_db()
        self.assertEqual(assignment.role, self.admin_role)
        self.assertEqual(assignment.expires_at, expires)

    def test_nightly_task_removes_expired(self):
        expired = FacilityOrganizationUser.objects.create(
            organization=self.department,
            user=self.user,
            role=self.nurse_role,
            expires_at=timezone.now() + timedelta(days=1),
        )
        FacilityOrganizationUser.objects.filter(id=expired.id).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        still_valid = FacilityOrganizationUser.objects.create(
            organization=self.department,
            user=self.other,
            role=self.nurse_role,
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.assertEqual(expire_role_assignments_now(), 1)
        self.assertFalse(
            FacilityOrganizationUser.objects.filter(id=expired.id).exists()
        )
        self.assertTrue(
            FacilityOrganizationUser.objects.filter(id=still_valid.id).exists()
        )
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.role_expired)
        self.assertIsNone(event.actor)
        self.assertEqual(event.target_user, self.user)

    def test_nightly_task_keeps_last_root_member(self):
        root = self.create_facility_organization(
            facility=self.facility, name="Root", org_type="root"
        )
        only = FacilityOrganizationUser.objects.create(
            organization=root, user=self.user, role=self.admin_role
        )
        FacilityOrganizationUser.objects.filter(id=only.id).update(
            expires_at=timezone.now() - timedelta(minutes=1)
        )
        expire_role_assignments_now()
        self.assertTrue(FacilityOrganizationUser.objects.filter(id=only.id).exists())


class AccessRequestTest(Phase3TestBase):
    list_url = reverse("access-request-list")

    def request_as(self, as_user, **body):
        self.client.force_authenticate(user=as_user)
        payload = {
            "role": str(self.nurse_role.external_id),
            "facility_organization": str(self.department.external_id),
            "reason": "Covering night shifts",
            **body,
        }
        return self.client.post(self.list_url, payload, format="json")

    def detail(self, pk, action):
        return reverse(f"access-request-{action}", kwargs={"external_id": pk})

    def test_request_approve_creates_assignment(self):
        response = self.request_as(self.user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        data = response.json()
        self.assertEqual(data["status"], "pending")
        self.assertEqual(data["kind"], "first_access")
        self.assertFalse(data["high_privilege"])
        self.assertEqual(self.last_event().action, AccessAuditAction.request_created)

        # Duplicate pending request is refused
        self.assertEqual(
            self.request_as(self.user).status_code, status.HTTP_400_BAD_REQUEST
        )

        # Requester cannot approve
        response = self.client.post(
            self.detail(data["id"], "approve"), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        self.client.force_authenticate(user=self.superuser)
        expires = (timezone.now() + timedelta(days=14)).isoformat()
        response = self.client.post(
            self.detail(data["id"], "approve"),
            {"comment": "OK for two weeks", "expires_at": expires},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.json()["status"], "approved")
        assignment = FacilityOrganizationUser.objects.get(
            user=self.user, organization=self.department
        )
        self.assertEqual(assignment.role, self.nurse_role)
        self.assertIsNotNone(assignment.expires_at)
        actions = list(
            AccessAuditEvent.objects.order_by("-created_date").values_list(
                "action", flat=True
            )[:2]
        )
        self.assertEqual(
            actions,
            [AccessAuditAction.request_approved, AccessAuditAction.role_assigned],
        )

        # Already approved: cannot review again
        response = self.client.post(
            self.detail(data["id"], "reject"), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_approve_changes_existing_role_in_same_department(self):
        FacilityOrganizationUser.objects.create(
            organization=self.department, user=self.user, role=self.admin_role
        )
        data = self.request_as(self.user).json()
        self.assertEqual(data["kind"], "additional_access")
        self.client.force_authenticate(user=self.superuser)
        response = self.client.post(
            self.detail(data["id"], "approve"), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            FacilityOrganizationUser.objects.get(
                user=self.user, organization=self.department
            ).role,
            self.nurse_role,
        )

    def test_reject_and_cancel(self):
        first = self.request_as(self.user).json()
        self.client.force_authenticate(user=self.superuser)
        response = self.client.post(
            self.detail(first["id"], "reject"),
            {"comment": "Use a lower role"},
            format="json",
        )
        self.assertEqual(response.json()["status"], "rejected")
        self.assertEqual(response.json()["review_comment"], "Use a lower role")
        self.assertFalse(
            FacilityOrganizationUser.objects.filter(user=self.user).exists()
        )

        second = self.request_as(self.user).json()
        self.client.force_authenticate(user=self.other)
        response = self.client.post(
            self.detail(second["id"], "cancel"), {}, format="json"
        )
        # Someone else's request is not visible to them at all
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            self.detail(second["id"], "cancel"), {}, format="json"
        )
        self.assertEqual(response.json()["status"], "cancelled")

    def test_visibility_and_requests_for_others(self):
        self.request_as(self.user)
        # A plain user cannot ask on someone else's behalf
        response = self.request_as(self.other, user=str(self.user.external_id))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        # and does not see other people's requests
        self.client.force_authenticate(user=self.other)
        self.assertEqual(self.client.get(self.list_url).json()["count"], 0)
        self.client.force_authenticate(user=self.superuser)
        self.assertEqual(
            self.client.get(self.list_url, {"status": "pending"}).json()["count"], 1
        )

    def test_high_privilege_flag_and_overview_count(self):
        response = self.request_as(self.user, role=str(self.admin_role.external_id))
        self.assertTrue(response.json()["high_privilege"])
        self.client.force_authenticate(user=self.superuser)
        overview = self.client.get(reverse("access-admin-overview")).json()
        self.assertEqual(overview["requests"], {"pending": 1, "high_privilege": 1})


class CloneAndRoleUsersTest(Phase3TestBase):
    def test_clone_role(self):
        url = reverse("role-clone", kwargs={"external_id": self.nurse_role.external_id})
        response = self.client.post(url, {"name": "Ward Nurse"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.json()
        self.assertFalse(data["is_system"])
        self.assertEqual(
            {p["slug"] for p in data["permissions"]},
            {"can_read_facility", "can_list_user"},
        )
        self.assertEqual(
            RolePermission.objects.filter(role__name="Ward Nurse").count(), 2
        )
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.role_created)
        self.assertEqual(event.new_value["cloned_from"]["name"], "Test Nurse")

        self.assertEqual(
            self.client.post(url, {"name": "ward nurse"}, format="json").status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.client.force_authenticate(user=self.user)
        self.assertEqual(
            self.client.post(url, {"name": "Another"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_role_users(self):
        FacilityOrganizationUser.objects.create(
            organization=self.department, user=self.user, role=self.nurse_role
        )
        url = reverse("role-users", kwargs={"external_id": self.nurse_role.external_id})
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["results"][0]["user"]["username"], "nurse_asha")
        self.assertEqual(
            data["results"][0]["assignments"][0]["organization"]["name"], "Emergency"
        )
        other_facility = self.create_facility(user=self.superuser)
        response = self.client.get(url, {"facility": str(other_facility.external_id)})
        self.assertEqual(response.json()["results"], [])
