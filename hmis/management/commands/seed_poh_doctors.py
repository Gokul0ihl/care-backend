"""Loads Pacific OneHealth's consultants from hmis.poh_doctors.POH_DOCTORS.

Each doctor becomes one Care user, who can be posted at several centres. Their
specialty is a department with the POH website's exact name in that facility, their
photo is the POH website's image URL, and they get a public schedule so the
appointment pages and the mobile app list them. Seeded doctors can't sign in: they
have no usable password. Doctors already in Care are left alone; their current state
is written to a backup CSV first. Safe to run again: it only adds what is missing and
refreshes the seeded doctors' names, qualifications and photos.
"""

import csv
import re
from datetime import datetime, time, timedelta
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from care.emr.models import Availability, SchedulableResource, Schedule
from care.emr.models.organization import FacilityOrganization, FacilityOrganizationUser
from care.facility.models import Facility
from care.security.access_audit import assignment_value, record_access_event
from care.security.models import AccessAuditAction, RoleModel
from care.security.policy import apply_default_expiry
from care.users.models import User
from hmis.poh_doctors import POH_DOCTORS

USERNAME_PREFIX = "poh-"
MARKER = "Created by seed_poh_doctors"
# Longest first, so "Prof.(Dr.)" is not read as a name starting "Prof.(Dr.)".
PREFIXES = ("Prof.(Dr.)", "Dr.", "Dt.", "Ms.")
# Seeded doctors never sign in, so their numbers only need to be unique.
PHONE_BASE = 917000000000
# Monday is 0. Mon-Sat 09:00-13:00 in 15-minute slots, like the test seed.
WORK_DAYS = range(6)
DAY_START, DAY_END = time(9), time(13)
SLOT_MINUTES = 15
BACKUP_FIELDS = [
    "facility",
    "username",
    "prefix",
    "first_name",
    "last_name",
    "qualification",
    "phone_number",
    "email",
    "profile_picture_url",
    "is_active",
    "departments",
    "schedules",
]


def split_name(full_name):
    prefix = next((p for p in PREFIXES if full_name.startswith(p + " ")), None)
    rest = full_name[len(prefix) :].strip() if prefix else full_name.strip()
    *first, last = rest.split()
    if not first:
        return prefix, last, "", rest
    return prefix, " ".join(first), last, rest


def read_doctors():
    doctors = {}
    for entry in POH_DOCTORS:
        prefix, first, last, bare = split_name(entry["name"])
        username = USERNAME_PREFIX + re.sub(r"[^a-z0-9]+", "-", bare.lower()).strip("-")
        if username in doctors:
            msg = f"Two doctors in POH_DOCTORS share the username {username}"
            raise CommandError(msg)
        doctors[username] = {
            "name": entry["name"],
            "prefix": prefix,
            "first_name": first,
            "last_name": last,
            "qualification": entry["qualification"],
            "photo": entry["photo"],
            "postings": [
                {"facility": facility, "department": department, "designation": title}
                for facility, department, title in entry["postings"]
            ],
        }
    return doctors


def write_backup(path, facilities):
    """Every doctor already bookable in these facilities, as they are now."""
    resources = (
        SchedulableResource.objects.filter(
            facility__in=facilities, resource_type="practitioner", user__isnull=False
        )
        .select_related("facility", "user")
        .order_by("facility__name", "user__username")
    )
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=BACKUP_FIELDS)
        writer.writeheader()
        for resource in resources:
            user = resource.user
            departments = FacilityOrganizationUser.objects.filter(
                user=user, organization__facility=resource.facility
            ).values_list("organization__name", flat=True)
            schedules = Schedule.objects.filter(resource=resource).order_by(
                "valid_from"
            )
            writer.writerow(
                {
                    "facility": resource.facility.name,
                    "username": user.username,
                    "prefix": user.prefix,
                    "first_name": user.first_name,
                    "last_name": user.last_name,
                    "qualification": user.qualification,
                    "phone_number": user.phone_number,
                    "email": user.email,
                    "profile_picture_url": user.profile_picture_url,
                    "is_active": user.is_active,
                    "departments": "; ".join(sorted(departments)),
                    "schedules": "; ".join(
                        f"{s.name} {s.valid_from:%Y-%m-%d}..{s.valid_to:%Y-%m-%d}"
                        f"{' public' if s.is_public else ''}"
                        for s in schedules
                    ),
                }
            )
    return resources.count()


