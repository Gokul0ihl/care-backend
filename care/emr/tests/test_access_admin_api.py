from datetime import timedelta
from unittest.mock import patch

from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient

from care.security.models import AccessAuditAction, AccessAuditEvent
from care.users.models import User
from care.utils.tests.base import CareAPITestBase


class AccessAdminTestBase(CareAPITestBase):
    def setUp(self):
        super().setUp()
        self.superuser = self.create_super_user(username="root_admin")
        self.user = self.create_user(username="nurse_asha", is_active=True)
        self.facility = self.create_facility(user=self.superuser)
        self.department = self.create_facility_organization(
            facility=self.facility, name="Emergency", org_type="dept"
        )
        self.nurse_role = self.create_role_with_permissions(
            ["can_read_facility", "can_list_user"], role_name="Test Nurse"
        )
        self.doctor_role = self.create_role_with_permissions(
            ["can_read_facility", "can_create_patient"], role_name="Test Doctor"
        )
        self.client.force_authenticate(user=self.superuser)

    def user_url(self, username, action=None):
        if action:
            return reverse(f"users-{action}", kwargs={"username": username})
        return reverse("users-detail", kwargs={"username": username})

    def last_event(self):
        return AccessAuditEvent.objects.order_by("-created_date").first()


class UserAccountActionsTest(AccessAdminTestBase):
    def test_deactivate_and_reactivate_user(self):
        response = self.client.post(
            self.user_url(self.user.username, "set-active"),
            {"is_active": False, "reason": "Left the hospital"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(response.json()["is_active"])
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertIsNotNone(self.user.tokens_valid_after)
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.user_deactivated)
        self.assertEqual(event.target_user, self.user)
        self.assertEqual(event.actor, self.superuser)
        self.assertEqual(event.reason, "Left the hospital")

        response = self.client.post(
            self.user_url(self.user.username, "set-active"),
            {"is_active": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertEqual(self.last_event().action, AccessAuditAction.user_activated)

    def test_non_superuser_cannot_deactivate(self):
        other = self.create_user()
        self.client.force_authenticate(user=other)
        response = self.client.post(
            self.user_url(self.user.username, "set-active"),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_cannot_deactivate_self(self):
        response = self.client.post(
            self.user_url(self.superuser.username, "set-active"),
            {"is_active": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_grant_and_revoke_superuser(self):
        response = self.client.post(
            self.user_url(self.user.username, "set-superuser"),
            {"is_superuser": True},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.json()["is_superuser"])
        self.assertEqual(self.last_event().action, AccessAuditAction.superuser_granted)

        response = self.client.post(
            self.user_url(self.user.username, "set-superuser"),
            {"is_superuser": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(self.last_event().action, AccessAuditAction.superuser_revoked)

    def test_cannot_remove_own_superuser(self):
        response = self.client.post(
            self.user_url(self.superuser.username, "set-superuser"),
            {"is_superuser": False},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @patch("care.emr.api.viewsets.user_access.send_totp_disabled_email.delay")
    def test_reset_mfa(self, mock_email):
        self.user.mfa_settings = {"totp": {"enabled": True, "backup_codes": []}}
        self.user.totp_secret = "SECRET"
        self.user.save()
        response = self.client.post(
            self.user_url(self.user.username, "reset-mfa"),
            {"reason": "Lost phone"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_mfa_enabled())
        self.assertIsNone(self.user.totp_secret)
        mock_email.assert_called_once()
        self.assertEqual(self.last_event().action, AccessAuditAction.mfa_reset)

    def test_reset_mfa_when_not_enabled(self):
        response = self.client.post(
            self.user_url(self.user.username, "reset-mfa"), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_set_password(self):
        response = self.client.post(
            self.user_url(self.user.username, "set-password"),
            {"password": "Ward7-Kestrel-2026", "reason": "Forgot password"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password("Ward7-Kestrel-2026"))
        self.assertIsNotNone(self.user.tokens_valid_after)
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.password_set)
        self.assertEqual(event.reason, "Forgot password")
        self.assertNotIn("Ward7-Kestrel-2026", str(event.old_value))
        self.assertNotIn("Ward7-Kestrel-2026", str(event.new_value))

    def test_set_password_rejects_weak_password(self):
        response = self.client.post(
            self.user_url(self.user.username, "set-password"),
            {"password": "12345678"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("too common", str(response.json()))
        self.user.refresh_from_db()
        self.assertFalse(self.user.check_password("12345678"))

    def test_set_password_not_for_self_or_non_superuser(self):
        response = self.client.post(
            self.user_url(self.superuser.username, "set-password"),
            {"password": "Ward7-Kestrel-2026"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        other = self.create_user(username="clerk_ravi", is_active=True)
        self.client.force_authenticate(user=other)
        response = self.client.post(
            self.user_url(self.user.username, "set-password"),
            {"password": "Ward7-Kestrel-2026"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_create_user_with_password_sends_no_email(self):
        with patch("care.emr.api.viewsets.user.send_password_reset_email") as mock:
            response = self.client.post(
                reverse("users-list"),
                {
                    "first_name": "Meena",
                    "last_name": "Rao",
                    "username": "meena_rao",
                    "gender": "female",
                    "phone_number": "+919812345678",
                    "email": "meena@example.com",
                    "password": "Ward7-Kestrel-2026",
                    "role_orgs": [],
                },
                format="json",
            )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.json())
        mock.assert_not_called()
        self.assertTrue(
            User.objects.get(username="meena_rao").check_password("Ward7-Kestrel-2026")
        )


class SessionRevocationTest(AccessAdminTestBase):
    password = "Str0ng!Passw0rd#2026"

    def login(self, user):
        client = APIClient()
        response = client.post(
            "/api/v1/auth/login/",
            {"username": user.username, "password": self.password},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response.json()

    def test_revoke_sessions_rejects_old_tokens(self):
        staff = self.create_user_with_password(self.password, username="staff_one")
        tokens = self.login(staff)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        me_url = reverse("users-getcurrentuser")
        self.assertEqual(api.get(me_url).status_code, status.HTTP_200_OK)

        response = self.client.post(
            self.user_url(staff.username, "revoke-sessions"), {}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.last_event().action, AccessAuditAction.sessions_revoked)

        # This API answers rejected tokens with 403 (authenticate_header is empty)
        self.assertEqual(api.get(me_url).status_code, status.HTTP_403_FORBIDDEN)
        refresh = APIClient().post(
            reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json"
        )
        self.assertNotEqual(refresh.status_code, status.HTTP_200_OK)

    def test_tokens_issued_after_revocation_work(self):
        staff = self.create_user_with_password(self.password, username="staff_two")
        staff.tokens_valid_after = timezone.now() - timedelta(minutes=1)
        staff.save(update_fields=["tokens_valid_after"])
        tokens = self.login(staff)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens['access']}")
        self.assertEqual(
            api.get(reverse("users-getcurrentuser")).status_code, status.HTTP_200_OK
        )

    def test_deactivated_user_cannot_refresh(self):
        staff = self.create_user_with_password(self.password, username="staff_three")
        tokens = self.login(staff)
        User.objects.filter(id=staff.id).update(is_active=False)
        refresh = APIClient().post(
            reverse("token_refresh"), {"refresh": tokens["refresh"]}, format="json"
        )
        self.assertNotEqual(refresh.status_code, status.HTTP_200_OK)


class UserAccessViewsTest(AccessAdminTestBase):
    def setUp(self):
        super().setUp()
        self.attach_role_facility_organization_user(
            self.department, self.user, self.nurse_role
        )

    def test_role_assignments(self):
        response = self.client.get(
            self.user_url(self.user.username, "role-assignments")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["count"], 1)
        assignment = data["results"][0]
        self.assertEqual(assignment["role"]["name"], "Test Nurse")
        self.assertEqual(assignment["organization"]["name"], "Emergency")
        self.assertEqual(assignment["facility"]["id"], str(self.facility.external_id))

    def test_effective_permissions_merge_roles(self):
        org = self.create_organization(org_type="role", name="State")
        self.attach_role_organization_user(org, self.user, self.doctor_role)
        response = self.client.get(
            self.user_url(self.user.username, "effective-permissions")
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        slugs = {row["slug"]: row for row in data["results"]}
        self.assertEqual(
            set(slugs), {"can_read_facility", "can_list_user", "can_create_patient"}
        )
        # Granted by both roles
        self.assertEqual(len(slugs["can_read_facility"]["granted_by"]), 2)

    def test_effective_permissions_for_one_facility(self):
        org = self.create_organization(org_type="role", name="State")
        self.attach_role_organization_user(org, self.user, self.doctor_role)
        response = self.client.get(
            self.user_url(self.user.username, "effective-permissions"),
            {"facility": str(self.facility.external_id)},
        )
        slugs = {row["slug"] for row in response.json()["results"]}
        self.assertEqual(slugs, {"can_read_facility", "can_list_user"})

    def test_user_list_filters(self):
        other = self.create_user(username="doc_ravi", is_active=False)
        list_url = reverse("users-list")

        response = self.client.get(list_url, {"role": str(self.nurse_role.external_id)})
        usernames = {row["username"] for row in response.json()["results"]}
        self.assertEqual(usernames, {"nurse_asha"})
        self.assertIn("is_active", response.json()["results"][0])

        response = self.client.get(
            list_url, {"facility": str(self.facility.external_id)}
        )
        usernames = {row["username"] for row in response.json()["results"]}
        self.assertIn("nurse_asha", usernames)
        self.assertNotIn(other.username, usernames)

        response = self.client.get(list_url, {"is_active": "false"})
        usernames = {row["username"] for row in response.json()["results"]}
        self.assertIn(other.username, usernames)
        self.assertNotIn("nurse_asha", usernames)

        response = self.client.get(list_url, {"is_superuser": "true"})
        usernames = {row["username"] for row in response.json()["results"]}
        self.assertEqual(usernames, {"root_admin"})


class RoleAuditTest(AccessAdminTestBase):
    def test_role_list_has_user_count(self):
        self.attach_role_facility_organization_user(
            self.department, self.user, self.nurse_role
        )
        response = self.client.get(reverse("role-list"), {"name": "Test Nurse"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["results"][0]["assigned_user_count"], 1)

    def test_cannot_delete_assigned_role(self):
        self.attach_role_facility_organization_user(
            self.department, self.user, self.nurse_role
        )
        response = self.client.delete(
            reverse("role-detail", kwargs={"external_id": self.nurse_role.external_id})
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_role_update_is_audited(self):
        response = self.client.put(
            reverse("role-detail", kwargs={"external_id": self.nurse_role.external_id}),
            {
                "name": "Test Nurse",
                "description": "Ward nurse",
                "permissions": ["can_read_facility", "can_create_patient"],
                "contexts": ["FACILITY"],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.role_updated)
        self.assertEqual(event.new_value["permissions_added"], ["can_create_patient"])
        self.assertEqual(event.old_value["permissions_removed"], ["can_list_user"])


class AssignmentAuditTest(AccessAdminTestBase):
    def users_url(self, pk=None):
        kwargs = {
            "facility_external_id": self.facility.external_id,
            "facility_organizations_external_id": self.department.external_id,
        }
        if pk:
            kwargs["external_id"] = pk
            return reverse("facility-organization-users-detail", kwargs=kwargs)
        return reverse("facility-organization-users-list", kwargs=kwargs)

    def test_assign_change_and_revoke_are_audited(self):
        response = self.client.post(
            self.users_url(),
            {
                "user": str(self.user.external_id),
                "role": str(self.nurse_role.external_id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.role_assigned)
        self.assertEqual(event.scope_name, "Emergency")
        self.assertEqual(event.facility, self.facility)

        assignment_id = response.json()["id"]
        response = self.client.put(
            self.users_url(assignment_id),
            {
                "user": str(self.user.external_id),
                "role": str(self.doctor_role.external_id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.role_changed)
        self.assertEqual(event.old_value["role"]["name"], "Test Nurse")
        self.assertEqual(event.new_value["role"]["name"], "Test Doctor")

        response = self.client.delete(self.users_url(assignment_id))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(self.last_event().action, AccessAuditAction.role_revoked)


class AccessAdminReadTest(AccessAdminTestBase):
    def test_audit_list_and_filters(self):
        self.client.post(
            self.user_url(self.user.username, "set-active"),
            {"is_active": False},
            format="json",
        )
        response = self.client.get(reverse("access-audit-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()["results"]
        self.assertEqual(results[0]["action"], "user_deactivated")
        self.assertEqual(results[0]["target_user"]["username"], "nurse_asha")
        self.assertEqual(results[0]["actor"]["username"], "root_admin")

        response = self.client.get(
            reverse("access-audit-list"), {"action": "role_created"}
        )
        self.assertEqual(response.json()["count"], 0)

        response = self.client.get(
            reverse("access-audit-list"), {"target_user": "nurse_asha"}
        )
        self.assertEqual(response.json()["count"], 1)

    def test_audit_requires_superuser(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.get(reverse("access-audit-list"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.client.get(reverse("access-admin-overview"))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_overview(self):
        self.attach_role_facility_organization_user(
            self.department, self.user, self.nurse_role
        )
        response = self.client.get(reverse("access-admin-overview"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertGreaterEqual(data["users"]["active"], 2)
        self.assertGreaterEqual(data["users"]["superusers"], 1)
        nurse_row = next(
            row for row in data["users_per_role"] if row["role"]["name"] == "Test Nurse"
        )
        self.assertEqual(nurse_row["user_count"], 1)
