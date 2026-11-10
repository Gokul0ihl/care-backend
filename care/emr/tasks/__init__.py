from celery import Celery, current_app
from celery.schedules import crontab
from django.conf import settings

from care.emr.tasks.cleanup_expired_otps import cleanup_expired_otps
from care.emr.tasks.cleanup_expired_token_slots import cleanup_expired_token_slots
from care.emr.tasks.cleanup_incomplete_file_uploads import (
    cleanup_incomplete_file_uploads,
)
from care.security.tasks import deactivate_inactive_users, expire_role_assignments


@current_app.on_after_finalize.connect
def setup_periodic_tasks(sender: Celery, **kwargs):
    sender.add_periodic_task(
        crontab(hour="0", minute="0"),
        cleanup_expired_token_slots.s(),
        name="cleanup_expired_token_slots",
    )

    sender.add_periodic_task(
        crontab(hour="0", minute="0"),
        cleanup_expired_otps.s(),
        name="cleanup_expired_otps",
    )

    sender.add_periodic_task(
        crontab(hour="0", minute="15"),
        expire_role_assignments.s(),
        name="expire_role_assignments",
    )

    sender.add_periodic_task(
        crontab(hour="0", minute="30"),
        deactivate_inactive_users.s(),
        name="deactivate_inactive_users",
    )

    if cleanup_file_upload_hours := settings.FILE_UPLOAD_EXPIRY_HOURS:
        sender.add_periodic_task(
            cleanup_file_upload_hours * 3600,  # convert hours to seconds
            cleanup_incomplete_file_uploads.s(),
            name="cleanup_incomplete_file_uploads",
        )
