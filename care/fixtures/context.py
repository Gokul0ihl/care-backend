import logging
import sys
import warnings
from contextlib import contextmanager
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import transaction
from django.test import override_settings
from rest_framework.test import APIClient

import care.emr.utils.valueset_coding_type  # noqa: F401

from .base import CareFixtureBase

sys.modules["care.emr.utils.valueset_coding_type"].validate_valueset = lambda f, s, c: c


class _NoOpLock:
    """Bypass create locks inside an outer transaction."""

    def __init__(self, *args, **kwargs):
        pass

    def acquire(self):
        pass

    def release(self):
        pass


@contextmanager
def care_fixture_context(
    base_cls: type[CareFixtureBase] = CareFixtureBase,
    superuser_username: str = "admin",
    superuser_password: str | None = "admin",  # noqa: S107 - dev-only default
):
    """Runs a fixture script atomically as a superuser. The default seed
    (re)sets admin / admin; a script for a shared database can name its own
    superuser with superuser_password=None, which leaves an existing
    password alone and gives a new user an unusable one."""
    if not settings.DEBUG:
        msg = "This command should not be run in production. Exiting..."
        raise RuntimeError(msg)

    audit_logger = logging.getLogger("audit_log")
    original_level = audit_logger.level
    audit_logger.setLevel(logging.WARNING)

    try:
        call_command("sync_permissions_roles")
        call_command("sync_valueset")

        with (
            transaction.atomic(),
            # APIClient sends Host: testserver, which a deployment's explicit
            # DJANGO_ALLOWED_HOSTS would reject with a bare 400.
            override_settings(ALLOWED_HOSTS=[*settings.ALLOWED_HOSTS, "testserver"]),
            patch("care.emr.api.viewsets.patient.PatientCreateLock", _NoOpLock),
            patch(
                "care.emr.api.viewsets.encounter.FacilityEncounterCreateLock",
                _NoOpLock,
            ),
            warnings.catch_warnings(),
        ):
            warnings.filterwarnings(
                "ignore",
                message=r".*received a naive datetime.*",
                category=RuntimeWarning,
            )

            user_model = get_user_model()
            superuser, created = user_model.objects.get_or_create(
                username=superuser_username,
                defaults={
                    "first_name": "Admin",
                    "last_name": "User",
                    "email": f"{superuser_username}@care.test",
                    "is_superuser": True,
                    "is_staff": True,
                },
            )
            if superuser_password is not None:
                superuser.set_password(superuser_password)
            elif created:
                superuser.set_unusable_password()
            superuser.is_superuser = True
            superuser.is_staff = True
            superuser.save()

            client = APIClient()
            client.force_authenticate(user=superuser)

            fixture_base = base_cls(client)
            fixture_base.user = superuser
            yield fixture_base
    finally:
        audit_logger.setLevel(original_level)
