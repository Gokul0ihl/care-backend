import csv
import io
from datetime import timedelta

from django.core import mail
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from care.emr.models.organization import FacilityOrganization, FacilityOrganizationUser
from care.security.models import AccessAuditAction, AccessAuditEvent, SecurityPolicy
from care.security.tasks import deactivate_inactive_users_now
from care.users.models import User
from care.utils.tests.base import CareAPITestBase


class Phase4TestBase(CareAPITestBase):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.superuser = self.create_super_user(username="root_admin")
        self.facility = self.create_facility(user=self.superuser, name="Main Campus")
        # Creating a facility also creates its root department.
        self.root = FacilityOrganization.objects.get(
            facility=self.facility, org_type="root"
        )
        self.department = self.create_facility_organization(
            facility=self.facility, name="OPD", org_type="dept"
        )
        self.nurse_role = self.create_role_with_permissions(
            ["can_read_facility", "can_list_user"], role_name="Bulk Nurse"
        )
        self.admin_role = self.create_role_with_permissions(
            ["can_read_facility", "can_create_user"], role_name="Bulk Admin"
        )
        self.client.force_authenticate(user=self.superuser)

    def url(self, name):
        return reverse(f"access-admin-{name}")

    def last_event(self):
        return AccessAuditEvent.objects.order_by("-created_date").first()