class Command(BaseCommand):
    help = (
        "Loads the POH consultants in hmis/poh_doctors.py: one user per doctor with "
        "the POH website photo URL, a department per specialty (POH names) in each "
        "facility they work at, and a public Mon-Sat 09:00-13:00 schedule. Writes a "
        "backup CSV of the doctors already there first. Dry run by default: it reports "
        "what it would do and changes nothing. Pass --apply to write."
    )

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes")
        parser.add_argument(
            "--backup",
            help="Where to write the backup of existing doctors "
            "(default /tmp/doctors_backup_<timestamp>.csv)",
        )
        parser.add_argument(
            "--valid-days",
            type=int,
            default=90,
            help="How many days new schedules run for (default 90)",
        )

    def handle(self, *args, **options):
        self.apply = options["apply"]
        doctors = read_doctors()
        names = {p["facility"] for d in doctors.values() for p in d["postings"]}
        facilities = {}
        for name in sorted(names):
            matches = list(Facility.objects.filter(name=name))
            if len(matches) != 1:
                msg = f'Expected one facility named "{name}", found {len(matches)}'
                raise CommandError(msg)
            facilities[name] = matches[0]
        role = RoleModel.objects.filter(name="Doctor").first()
        if role is None:
            raise CommandError('No role named "Doctor"')

        backup = options["backup"] or (
            f"/tmp/doctors_backup_{timezone.localtime():%Y%m%d_%H%M%S}.csv"  # noqa: S108
        )
        count = write_backup(backup, facilities.values())
        self.stdout.write(f"Backed up {count} existing doctor(s) to {backup}\n")

        today = timezone.localdate()
        self.valid_from = timezone.make_aware(datetime.combine(today, time.min))
        self.valid_to = timezone.make_aware(
            datetime.combine(today + timedelta(days=options["valid_days"]), time.max)
        )
        self.counts = dict.fromkeys(
            ("users", "updated", "departments", "memberships", "schedules"), 0
        )
        self.next_phone = PHONE_BASE

        with transaction.atomic():
            for username, doctor in sorted(doctors.items()):
                self.seed_doctor(username, doctor, facilities, role)
            if not self.apply:
                transaction.set_rollback(True)

        c = self.counts
        self.stdout.write(
            f"\n{len(doctors)} POH doctors: {c['users']} users created, "
            f"{c['updated']} updated, {c['departments']} departments created, "
            f"{c['memberships']} department memberships and {c['schedules']} "
            "schedules added."
        )
        if self.apply:
            self.stdout.write(self.style.SUCCESS("Done."))
        else:
            self.stdout.write("Dry run: nothing changed. Pass --apply to write.")

    def seed_doctor(self, username, doctor, facilities, role):
        fields = {
            "prefix": doctor["prefix"],
            "first_name": doctor["first_name"],
            "last_name": doctor["last_name"],
            "qualification": doctor["qualification"],
            "profile_picture_url": doctor["photo"],
        }
        user = User.objects.filter(username=username).first()
        if user is None:
            if User.check_username_exists(username):
                self.stdout.write(
                    f"! {doctor['name']}: username {username} belongs to a deleted "
                    "user; skipped"
                )
                return
            user = User(username=username, email=f"{username}@example.com", **fields)
            user.phone_number = self.free_phone()
            user.set_unusable_password()
            user.save()
            record_access_event(
                AccessAuditAction.user_created,
                None,
                target_user=user,
                new_value={"username": username, "is_service_account": False},
                reason=MARKER,
            )
            self.counts["users"] += 1
            self.stdout.write(f"+ {doctor['name']} ({username})")
        else:
            changed = [k for k, v in fields.items() if getattr(user, k) != v]
            if changed:
                for key in changed:
                    setattr(user, key, fields[key])
                # save() rather than update(), so Care's user cache is cleared.
                user.save(update_fields=changed)
                self.counts["updated"] += 1
            self.stdout.write(
                f"• {doctor['name']} ({username}) exists"
                + (f"; updated {', '.join(changed)}" if changed else "")
            )
        if not doctor["photo"]:
            self.stdout.write("    no photo on the POH website; initials will show")

        for posting in doctor["postings"]:
            facility = facilities[posting["facility"]]
            dept = self.department(facility, posting["department"])
            # Without a department the doctor is still bookable, just with no specialty.
            if (
                dept
                and not FacilityOrganizationUser.objects.filter(
                    organization=dept, user=user
                ).exists()
            ):
                member = FacilityOrganizationUser(
                    organization=dept,
                    user=user,
                    role=role,
                    meta={"designation": posting["designation"], "source": MARKER},
                )
                apply_default_expiry(member)
                member.save()
                record_access_event(
                    AccessAuditAction.role_assigned,
                    None,
                    target_user=user,
                    role=role,
                    scope=dept,
                    new_value=assignment_value(member),
                    reason=MARKER,
                )
                self.counts["memberships"] += 1
                self.stdout.write(f"    + {facility.name}: {dept.name}")

            resource, _ = SchedulableResource.objects.get_or_create(
                facility=facility, resource_type="practitioner", user=user
            )
            if not Schedule.objects.filter(resource=resource).exists():
                schedule = Schedule.objects.create(
                    resource=resource,
                    name="OPD (POH)",
                    valid_from=self.valid_from,
                    valid_to=self.valid_to,
                    is_public=True,
                )
                Availability.objects.create(
                    schedule=schedule,
                    name="Morning",
                    slot_type="appointment",
                    slot_size_in_minutes=SLOT_MINUTES,
                    tokens_per_slot=1,
                    reason="",
                    availability=[
                        {
                            "day_of_week": day,
                            "start_time": DAY_START.isoformat(),
                            "end_time": DAY_END.isoformat(),
                        }
                        for day in WORK_DAYS
                    ],
                )
                self.counts["schedules"] += 1
                self.stdout.write(f"    + {facility.name}: schedule")

    def department(self, facility, name):
        """The facility's top-level department with this name, created if missing."""
        top_level = FacilityOrganization.objects.filter(
            facility=facility, name=name, level_cache=0, root_org__isnull=True
        )
        dept = top_level.filter(org_type="dept").first()
        if dept:
            return dept
        clash = top_level.first()
        if clash:
            # Care keeps top-level names unique across organization types.
            self.stdout.write(
                f'    ! {facility.name}: "{name}" is already a {clash.org_type} '
                "organization, so the doctor gets no specialty there"
            )
            return None
        dept = FacilityOrganization(
            facility=facility, name=name, org_type="dept", description=MARKER
        )
        dept.save()
        self.counts["departments"] += 1
        self.stdout.write(f'    + {facility.name}: department "{name}"')
        return dept

    def free_phone(self):
        while True:
            self.next_phone += 1
            phone = f"+{self.next_phone}"
            if not User.objects.filter(phone_number=phone).exists():
                return phone
