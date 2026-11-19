"""UAT seed: three Delhi facilities (Lajpat Nagar, Karol Bagh, Dwarka) with
staff, schedules, desk-registered patients of every kind, appointments,
encounters, emergency arrivals, bills and outreach camps.

    python manage.py load_fixtures --path care/fixtures/scripts/uat_fixtures.py

Everything goes through the APIs the frontend uses, so the desk, billing and
scheduling rules run as they do in the app. The run is one transaction: any
failure leaves the database as it was (uploaded sample documents may stay in
the bucket). It refuses to run when one of the three facilities or unit codes
already exists, so run it once on a fresh database.

Settings (environment):
    UAT_PASSWORD   password of every seeded staff user (default Poh@Uat2026)
    UAT_PATIENTS   general desk patients per facility on top of the special
                   cases (default 24)

It runs as its own superuser, uat-seed, with an unusable password, so it
never resets anyone's admin password. All people are synthetic (Faker),
uploaded documents are labelled SYNTHETIC UAT SAMPLE, and no SMS is sent:
the SMS backend is the console for the run and phone-booking reminders are
marked as sent. See docs/uat-data.md in the frontend repo.
"""

import io
import os
from datetime import datetime, timedelta
from secrets import choice, randbelow

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone

from care.emr.models import Encounter, Patient
from care.emr.models.charge_item_definition import ChargeItemDefinition
from care.emr.models.resource_category import ResourceCategory
from care.emr.models.scheduling.booking import TokenBooking
from care.emr.models.scheduling.schedule import Schedule
from care.emr.resources.account.default_account import get_default_account
from care.emr.resources.organization.spec import OrganizationTypeChoices
from care.facility.models import Facility
from care.fixtures.base import FixtureError, generate_phone_number
from care.fixtures.context import care_fixture_context
from care.fixtures.poh import ALLERGIES, name_part
from care.security.models import PermissionModel, RoleModel, RolePermission
from care.utils.time_util import care_now
from hmis.billing import ensure_definition, setup_billing, slug_for
from hmis.constants import PMJAY, STAFF_DEPENDANT, STAFF_SELF
from hmis.lab_catalogue import seed_lab_catalogue
from hmis.pincodes import lookup
from hmis.specs import LAST_NAME

PASSWORD = os.environ.get("UAT_PASSWORD", "Poh@Uat2026")
GENERAL_PATIENTS = int(os.environ.get("UAT_PATIENTS", "24"))
SEED_USER = "uat-seed"
EMAIL_DOMAIN = "uat.poh.test"

# (name, unit code, district, PIN, latitude, longitude, police station,
#  local addresses as (PIN, locality, district))
FACILITIES = [
    (
        "Lajpat Nagar",
        "LN",
        "South East Delhi",
        "110024",
        28.5677,
        77.2433,
        "Lajpat Nagar Police Station",
        [
            ("110024", "Lajpat Nagar", "South East Delhi"),
            ("110014", "Jangpura", "South East Delhi"),
            ("110065", "East of Kailash", "South East Delhi"),
            ("110048", "Greater Kailash", "South Delhi"),
        ],
    ),
    (
        "Karol Bagh",
        "KB",
        "Central Delhi",
        "110005",
        28.6519,
        77.1909,
        "Karol Bagh Police Station",
        [
            ("110005", "Karol Bagh", "Central Delhi"),
            ("110060", "Rajinder Nagar", "Central Delhi"),
            ("110008", "Patel Nagar", "Central Delhi"),
            ("110055", "Paharganj", "Central Delhi"),
        ],
    ),
    (
        "Dwarka",
        "DW",
        "South West Delhi",
        "110075",
        28.5921,
        77.0460,
        "Dwarka Sector 9 Police Station",
        [
            ("110075", "Dwarka Sector 6", "South West Delhi"),
            ("110077", "Dwarka Sector 12", "South West Delhi"),
            ("110078", "Dwarka Sector 23", "South West Delhi"),
            ("110045", "Palam", "South West Delhi"),
        ],
    ),
]

# OPD departments with a doctor each: (department, username suffix).
OPD = [
    ("General Medicine", "gm"),
    ("Cardiology", "cardio"),
    ("Orthopedics", "ortho"),
    ("Pediatrics", "paeds"),
    ("Obstetrics & Gynecology", "obg"),
    ("Dermatology", "derma"),
    ("ENT", "ent"),
    ("Endocrinology", "endo"),
]
SUPPORT_DEPARTMENTS = ["Emergency", "Laboratory", "Pharmacy"]
COMPLAINTS = [
    "Fever for three days",
    "Knee pain on climbing stairs",
    "Follow-up of blood sugar",
    "Skin rash on both arms",
    "Ear pain and blocked ear",
    "Routine antenatal check",
    "Chest discomfort on exertion",
    "Cough and cold",
    "Back pain after lifting",
    "Thyroid report review",
]
LANGUAGES = ["Hindi", "English", "Punjabi", "Bengali", "Urdu"]
CORPORATES = ["Northwind Synthetic Pvt Ltd", "Contoso Logistics India Ltd"]
INSURERS = ["Medi Assist TPA", "Paramount Health TPA", "Star Health Insurance"]
VIP_ROLE = "Front Office (VIP)"
SUNDAY = 6
CON1 = "CON1"


def log(message):
    print(message)  # noqa: T201


def local_place(addresses):
    """A Delhi address that passes the desk's PIN check: when the PIN
    directory lists the PIN its post office, district and state are used."""
    pincode, locality, district = choice(addresses)
    state = "Delhi"
    listed = lookup(pincode)
    if listed:
        locality = choice(listed["localities"])
        district, state = listed["district"], listed["state"]
    return {
        "pincode": pincode,
        "house": f"{choice(['H.No.', 'Flat', 'Plot'])} {randbelow(400) + 1}",
        "street": f"Block {choice('ABCDEFGHJK')}, Gali No. {randbelow(20) + 1}",
        "locality": locality,
        "city": "New Delhi",
        "district": district,
        "state": state,
    }


