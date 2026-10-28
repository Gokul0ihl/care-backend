import json
from pathlib import Path

from django.test import SimpleTestCase
from django.urls import resolve
from drf_spectacular.generators import SchemaGenerator
from drf_spectacular.validation import validate_schema

from hmis.mobile_schema import MOBILE_PREFIX


class MobileSchemaTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.schema = SchemaGenerator().get_schema(request=None, public=True)
        cls.contract = json.loads(
            Path(__file__).parents[1].joinpath("mobile.openapi.json").read_text()
        )

    def test_mobile_operations_match_routes_and_contract(self):
        count = 0
        for path, operations in self.contract["paths"].items():
            full_path = f"{MOBILE_PREFIX}{path}"
            self.assertEqual(self.schema["paths"][full_path], operations)
            for method, operation in operations.items():
                if operation.get("x-implementation-status") == "planned":
                    continue
                # Substitute the contract UUID parameters with a valid fake UUID.
                concrete = full_path
                for name in ("facility_id", "doctor_id", "booking_id"):
                    concrete = concrete.replace(
                        "{" + name + "}", "11111111-1111-4111-8111-111111111111"
                    )
                view = resolve(concrete).func.view_class
                self.assertTrue(hasattr(view, method), full_path)
                count += 1
        self.assertEqual(count, 16)

    def test_patient_auth_and_public_login(self):
        paths = self.schema["paths"]
        for path in (
            "/auth/login/send_otp",
            "/auth/login/verify_otp",
            "/auth/refresh",
            "/auth/logout",
            "/auth/login/complete_registration",
        ):
            self.assertEqual(paths[MOBILE_PREFIX + path]["post"]["security"], [])
        self.assertEqual(
            paths[MOBILE_PREFIX + "/appointments"]["post"]["security"],
            [{"MobilePatientAccess": []}],
        )
        self.assertEqual(
            self.schema["components"]["securitySchemes"]["MobilePatientAccess"][
                "scheme"
            ],
            "bearer",
        )

    def test_registration_and_profile_update_are_documented(self):
        operation = self.schema["paths"][
            MOBILE_PREFIX + "/auth/login/complete_registration"
        ]["post"]
        self.assertNotIn("x-implementation-status", operation)
        self.assertEqual(set(operation["responses"]), {"201", "409", "422", "423"})
        self.assertEqual(
            self.schema["paths"][MOBILE_PREFIX + "/auth/registration/facilities"][
                "get"
            ]["security"],
            [],
        )
        self.assertEqual(
            self.schema["paths"][MOBILE_PREFIX + "/users/me"]["patch"]["security"],
            [{"MobilePatientAccess": []}],
        )

    def test_profile_photo_upload_is_documented(self):
        operation = self.schema["paths"][MOBILE_PREFIX + "/users/me/profile-photo"][
            "post"
        ]
        self.assertEqual(operation["security"], [{"MobilePatientAccess": []}])
        self.assertEqual(set(operation["responses"]), {"201", "401", "422", "429"})
        schemas = self.schema["components"]["schemas"]
        self.assertIn("profile_photo_id", schemas["MobileProfileUpdate"]["properties"])
        self.assertEqual(
            schemas["MobileUser"]["properties"]["profile_photo"]["format"], "uri"
        )

    def test_openapi_contract_is_valid(self):
        validate_schema(self.contract)

    def test_mobile_refs_resolve_in_shared_schema(self):
        def check(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    target = self.schema
                    for part in value["$ref"].removeprefix("#/").split("/"):
                        target = target[part]
                for item in value.values():
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)

        for path, operations in self.schema["paths"].items():
            if path.startswith(MOBILE_PREFIX):
                check(operations)