def csv_file(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return SimpleUploadedFile(
        "users.csv", buffer.getvalue().encode(), content_type="text/csv"
    )


HEADER = [
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


class BulkImportTest(Phase4TestBase):
    def test_template(self):
        response = self.client.get(self.url("bulk-import-template"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertTrue(
            response.content.decode().startswith("first_name,last_name,username")
        )

    def test_dry_run_checks_rows_and_saves_nothing(self):
        upload = csv_file(
            [
                HEADER,
                [
                    "Neha",
                    "K",
                    "dr-neha",
                    "neha@example.org",
                    "Ward7-Kestrel-2026",
                    "+919800000001",
                    "female",
                    "Main Campus",
                    "OPD",
                    "Bulk Nurse",
                    "",
                ],
                [
                    "Ravi",
                    "S",
                    "dr-neha",
                    "ravi@example.org",
                    "Ward7-Kestrel-2026",
                    "+919800000002",
                    "male",
                    "Main Campus",
                    "Nowhere",
                    "Bulk Nurse",
                    "",
                ],
            ]
        )
        response = self.client.post(
            self.url("bulk-import"),
            {"file": upload, "dry_run": "true"},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.json()
        self.assertTrue(data["dry_run"])
        self.assertEqual([r["status"] for r in data["results"]], ["valid", "error"])
        errors = " ".join(data["results"][1]["errors"])
        self.assertIn("repeated in this file", errors)
        self.assertIn("department", errors)
        self.assertFalse(User.objects.filter(username="dr-neha").exists())

    def test_import_creates_users_with_passwords_and_roles(self):
        future = (timezone.now() + timedelta(days=30)).date().isoformat()
        upload = csv_file(
            [
                HEADER,
                [
                    "Neha",
                    "K",
                    "dr-neha",
                    "neha@example.org",
                    "Ward7-Kestrel-2026",
                    "+919800000001",
                    "female",
                    "main campus",
                    "opd",
                    "bulk nurse",
                    future,
                ],
                [
                    "No",
                    "Role",
                    "no-role",
                    "norole@example.org",
                    "Ward7-Kestrel-2026",
                    "+919800000003",
                    "",
                    "",
                    "",
                    "",
                    "",
                ],
                [
                    "Bad",
                    "Email",
                    "bad-email",
                    "not-an-email",
                    "Ward7-Kestrel-2026",
                    "+919800000004",
                    "male",
                    "",
                    "",
                    "",
                    "",
                ],
            ]
        )
        response = self.client.post(
            self.url("bulk-import"), {"file": upload}, format="multipart"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.json()
        self.assertEqual((data["created"], data["failed"]), (2, 1))
        user = User.objects.get(username="dr-neha")
        self.assertEqual(user.created_by, self.superuser)
        assignment = FacilityOrganizationUser.objects.get(user=user)
        self.assertEqual(assignment.organization, self.department)
        self.assertEqual(assignment.role, self.nurse_role)
        self.assertIsNotNone(assignment.expires_at)
        self.assertTrue(user.check_password("Ward7-Kestrel-2026"))
        self.assertEqual(len(mail.outbox), 0)
        self.assertEqual(self.last_event().action, AccessAuditAction.users_imported)

    def test_import_needs_a_strong_password(self):
        row = {
            "first_name": "Asha",
            "username": "asha-pw",
            "email": "asha@example.org",
            "phone_number": "+919800000005",
        }
        response = self.client.post(
            self.url("bulk-import"),
            {"rows": [row, {**row, "username": "asha-pw2", "password": "12345678"}]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        results = response.json()["results"]
        self.assertIn("password: required", " ".join(results[0]["errors"]))
        self.assertIn("password", " ".join(results[1]["errors"]))
        self.assertFalse(User.objects.filter(username__startswith="asha-pw").exists())

    def test_superuser_only(self):
        self.client.force_authenticate(user=self.create_user())
        response = self.client.post(
            self.url("bulk-import"), {"rows": []}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class BulkAssignTest(Phase4TestBase):
    def setUp(self):
        super().setUp()
        self.a = self.create_user(username="staff_a")
        self.b = self.create_user(username="staff_b")

    def assign(self, **body):
        payload = {
            "usernames": ["staff_a", "staff_b"],
            "action": "assign",
            "facility_organization": str(self.department.external_id),
            "role": str(self.nurse_role.external_id),
            **body,
        }
        return self.client.post(self.url("bulk-assign"), payload, format="json")

    def test_assign_skip_change_remove(self):
        response = self.assign(usernames=["staff_a", "staff_b", "ghost"])
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.json()
        self.assertEqual((data["done"], data["failed"]), (2, 1))
        self.assertEqual(
            FacilityOrganizationUser.objects.filter(
                organization=self.department
            ).count(),
            2,
        )
        self.assertEqual(
            self.last_event().action, AccessAuditAction.roles_bulk_assigned
        )

        self.assertEqual(self.assign().json()["skipped"], 2)

        data = self.assign(role=str(self.admin_role.external_id)).json()
        self.assertEqual(data["done"], 2)
        self.assertEqual(
            FacilityOrganizationUser.objects.get(user=self.a).role, self.admin_role
        )

        data = self.assign(action="remove", role=None).json()
        self.assertEqual(data["done"], 2)
        self.assertFalse(
            FacilityOrganizationUser.objects.filter(
                organization=self.department
            ).exists()
        )

    def test_remove_keeps_last_root_member(self):
        # The facility's creator is put in its root; make staff_a the only one there.
        FacilityOrganizationUser.objects.filter(organization=self.root).delete()
        FacilityOrganizationUser.objects.create(
            organization=self.root, user=self.a, role=self.admin_role
        )
        data = self.client.post(
            self.url("bulk-assign"),
            {
                "usernames": ["staff_a"],
                "action": "remove",
                "facility_organization": str(self.root.external_id),
            },
            format="json",
        ).json()
        self.assertEqual(data["failed"], 1)
        self.assertTrue(FacilityOrganizationUser.objects.filter(user=self.a).exists())

    def test_assign_needs_role(self):
        response = self.assign(role=None)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class MfaFilterTest(Phase4TestBase):
    def test_users_without_two_factor_include_never_set_up(self):
        never = self.create_user(username="never_set_up", mfa_settings={})
        on = self.create_user(
            username="has_totp", mfa_settings={"totp": {"enabled": True}}
        )
        response = self.client.get(
            reverse("users-list"), {"mfa_enabled": "false", "limit": 100}
        )
        usernames = {r["username"] for r in response.json()["results"]}
        self.assertIn(never.username, usernames)
        self.assertNotIn(on.username, usernames)
        overview = self.client.get(self.url("overview")).json()
        self.assertGreaterEqual(overview["users"]["without_mfa"], 2)


class ExportAndMatrixTest(Phase4TestBase):
    def test_export_users(self):
        user = self.create_user(username="export_me", first_name="Export")
        FacilityOrganizationUser.objects.create(
            organization=self.department, user=user, role=self.nurse_role
        )
        response = self.client.get(
            self.url("export-users"), {"role": str(self.nurse_role.external_id)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0][0], "username")
        self.assertEqual([r[0] for r in rows[1:]], ["export_me"])
        self.assertIn("Bulk Nurse @ Main Campus / OPD", rows[1][-1])

    def test_export_audit(self):
        self.client.post(
            reverse(
                "users-set-active",
                kwargs={"username": self.create_user(username="x1").username},
            ),
            {"is_active": False},
            format="json",
        )
        response = self.client.get(self.url("export-audit"))
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[1][2], "user_deactivated")
        self.assertEqual(rows[1][1], "root_admin")

    def test_matrix(self):
        data = self.client.get(self.url("matrix")).json()
        nurse = next(r for r in data["roles"] if r["name"] == "Bulk Nurse")
        self.assertEqual(sum(nurse["granted"].values()), 2)
        self.assertTrue(all("total" in c for c in data["contexts"]))


class SecurityPolicyTest(Phase4TestBase):
    def put_policy(self, **changes):
        body = {**self.client.get(self.url("security-policy")).json(), **changes}
        body.pop("updated_by", None)
        body.pop("modified_date", None)
        return self.client.put(self.url("security-policy"), body, format="json")

    def test_read_and_update(self):
        data = self.client.get(self.url("security-policy")).json()
        self.assertFalse(data["approval_required_for_high_privilege"])
        response = self.put_policy(
            mfa_required_role_ids=[str(self.nurse_role.external_id)],
            max_session_hours=12,
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.json()["max_session_hours"], 12)
        event = self.last_event()
        self.assertEqual(event.action, AccessAuditAction.policy_updated)
        self.assertEqual(event.new_value["max_session_hours"], 12)

        bad = self.put_policy(
            temporary_role_ids=["00000000-0000-4000-8000-000000000000"]
        )
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

        self.client.force_authenticate(user=self.create_user())
        self.assertEqual(
            self.client.get(self.url("security-policy")).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_temporary_role_gets_default_end_date(self):
        self.put_policy(
            temporary_role_ids=[str(self.nurse_role.external_id)],
            temporary_role_days=10,
        )
        user = self.create_user()
        response = self.client.post(
            reverse(
                "facility-organization-users-list",
                kwargs={
                    "facility_external_id": self.facility.external_id,
                    "facility_organizations_external_id": self.department.external_id,
                },
            ),
            {"user": str(user.external_id), "role": str(self.nurse_role.external_id)},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        expires = FacilityOrganizationUser.objects.get(user=user).expires_at
        self.assertAlmostEqual((expires - timezone.now()).days, 9, delta=1)

    def test_mfa_setup_required_on_current_user(self):
        self.put_policy(mfa_required_role_ids=[str(self.nurse_role.external_id)])
        nurse = self.create_user()
        FacilityOrganizationUser.objects.create(
            organization=self.department, user=nurse, role=self.nurse_role
        )
        self.client.force_authenticate(user=nurse)
        data = self.client.get(reverse("users-getcurrentuser")).json()
        self.assertTrue(data["mfa_setup_required"])
        overview_client = APIClient()
        overview_client.force_authenticate(user=self.superuser)
        overview = overview_client.get(self.url("overview")).json()
        self.assertEqual(overview["users"]["missing_required_mfa"], 1)

    def test_max_session_hours_refuses_old_refresh(self):
        self.put_policy(max_session_hours=1)
        user = self.create_user()
        refresh = RefreshToken.for_user(user)
        refresh["iat"] = int((timezone.now() - timedelta(hours=2)).timestamp())
        response = APIClient().post(
            reverse("token_refresh"), {"refresh": str(refresh)}, format="json"
        )
        self.assertNotEqual(response.status_code, status.HTTP_200_OK)

        fresh = RefreshToken.for_user(user)
        response = APIClient().post(
            reverse("token_refresh"), {"refresh": str(fresh)}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_auto_deactivate_inactive_users(self):
        self.put_policy(auto_deactivate_after_days=30)
        stale = self.create_user(username="stale", is_active=True)
        User.objects.filter(id=stale.id).update(
            last_login=timezone.now() - timedelta(days=40)
        )
        fresh = self.create_user(username="fresh", is_active=True)
        User.objects.filter(id=fresh.id).update(
            last_login=timezone.now() - timedelta(days=2)
        )
        self.assertGreaterEqual(deactivate_inactive_users_now(), 1)
        stale.refresh_from_db()
        fresh.refresh_from_db()
        self.superuser.refresh_from_db()
        self.assertFalse(stale.is_active)
        self.assertTrue(fresh.is_active)
        self.assertTrue(self.superuser.is_active)


class ApprovalPolicyTest(Phase4TestBase):
    def test_facility_admin_must_request_high_privilege_roles(self):
        manager_role = self.create_role_with_permissions(
            [
                "can_manage_facility_organization_users",
                "can_list_facility_organization_users",
                "can_manage_facility_organization",
                "can_create_user",
                "can_read_facility",
                # A manager can only give roles whose permissions they hold.
                "can_list_user",
            ],
            role_name="Dept Manager",
        )
        manager = self.create_user()
        FacilityOrganizationUser.objects.create(
            organization=self.department, user=manager, role=manager_role
        )
        SecurityPolicy.objects.update_or_create(
            id=SecurityPolicy.get().id,
            defaults={"approval_required_for_high_privilege": True},
        )
        cache.clear()

        url = reverse(
            "facility-organization-users-list",
            kwargs={
                "facility_external_id": self.facility.external_id,
                "facility_organizations_external_id": self.department.external_id,
            },
        )
        self.client.force_authenticate(user=manager)
        response = self.client.post(
            url,
            {
                "user": str(self.create_user().external_id),
                "role": str(self.admin_role.external_id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertIn("access request", str(response.data))

        response = self.client.post(
            url,
            {
                "user": str(self.create_user().external_id),
                "role": str(self.nurse_role.external_id),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