def address_line(place):
    return ", ".join(
        place[key]
        for key in ("house", "street", "locality", "city", "district", "state")
        if place.get(key)
    )


def born_between(fake, low, high):
    return fake.date_of_birth(minimum_age=low, maximum_age=high)


class Seeder:
    def __init__(self, base, geo):
        self.base = base
        self.fake = base.fake
        self.geo = geo
        self.now = timezone.now()
        self.today = timezone.localdate()
        self.roles = base.get_roles()
        self.credentials = []
        self.counts = {}
        self.warnings = []

    def count(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n

    # People ---------------------------------------------------------------

    def person(
        self,
        ctx,
        *,
        gender,
        ages=None,
        minor=False,
        no_phone=False,
        phone=None,
        mobile_owner=None,
        last_name=None,
        married=None,
        **registration,
    ):
        """A desk registration body (patient, registration), as the frontend
        builds it. Keyword arguments override registration fields."""
        fake = self.fake
        first_name = name_part(
            fake.first_name_male if gender == "male" else fake.first_name_female
        )
        last_name = last_name or name_part(fake.last_name, LAST_NAME)
        place = local_place(ctx["addresses"])
        if not no_phone and phone is None:
            phone = generate_phone_number()
        language = choice(LANGUAGES)
        allergic = randbelow(4) == 0
        if minor:
            born = born_between(fake, *(ages or (2, 16)))
            parent = f"{name_part(fake.first_name_male)} {last_name}"
            details = {
                "title": "Master" if gender == "male" else "Ms",
                "guardian_name": parent,
                "guardian_relationship": "S/O" if gender == "male" else "D/O",
                "next_of_kin_title": "Mr",
                "next_of_kin_name": parent,
                "next_of_kin_relationship": "Father",
                "next_of_kin_phone": phone or generate_phone_number(),
                "occupation": "Student",
                "consent_given_by": "Parent",
                "father_name": parent,
            }
            owner = mobile_owner or "Father"
        else:
            born = born_between(fake, *(ages or (19, 75)))
            married = choice([True, True, False]) if married is None else married
            details = {
                "title": "Mr" if gender == "male" else ("Mrs" if married else "Ms"),
                "marital_status": "married" if married else "single",
                "occupation": choice(
                    ["Engineer", "Teacher", "Retired", "Business", "Homemaker"]
                ),
                "consent_given_by": "The patient",
            }
            owner = mobile_owner or "Self"
        body = {
            "registration_type": "DOM",
            "first_name": first_name,
            "last_name": last_name,
            "preferred_language": language,
            "mobile_owner": "No phone" if no_phone else owner,
            "whatsapp_number": phone,
            "house": place["house"],
            "street": place["street"],
            "locality": place["locality"],
            "city": place["city"],
            "district": place["district"],
            "state": place["state"],
            "nationality_category": "Indian resident",
            "identity_storage_consent": True,
            "identity_card_type": "aadhaar",
            "identity_number_last4": f"{randbelow(10000):04d}",
            "abha_status": "not_now",
            "allergy_status": "has" if allergic else "nka",
            "allergies": [choice(ALLERGIES)] if allergic else [],
            "payer_type": "Self",
            "arrival_source": choice(["Walk-in", "Walk-in", "Our own doctor"]),
            "consent_method": "Signature",
            "consent_language": language,
            "general_consent": True,
            "data_processing_consent": True,
            "tariff_consent": True,
            "communication_channels": [] if no_phone else ["SMS", "WhatsApp"],
            **details,
            **registration,
        }
        line = address_line(place)
        patient = {
            "name": f"{first_name} {last_name}",
            "gender": gender,
            "date_of_birth": born.isoformat(),
            "pincode": int(place["pincode"]),
            "address": line,
            "permanent_address": line,
            "blood_group": choice(
                [
                    "A_positive",
                    "A_negative",
                    "B_positive",
                    "O_positive",
                    "O_negative",
                    "AB_positive",
                    "unknown",
                ]
            ),
            "geo_organization": ctx["geo"],
        }
        if phone:
            patient["phone_number"] = phone
        return patient, body

    def register(self, ctx, patient, registration, kind, **extra):
        response = self.base.register_desk_patient(
            ctx["facility"].id, patient, registration, **extra
        )
        self.count(kind)
        record = response.patient
        # Pre-registered patients have no UHID yet, so they get no visits.
        if not extra.get("pre_registration"):
            ctx["patients"].append(record)
        return record

    def document(self, category, side="single", label=""):
        """Stages a synthetic document image; returns its id."""
        url = "/api/hmis/documents/"
        upload = SimpleUploadedFile(
            f"uat-{category}-{side}.png",
            sample_image(f"{category.replace('_', ' ')} ({side}) {label}".strip()),
            content_type="image/png",
        )
        response = self.base.client.post(
            url,
            {
                "file": upload,
                "category": category,
                "side": side,
                "capture_method": "scanned",
            },
            format="multipart",
        )
        if response.status_code not in (200, 201):
            msg = f"POST {url} failed ({response.status_code}): {response.data}"
            raise FixtureError(msg)
        self.count("documents")
        return response.data["id"]

    def payer_card(self, label):
        return [
            self.document("payer_card", "front", label),
            self.document("payer_card", "back", label),
        ]

    # Facility set-up ------------------------------------------------------

    def facility(self, spec):
        name, code, district, pincode, lat, lng, police, addresses = spec
        base = self.base
        facility = base.create_facility(
            self.geo["Delhi"].id,
            name=name,
            description=f"Pacific OneHealth {name} (UAT)",
            facility_type="Private Hospital",
            address=f"Pacific OneHealth, {name}, New Delhi",
            pincode=int(pincode),
            latitude=lat,
            longitude=lng,
            is_public=True,
        )
        base.set_unit_code(facility.id, code)
        base.set_emergency_desk(facility.id, generate_phone_number())
        obj = Facility.objects.get(external_id=facility.id)
        setup_billing(obj)
        added = seed_lab_catalogue(obj, user=base.user)
        self.count("lab tests", added)
        ctx = {
            "facility": facility,
            "obj": obj,
            "code": code,
            "prefix": code.lower(),
            "geo": self.geo[district].id,
            "police": police,
            "addresses": addresses,
            "patients": [],
            "doctors": {},
            "users": {},
        }
        existing = {o.name: o for o in base.get_facility_organizations(facility.id)}
        departments = {}
        departments["Administration"] = existing.get(
            "Administration"
        ) or base.create_facility_organization(
            facility.id, name="Administration", org_type="dept"
        )
        for dept in [*(d for d, _ in OPD), *SUPPORT_DEPARTMENTS]:
            departments[dept] = existing.get(dept) or base.create_facility_organization(
                facility.id, name=dept, org_type="dept"
            )
        ctx["departments"] = departments
        self.consultation_fee(obj)
        log(f"  {name} ({code}): facility, unit code, billing, lab catalogue")
        return ctx

    def consultation_fee(self, facility):
        """The catalogue has no first-consultation line (CON2 is a referred
        consultation), so the seed adds CON1 at ₹800 in the consultation
        head for the doctors' schedules, with FU as the 7-day revisit."""
        category = ResourceCategory.objects.filter(
            facility=facility, slug__endswith=slug_for("H1")
        ).first()
        ensure_definition(
            facility,
            category,
            CON1,
            "OPD consultation",
            800,
            "First OPD consultation (UAT seed)",
        )

    # Staff ----------------------------------------------------------------

    def user(self, username, role, orgs, ctx=None, gender=None, title=""):
        base = self.base
        gender = gender or choice(["male", "female"])
        first = name_part(
            self.fake.first_name_male
            if gender == "male"
            else self.fake.first_name_female
        )
        last = name_part(self.fake.last_name, LAST_NAME)
        user = base.create_user(
            self.geo["Delhi"].id,
            username=username,
            email=f"{username}@{EMAIL_DOMAIN}",
            password=PASSWORD,
            first_name=first,
            last_name=last,
            gender=gender,
            prefix=title or None,
        )
        role_obj = self.roles[role] if isinstance(role, str) else role
        for facility_id, org in orgs:
            base.add_user_to_facility_organization(
                facility_id, org.id, user.id, role_obj.id
            )
        where = ctx["facility"].name if ctx else "All three facilities"
        self.credentials.append((username, role_obj.name, where))
        self.count("staff users")
        return user

    def vip_role(self):
        """A cloned front-office role that can see VIP names: no built-in role
        has can_reveal_vip_name (docs/access-control.md)."""
        source = self.roles["Staff"]
        self.base.post(f"/api/v1/role/{source.id}/clone/", {"name": VIP_ROLE})
        role = RoleModel.objects.get(name=VIP_ROLE)
        RolePermission.objects.get_or_create(
            role=role,
            permission=PermissionModel.objects.get(slug="can_reveal_vip_name"),
        )
        self.roles = self.base.get_roles()
        return self.roles[VIP_ROLE]

    def staff(self, ctx, vip_role):
        f = ctx["facility"].id
        p = ctx["prefix"]
        d = ctx["departments"]
        admin = d["Administration"]
        users = ctx["users"]
        users["fac-admin"] = self.user(
            f"{p}-facadmin", "Facility Admin", [(f, admin)], ctx
        )
        users["desk"] = self.user(f"{p}-desk", "Staff", [(f, admin)], ctx)
        users["desk2"] = self.user(f"{p}-desk2", "Staff", [(f, admin)], ctx)
        users["desk-vip"] = self.user(f"{p}-desk-vip", vip_role, [(f, admin)], ctx)
        users["nurse"] = self.user(
            f"{p}-nurse",
            "Nurse",
            [(f, d["Emergency"]), (f, d["General Medicine"])],
            ctx,
            gender="female",
        )
        users["pharmacist"] = self.user(
            f"{p}-pharmacist", "Pharmacist", [(f, d["Pharmacy"])], ctx
        )
        users["lab"] = self.user(f"{p}-lab", "Staff", [(f, d["Laboratory"])], ctx)
        users["camp"] = self.user(f"{p}-camp", "Camp Staff", [(f, admin)], ctx)
        users["er-doctor"] = self.user(
            f"{p}-dr-er", "Doctor", [(f, d["Emergency"])], ctx, title="Dr"
        )
        for dept, suffix in OPD:
            ctx["doctors"][dept] = self.user(
                f"{p}-dr-{suffix}",
                "Doctor",
                [(f, d[dept]), (f, admin)],
                ctx,
                gender="female" if dept == "Obstetrics & Gynecology" else None,
                title="Dr",
            )

    # Schedules ------------------------------------------------------------

    def schedules(self, ctx):
        base = self.base
        f = ctx["facility"].id
        fee = ChargeItemDefinition.calculate_slug_from_facility(
            ctx["obj"].external_id, slug_for(CON1)
        )
        follow_up = ChargeItemDefinition.calculate_slug_from_facility(
            ctx["obj"].external_id, slug_for("FU")
        )
        # Care refuses a schedule that starts in the past, so start it now.
        valid_from = (
            (care_now() + timedelta(minutes=1)).replace(tzinfo=None, microsecond=0)
        ).isoformat()
        valid_to = (self.today + timedelta(days=90)).isoformat() + "T23:59:59"
        ctx["schedules"] = {}
        for idx, (dept, doctor) in enumerate(ctx["doctors"].items()):
            availabilities = [
                {
                    "name": "Morning OPD",
                    "slot_type": "appointment",
                    "slot_size_in_minutes": 15,
                    "tokens_per_slot": 2,
                    "reason": "",
                    "availability": [
                        {"day_of_week": day, "start_time": "09:00", "end_time": "13:00"}
                        for day in range(6)
                    ],
                },
            ]
            if idx % 2 == 0:
                availabilities.append(
                    {
                        "name": "Evening OPD",
                        "slot_type": "appointment",
                        "slot_size_in_minutes": 20,
                        "tokens_per_slot": 1,
                        "reason": "",
                        "availability": [
                            {
                                "day_of_week": day,
                                "start_time": "17:00",
                                "end_time": "20:00",
                            }
                            for day in range(5)
                        ],
                    }
                )
            schedule = base.create_schedule(
                f,
                resource_type="practitioner",
                resource_id=doctor.id,
                name=f"{dept} OPD",
                valid_from=valid_from,
                valid_to=valid_to,
                availabilities=availabilities,
            )
            base.post(
                f"/api/v1/facility/{f}/schedule/{schedule.id}/set_charge_item_definition/",
                {
                    "charge_item_definition": fee,
                    "re_visit_allowed_days": 7,
                    "re_visit_charge_item_definition": follow_up,
                },
            )
            if idx % 3 == 0:
                morning = next(
                    a for a in schedule.availabilities if a.name == "Morning OPD"
                )
                base.post(
                    f"/api/v1/facility/{f}/schedule/{schedule.id}/availability/"
                    f"{morning.id}/set_walk_in_reserve/",
                    {"walk_in_every": choice([3, 4])},
                )
            ctx["schedules"][dept] = schedule
            self.count("doctor schedules")
        # Care's slot lookup only covers a day whose midnight is inside the
        # schedule, and the API refuses a start in the past. Move the start
        # back to midnight so today's sessions can be booked.
        Schedule.objects.filter(
            resource__facility=ctx["obj"], valid_from__date=self.today
        ).update(
            valid_from=timezone.make_aware(
                datetime.combine(self.today, datetime.min.time())
            )
        )

    # Patients -------------------------------------------------------------

    def patients(self, ctx):  # noqa: PLR0915
        fake = self.fake
        today = self.today
        valid_till = (today + timedelta(days=300)).isoformat()

        # General desk patients (Self payer), a few with concessions.
        for i in range(GENERAL_PATIENTS):
            gender = choice(["male", "female"])
            if i % 8 == 0:
                patient, reg = self.person(ctx, gender=gender, minor=True)
            elif i % 8 == 1:
                patient, reg = self.person(
                    ctx,
                    gender=gender,
                    ages=(62, 85),
                    concessions=["Senior citizen — 15%"],
                    occupation="Retired",
                )
            else:
                patient, reg = self.person(ctx, gender=gender)
            self.register(ctx, patient, reg, "desk patients")

        # No phone: registered on the emergency desk number.
        patient, reg = self.person(ctx, gender="male", ages=(60, 80), no_phone=True)
        ctx["no_phone"] = self.register(ctx, patient, reg, "no-phone patients")

        # A family on one phone: husband, wife and child.
        phone = generate_phone_number()
        surname = name_part(fake.last_name, LAST_NAME)
        family = [
            self.person(
                ctx,
                gender="male",
                ages=(35, 50),
                phone=phone,
                last_name=surname,
                married=True,
            ),
            self.person(
                ctx,
                gender="female",
                ages=(32, 45),
                phone=phone,
                last_name=surname,
                married=True,
                mobile_owner="Spouse",
            ),
            self.person(
                ctx,
                gender="female",
                minor=True,
                phone=phone,
                last_name=surname,
                mobile_owner="Father",
            ),
        ]
        for patient, reg in family:
            self.register(ctx, patient, reg, "family (shared phone)")

        # EWS concession, approved.
        patient, reg = self.person(
            ctx,
            gender="female",
            ages=(30, 55),
            concessions=["EWS / charity"],
            concession_approved_by="Medical Superintendent",
        )
        self.register(ctx, patient, reg, "concession patients")

        # Corporate.
        patient, reg = self.person(
            ctx,
            gender="male",
            ages=(25, 55),
            registration_type="CORP",
            payer_type="Corporate",
            payer_name=choice(CORPORATES),
            employee_id=f"EMP-{randbelow(90000) + 10000}",
            policy_holder_relationship="Self",
            arrival_source="Corporate tie-up",
        )
        self.register(
            ctx,
            patient,
            reg,
            "payer patients",
            documents=self.payer_card("corporate card"),
        )

        # Insurance / TPA.
        patient, reg = self.person(
            ctx,
            gender="female",
            ages=(28, 60),
            registration_type="TPA",
            payer_type="Insurance/TPA",
            payer_name=choice(INSURERS),
            policy_number=f"POL/{today.year}/{randbelow(900000) + 100000}",
            policy_valid_till=valid_till,
            policy_holder_relationship="Self",
            arrival_source="Insurance network",
        )
        self.register(
            ctx,
            patient,
            reg,
            "payer patients",
            documents=self.payer_card("insurance card"),
        )

        # Government schemes: CGHS (with validity) and PM-JAY (no validity).
        patient, reg = self.person(
            ctx,
            gender="male",
            ages=(60, 80),
            registration_type="GOVT",
            payer_type="Govt scheme",
            payer_name="CGHS",
            policy_number=f"CGHS{randbelow(9000000) + 1000000}",
            policy_valid_till=valid_till,
            policy_holder_relationship="Self",
            occupation="Retired",
        )
        self.register(
            ctx, patient, reg, "payer patients", documents=self.payer_card("CGHS card")
        )
        patient, reg = self.person(
            ctx,
            gender="female",
            ages=(30, 55),
            married=True,
            registration_type="GOVT",
            payer_type="Govt scheme",
            payer_name=PMJAY,
            policy_number=f"PMJAY{randbelow(90000000) + 10000000}",
            policy_holder_relationship="Spouse",
            occupation="Homemaker",
        )
        self.register(
            ctx,
            patient,
            reg,
            "payer patients",
            documents=self.payer_card("PM-JAY card"),
        )

        # Staff (self) and a staff dependant child.
        employee = f"POH-{ctx['code']}-{randbelow(9000) + 1000}"
        patient, reg = self.person(
            ctx,
            gender="female",
            ages=(25, 50),
            registration_type="STAFF",
            payer_type="Staff",
            payer_name=STAFF_SELF,
            staff_category="Nurse",
            employee_id=employee,
        )
        self.register(
            ctx, patient, reg, "payer patients", documents=self.payer_card("staff ID")
        )
        patient, reg = self.person(
            ctx,
            gender="male",
            minor=True,
            registration_type="STAFF",
            payer_type="Staff",
            payer_name=STAFF_DEPENDANT,
            employee_id=employee,
            policy_holder_relationship="Child",
        )
        self.register(
            ctx,
            patient,
            reg,
            "payer patients",
            documents=self.payer_card("staff dependant card"),
        )

        # International patient (foreign national, medical visa).
        passport = f"EB{randbelow(9000000) + 1000000}"
        hotel = local_place(ctx["addresses"])
        patient, reg = self.person(
            ctx,
            gender="male",
            ages=(30, 65),
            phone="+8801712" + f"{randbelow(900000) + 100000}",
            registration_type="INT",
            nationality_category="Foreign",
            nationality=choice(["Bangladesh", "Afghanistan", "Iraq", "Uzbekistan"]),
            passport_number=passport,
            identity_card_type="passport",
            identity_number_last4=passport[-4:],
            visa_type="Medical (MED)",
            visa_expiry=(today + timedelta(days=90)).isoformat(),
            address_in_india=f"Guest house, {address_line(hotel)}"[:300],
            alternate_phone_number=generate_phone_number(),
            alternate_phone_owner="Relative",
            payer_type="International",
            international_tariff_consent=True,
            preferred_language="English",
            consent_language="English",
        )
        self.register(
            ctx,
            patient,
            reg,
            "international patients",
            documents=[
                self.document("passport", "single", "foreign"),
                self.document("visa", "single", "medical visa"),
            ],
        )

        # NRI (Indian passport, home in the UAE).
        passport = f"Z{randbelow(9000000) + 1000000}"
        patient, reg = self.person(
            ctx,
            gender="female",
            ages=(30, 60),
            phone="+97150" + f"{randbelow(9000000) + 1000000}",
            registration_type="NRI",
            nationality_category="NRI",
            nationality="India",
            passport_number=passport,
            identity_card_type="passport",
            identity_number_last4=passport[-4:],
            address_in_india=address_line(local_place(ctx["addresses"])),
            alternate_phone_number=generate_phone_number(),
            alternate_phone_owner="Parent",
            permanent_country="United Arab Emirates",
            international_tariff_consent=True,
        )
        self.register(ctx, patient, reg, "international patients")

        # VIP: only the Front Office (VIP) role sees the name.
        patient, reg = self.person(
            ctx,
            gender="male",
            ages=(45, 70),
            is_vip=True,
            vip_note="Public figure: keep the name masked",
            vip_authorised_by="Medical Superintendent",
            vip_protocol_consent=True,
        )
        ctx["vip"] = self.register(ctx, patient, reg, "VIP patients")

        # IVF / ART couple.
        surname = name_part(fake.last_name, LAST_NAME)
        patient, reg = self.person(
            ctx,
            gender="female",
            ages=(28, 38),
            married=True,
            last_name=surname,
            registration_type="CPL",
        )
        partner_born = born_between(fake, 30, 42)
        couple = {
            "single_woman": False,
            "art_consent": True,
            "partner": {
                "first_name": name_part(fake.first_name_male),
                "last_name": surname,
                "date_of_birth": partner_born.isoformat(),
                "phone_number": generate_phone_number(),
                "identity_card_type": "aadhaar",
                "identity_number_last4": f"{randbelow(10000):04d}",
                "documents": [self.document("government_id", "front", "partner")],
            },
        }
        self.register(
            ctx,
            patient,
            reg,
            "IVF couples",
            couple=couple,
            documents=[
                self.document("government_id", "front", "woman"),
                self.document("marriage_certificate", "single"),
            ],
        )

        # A likely duplicate: the same person registered twice.
        patient, reg = self.person(ctx, gender="female", ages=(40, 60))
        self.register(ctx, patient, reg, "duplicate pairs")
        self.register(ctx, dict(patient), dict(reg), "duplicate pairs")

        # Pre-registrations (PRE- number until they arrive).
        for _ in range(2):
            patient, reg = self.person(
                ctx, gender=choice(["male", "female"]), arrival_source="Call centre"
            )
            self.register(ctx, patient, reg, "pre-registrations", pre_registration=True)

    def quick_registrations(self, ctx):
        for dept in ("General Medicine", "Dermatology"):
            gender = choice(["male", "female"])
            self.base.post(
                "/api/hmis/registration/quick/",
                {
                    "facility": ctx["facility"].id,
                    "title": "Mr" if gender == "male" else "Ms",
                    "first_name": name_part(
                        self.fake.first_name_male
                        if gender == "male"
                        else self.fake.first_name_female
                    ),
                    "last_name": name_part(self.fake.last_name, LAST_NAME),
                    "date_of_birth": born_between(self.fake, 20, 60).isoformat(),
                    "gender": gender,
                    "phone_number": generate_phone_number(),
                    "consultation_type": "in_person",
                    "doctor": ctx["doctors"][dept].id,
                    "whatsapp_same_as_mobile": True,
                    # No SMS from a seed.
                    "send_form_link": False,
                    "send_payment_reminder": False,
                },
            )
            self.count("quick registrations")

    # Visits ---------------------------------------------------------------

    def past_encounters(self, ctx, patients):
        """Completed OPD visits over the last three months."""
        depts = list(ctx["doctors"])
        for patient in patients:
            for _ in range(1 + randbelow(2)):
                dept = choice(depts)
                start = self.now - timedelta(days=3 + randbelow(85), hours=randbelow(6))
                end = start + timedelta(minutes=20)
                encounter = self.base.create_encounter(
                    patient.id,
                    ctx["facility"].id,
                    organizations=[ctx["departments"][dept].id],
                    status="completed",
                    encounter_class="amb",
                    priority="routine",
                    period={"start": start.isoformat(), "end": end.isoformat()},
                )
                Encounter.objects.filter(external_id=encounter.id).update(
                    created_date=start
                )
                self.count("past OPD encounters")

    def restricted(self, ctx, patient):
        """An open encounter with a statutory flag: the patient shows as an
        alias (PT-…) to everyone without break-glass."""
        encounter = self.base.create_encounter(
            patient.id,
            ctx["facility"].id,
            organizations=[ctx["departments"]["Obstetrics & Gynecology"].id],
            status="in_progress",
            encounter_class="amb",
            priority="routine",
        )
        self.base.post(
            f"/api/hmis/patients/{patient.id}/restriction/flags/",
            {"encounter": encounter.id, "category": "mental_health"},
        )
        self.count("restricted patients")

    def emergencies(self, ctx):
        base = self.base
        f = ctx["facility"].id
        common = {"organizations": [ctx["departments"]["Emergency"].id]}
        base.register_emergency_patient(
            f,
            {"name": "Unknown", "gender": "male", "age": 35},
            triage="red",
            is_medico_legal=True,
            mlc_nature="Road accident",
            police_station=ctx["police"],
            distinguishing_marks="Scar on left forearm",
            is_unidentified=True,
            brought_by={"name": "CATS ambulance"},
            **common,
        )
        base.register_emergency_patient(
            f,
            {"name": "Unknown", "gender": "female", "age": 70},
            triage="green",
            distinguishing_marks="Wearing a green shawl",
            is_unidentified=True,
            brought_by={"name": "Passer-by", "phone_number": generate_phone_number()},
            **common,
        )
        for triage, complaint, gender, age in (
            ("yellow", "Chest pain", "male", 56),
            ("red", "Breathlessness", "female", 64),
            ("green", "Fall", "male", 24),
        ):
            first = name_part(
                self.fake.first_name_male
                if gender == "male"
                else self.fake.first_name_female
            )
            last = name_part(self.fake.last_name, LAST_NAME)
            base.register_emergency_patient(
                f,
                {
                    "name": f"{first} {last}",
                    "gender": gender,
                    "age": age,
                    "geo_organization": ctx["geo"],
                    "registration": {
                        "prefix": "Mr" if gender == "male" else "Mrs",
                        "first_name": first,
                        "last_name": last,
                    },
                },
                triage=triage,
                is_unidentified=False,
                chief_complaint=complaint,
                mode_of_arrival=choice(["Walked in", "108 ambulance"]),
                brought_by={
                    "name": name_part(self.fake.first_name_female),
                    "phone_number": generate_phone_number(),
                    "relationship": "Spouse",
                },
                **common,
            )
        self.count("emergency arrivals", 5)

    def newborn(self, ctx, mother):
        self.base.post(
            "/api/hmis/registration/newborn/",
            {
                "facility": ctx["facility"].id,
                "mother": mother.id,
                "delivery_type": "Normal vaginal",
                "gestation_weeks": 39,
                "babies": [
                    {
                        "gender": choice(["male", "female"]),
                        "born_at": (self.now - timedelta(hours=20)).isoformat(),
                        "birth_weight_grams": 2900 + randbelow(600),
                        "apgar_1_min": 8,
                        "apgar_5_min": 9,
                    }
                ],
            },
        )
        self.count("newborns")

    # Appointments ---------------------------------------------------------

    def open_slots(self, ctx, doctor, day):
        response = self.base.get_slots_for_day(
            ctx["facility"].id, "practitioner", doctor.id, day.isoformat()
        )
        soon = self.now + timedelta(minutes=10)
        return [
            slot
            for slot in response.get("results", [])
            if datetime.fromisoformat(str(slot.start_datetime)) > soon
            and slot.allocated < slot.availability.tokens_per_slot
        ]

    def book(self, ctx, slot, patient, **details):
        return self.base.post(
            f"/api/v1/facility/{ctx['facility'].id}/slots/{slot.id}/create_appointment/",
            {
                "patient": patient.id,
                "note": "",
                "booking_channel": details.get(
                    "channel", choice(["front_desk", "walk_in", "phone", "online"])
                ),
                "consultation_type": details.get(
                    "consultation_type", choice(["in_person"] * 5 + ["video"])
                ),
                "visit_purpose": details.get(
                    "visit_purpose",
                    choice(["new", "new", "follow_up", "report_review"]),
                ),
                "chief_complaint": choice(COMPLAINTS),
            },
        )

    def appointments(self, ctx, patients):
        """Bookings today and over the next week. Each patient books once a
        day at most, so no two bookings of theirs clash."""
        f = ctx["facility"].id
        url = f"/api/v1/facility/{f}/appointments"
        days = [self.today + timedelta(days=n) for n in range(8)]
        days = [d for d in days if d.weekday() != SUNDAY]  # no OPD on Sundays
        pool = list(patients)
        booked = []
        for day in days:
            day_patients = pool[:]
            for doctor in ctx["doctors"].values():
                slots = self.open_slots(ctx, doctor, day)
                wanted = 3 if day == self.today else 2
                for slot in slots[::3][:wanted]:
                    if not day_patients:
                        break
                    patient = day_patients.pop(randbelow(len(day_patients)))
                    booking = self.book(ctx, slot, patient)
                    booked.append((day, doctor, booking))
                    self.count("appointments")
        if not booked:
            self.warnings.append(f"{ctx['facility'].name}: no open slots to book")
            return

        today = [b for d, _, b in booked if d == self.today]
        later = [(d, doc, b) for d, doc, b in booked if d != self.today]
        # Today's desk: some arrived, some checked in.
        for booking, status in zip(
            today, ["checked_in", "checked_in", "arrived"], strict=False
        ):
            self.base.put(f"{url}/{booking.id}/", {"status": status, "note": ""})
            self.count(f"appointments {status}")
        # Cancellations and a reschedule among the later ones.
        for _, _, booking in later[:2]:
            self.base.post(
                f"{url}/{booking.id}/cancel/",
                {"reason": "cancelled", "note": "Patient called to cancel"},
            )
            self.count("appointments cancelled")
        for slot_day, doctor, booking in later[2:3]:
            for offset in (1, 2, 3):
                target = slot_day + timedelta(days=offset)
                # Skip times the patient already has a booking at.
                taken = set(
                    TokenBooking.objects.filter(
                        patient__external_id=booking.patient.id,
                        status="booked",
                    ).values_list("token_slot__start_datetime", flat=True)
                )
                candidates = [
                    slot
                    for slot in self.open_slots(ctx, doctor, target)
                    if datetime.fromisoformat(str(slot.start_datetime)) not in taken
                ]
                if candidates:
                    self.base.post(
                        f"{url}/{booking.id}/reschedule/",
                        {
                            "new_slot": candidates[-1].id,
                            "new_booking_note": "Rescheduled at the patient's request",
                            "previous_booking_note": "Patient asked for a later day",
                        },
                    )
                    self.count("appointments rescheduled")
                    break

        # No reminder texts to synthetic numbers.
        TokenBooking.objects.filter(
            token_slot__resource__facility=ctx["obj"], reminder_sent_at__isnull=True
        ).update(reminder_sent_at=self.now)

    # Billing --------------------------------------------------------------

    def billing(self, ctx, patients):
        """Lab bills: paid, due, draft and cancelled, plus an advance."""
        base = self.base
        f = ctx["facility"].id
        facility = ctx["obj"]
        performer = str(base.user.external_id)

        def slug(code):
            return ChargeItemDefinition.calculate_slug_from_facility(
                facility.external_id, slug_for(code)
            )

        plans = [
            (["CBC", "LFT"], "balanced", 2),
            (["HBA1C", "FBS"], "balanced", 9),
            (["TSH"], "issued", 1),
            (["KFT", "UR"], "issued", 15),
            (["LIP"], "draft", 0),
            (["VITD"], "cancelled", 20),
            (["ECG", "CRP"], "balanced", 30),
        ]
        for patient, (codes, status, days_ago) in zip(patients, plans, strict=False):
            obj = Patient.objects.get(external_id=patient.id)
            account = get_default_account(obj, facility)
            account_id = str(account.external_id)
            before = {ci.id for ci in base.list_charge_items(f, account_id)}
            base.apply_charge_item_defs(
                f,
                [
                    {
                        "quantity": "1",
                        "charge_item_definition": slug(code),
                        "patient": patient.id,
                        "account": account_id,
                        "performer_actor": performer,
                    }
                    for code in codes
                ],
            )
            items = [
                ci.id
                for ci in base.list_charge_items(f, account_id)
                if ci.id not in before
            ]
            created = self.now - timedelta(days=days_ago)
            invoice = base.create_invoice(f, account_id, items, created_date=created)
            self.count("invoices")
            if status == "draft":
                continue
            invoice = base.issue_invoice(f, invoice, issue_date=created.isoformat())
            if status == "cancelled":
                base.cancel_invoice(f, invoice, reason="cancelled")
                continue
            if status == "balanced":
                amount = int(float(invoice.total_gross))
                base.create_payment_reconciliation(
                    f,
                    account_id,
                    tendered_amount=str(amount),
                    amount=str(amount),
                    returned_amount=0,
                    target_invoice=invoice.id,
                    method=choice(["cash", "ccca", "debc"]),
                    payment_datetime=created.isoformat(),
                )
                base.balance_invoice(f, invoice)
                self.count("payments")

        # An advance for a health check package.
        if len(patients) > len(plans):
            patient = patients[len(plans)]
            account = get_default_account(
                Patient.objects.get(external_id=patient.id), facility
            )
            base.create_payment_reconciliation(
                f,
                str(account.external_id),
                tendered_amount="5000",
                amount="5000",
                reconciliation_type="advance",
                extensions={"billing": {"purpose": "Health check package"}},
                payment_datetime=self.now.isoformat(),
            )
            self.count("advances")

    # Camps ----------------------------------------------------------------

    def camps(self, ctx, form):
        f = ctx["facility"].id
        code = ctx["code"]
        name = ctx["facility"].name
        camp_email = f"{ctx['prefix']}-camp@{EMAIL_DOMAIN}"
        event_day = self.today + timedelta(days=10)
        body = {
            "facility": f,
            "event_type": "CAMP",
            "name": f"Diabetes and BP screening camp, {name}",
            "theme": "World Diabetes Day",
            "description": (
                "Free blood sugar and blood pressure screening for residents, "
                "with referral to the hospital OPD."
            ),
            "venue_name": f"{name} RWA Community Hall",
            "address": address_line(local_place(ctx["addresses"])),
            "pincode": ctx["addresses"][0][0],
            "event_date": event_day.isoformat(),
            "start_time": "09:00",
            "end_time": "13:00",
            "expected_attendees": 150,
            "has_coupon": True,
            "coupon_code": f"{code}CAMP{event_day:%y}",
            "coupon_valid_from": event_day.isoformat(),
            "coupon_valid_to": (event_day + timedelta(days=30)).isoformat(),
            "discount_registration_pct": 100,
            "discount_consultation_pct": 50,
            "discount_tests_pct": 20,
            "login_emails": [camp_email],
            "questionnaire": form.slug if form else "",
            "tests": [
                {
                    "name": "RBS",
                    "location": "camp",
                    "fee_type": "free",
                    "cost_to_poh": "20",
                    "quantity_offered": 1,
                },
                {
                    "name": "HbA1c",
                    "location": "hospital",
                    "fee_type": "paid",
                    "rate": "400",
                },
            ],
            "staff": [
                {
                    "role": "lead",
                    "name": "Dr Camp Lead",
                    "department": "General Medicine",
                }
            ],
            "cost_lines": [
                {
                    "category": "consumable",
                    "item_name": "Glucometer strips",
                    "unit": "box",
                    "quantity": "3",
                    "unit_cost": "500",
                }
            ],
        }
        event = self.base.post("/api/hmis/outreach/events/", body)
        self.count("camps")
        if form is None:
            return
        self.base.post(f"/api/hmis/outreach/events/{event.id}/", {"action": "submit"})
        self.base.post(f"/api/hmis/outreach/events/{event.id}/", {"action": "approve"})
        self.count("camps approved")


def sample_image(label):
    """A small PNG that says it is a synthetic UAT sample."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 400), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([10, 10, 629, 389], outline="red", width=6)
    draw.text((40, 60), "SYNTHETIC UAT SAMPLE", fill="red")
    draw.text((40, 100), "NOT A REAL DOCUMENT", fill="red")
    draw.text((40, 160), label[:60], fill="black")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def preflight():
    names = [spec[0] for spec in FACILITIES]
    codes = [spec[1] for spec in FACILITIES]
    clash = Facility.objects.filter(name__in=names) | Facility.objects.filter(
        unit_code__in=codes
    )
    clash = clash.filter(deleted=False)
    if clash.exists():
        found = ", ".join(
            f"{f.name} ({f.unit_code or 'no unit code'})" for f in clash.distinct()
        )
        msg = (
            f"Facilities already exist with these names or unit codes: {found}. "
            "The UAT seed is meant for a fresh database."
        )
        raise RuntimeError(msg)


def camp_form():
    from care.emr.models import Questionnaire

    return (
        Questionnaire.objects.filter(
            status="active",
            styling_metadata__poh__family="standard-camp-registration",
        )
        .order_by("-created_date")
        .first()
    )


def load_uat(base):
    preflight()
    delhi = base.create_organization(
        org_type=OrganizationTypeChoices.govt.value, name="Delhi"
    )
    geo = {"Delhi": delhi}
    for district in sorted({spec[2] for spec in FACILITIES}):
        geo[district] = base.create_organization(
            org_type=OrganizationTypeChoices.govt.value,
            parent=delhi.id,
            name=district,
        )
    seeder = Seeder(base, geo)
    vip_role = seeder.vip_role()
    log("Organizations and the Front Office (VIP) role created")

    contexts = []
    for spec in FACILITIES:
        ctx = seeder.facility(spec)
        seeder.staff(ctx, vip_role)
        seeder.schedules(ctx)
        contexts.append(ctx)
    log("Facilities, staff and schedules created")

    # One operations administrator across the three facilities.
    seeder.user(
        "uat-ops",
        "Administrator",
        [
            (ctx["facility"].id, ctx["departments"]["Administration"])
            for ctx in contexts
        ],
    )

    form = camp_form()
    if form is None:
        seeder.warnings.append(
            "No standard camp form: camps were left as drafts. Run "
            "scripts/seed-standard-form-template.mjs (frontend) and submit them."
        )
    for ctx in contexts:
        name = ctx["facility"].name
        seeder.patients(ctx)
        seeder.quick_registrations(ctx)
        log(f"  {name}: patients registered")
        patients = ctx["patients"]
        regular = [
            p for p in patients if p.id not in {ctx["vip"].id, ctx["no_phone"].id}
        ]
        seeder.past_encounters(ctx, regular[: len(regular) // 2])
        seeder.restricted(ctx, regular[-1])
        seeder.emergencies(ctx)
        mothers = [
            p
            for p in regular
            if p.gender == "female"
            and (seeder.today.year - int(str(p.date_of_birth)[:4])) in range(21, 41)
        ]
        if mothers:
            seeder.newborn(ctx, mothers[0])
        seeder.appointments(ctx, regular[:-1])
        seeder.billing(ctx, list(reversed(regular[:-1])))
        seeder.camps(ctx, form)
        log(f"  {name}: encounters, emergencies, appointments, bills, camps")

    log("\n" + "=" * 72)
    log("UAT data loaded")
    for key in sorted(seeder.counts):
        log(f"  {key:<32} {seeder.counts[key]}")
    log("-" * 72)
    log(f"  Every staff user's password: {PASSWORD}")
    log(f"  {'Username':<22} {'Role':<22} Facility")
    for username, role, where in seeder.credentials:
        log(f"  {username:<22} {role:<22} {where}")
    for warning in seeder.warnings:
        log(f"  WARNING: {warning}")
    log("=" * 72 + "\n")


if __name__ == "__main__":
    with (
        override_settings(SMS_BACKEND="care.utils.sms.backend.console.ConsoleBackend"),
        care_fixture_context(
            superuser_username=SEED_USER, superuser_password=None
        ) as base,
    ):
        load_uat(base)
