"""POH data on top of Care's fixtures (the hmis/ app).

Everything goes through the hmis endpoints, so the desk and emergency rules
run as they do for the frontend:

- facilities get a unit code and an emergency desk number, without which
  hmis issues no UHIDs, invoice numbers or emergency registrations;
- patients are registered at the front desk (DOM, payer Self) and get a UHID;
- a few emergency arrivals: unidentified (UNK number, MLC) and quick-registered.

All names, phones and ID tails are fake.
"""

import re
from secrets import choice

from care.emr.resources.patient.spec import BloodGroupChoices
from care.fixtures.base import generate_phone_number
from hmis.pincodes import lookup
from hmis.specs import LAST_NAME, NAME_PART

# (PIN, locality, city, district, state). When the PIN directory lists the PIN,
# its post office, district and state are used instead, so the desk's
# PIN check (hmis.pincodes.address_problem) passes either way.
ADDRESSES = [
    ("110024", "Lajpat Nagar III", "New Delhi", "South East Delhi", "Delhi"),
    ("400050", "Bandra West", "Mumbai", "Mumbai", "Maharashtra"),
    ("560034", "Koramangala", "Bengaluru", "Bengaluru Urban", "Karnataka"),
    ("682016", "Ernakulam South", "Kochi", "Ernakulam", "Kerala"),
    ("600017", "T Nagar", "Chennai", "Chennai", "Tamil Nadu"),
]
LANGUAGES = ["English", "Hindi", "Malayalam", "Tamil", "Kannada", "Marathi"]
ALLERGIES = [
    {"substance": "Penicillin", "reaction": "Hives", "severity": "moderate"},
    {"substance": "Peanuts", "reaction": "Swelling", "severity": "severe"},
    {"substance": "Sulfa drugs", "reaction": "Rash", "severity": "mild"},
]


def name_part(make, pattern=NAME_PART):
    """A faker name that passes the desk's name rules (letters, single spaces)."""
    for _ in range(20):
        value = make().strip()
        if re.fullmatch(pattern, value):
            return value
    msg = "Could not generate a valid name"
    raise RuntimeError(msg)


def address(base):
    pincode, locality, city, district, state = choice(ADDRESSES)
    listed = lookup(pincode)
    if listed:
        locality = listed["localities"][0]
        district, state = listed["district"], listed["state"]
    return {
        "pincode": pincode,
        "house": base.fake.building_number(),
        "street": base.fake.street_name()[:150],
        "locality": locality,
        "city": city,
        "district": district,
        "state": state,
    }


def desk_body(base, geo_organization, *, gender, minor=False, no_phone=False):
    """The patient and registration halves of a desk registration request,
    as the frontend builds them (lib/patient-registration.ts)."""
    fake = base.fake
    first_name = name_part(
        fake.first_name_male if gender == "male" else fake.first_name_female
    )
    last_name = name_part(fake.last_name, LAST_NAME)
    place = address(base)
    phone = None if no_phone else generate_phone_number()
    language = choice(LANGUAGES)
    allergic = choice([True, False])

    if minor:
        born = fake.date_of_birth(minimum_age=5, maximum_age=15)
        title = "Master" if gender == "male" else "Ms"
        parent = f"{name_part(fake.first_name_male)} {last_name}"
        kin = {
            "guardian_name": parent,
            "guardian_relationship": "S/O" if gender == "male" else "D/O",
            "next_of_kin_title": "Mr",
            "next_of_kin_name": parent,
            "next_of_kin_relationship": "Father",
            "next_of_kin_phone": phone or generate_phone_number(),
            "occupation": "Student",
            "consent_given_by": "Parent",
        }
        mobile_owner = "Father"
    else:
        born = fake.date_of_birth(minimum_age=19, maximum_age=80)
        married = choice([True, False])
        title = "Mr" if gender == "male" else "Mrs" if married else "Ms"
        kin = {
            "marital_status": "married" if married else "single",
            "occupation": choice(["Engineer", "Teacher", "Retired", "Business"]),
            "consent_given_by": "The patient",
        }
        mobile_owner = "Self"

    registration = {
        "registration_type": "DOM",
        "title": title,
        "first_name": first_name,
        "last_name": last_name,
        "preferred_language": language,
        "mobile_owner": "No phone" if no_phone else mobile_owner,
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
        "identity_number_last4": f"{fake.random_int(min=0, max=9999):04d}",
        "abha_status": "not_now",
        "allergy_status": "has" if allergic else "nka",
        "allergies": [choice(ALLERGIES)] if allergic else [],
        "payer_type": "Self",
        "arrival_source": "Walk-in",
        "consent_method": "Signature",
        "consent_language": language,
        "general_consent": True,
        "data_processing_consent": True,
        "tariff_consent": True,
        "communication_channels": [] if no_phone else ["SMS", "WhatsApp"],
        **kin,
    }
    line = ", ".join(
        part
        for part in (
            place["house"],
            place["street"],
            place["locality"],
            place["city"],
            place["district"],
            place["state"],
        )
        if part
    )
    patient = {
        "name": f"{first_name} {last_name}",
        "gender": gender,
        "date_of_birth": born.isoformat(),
        "pincode": int(place["pincode"]),
        "address": line,
        "permanent_address": line,
        "blood_group": choice(
            [b.value for b in BloodGroupChoices if b.value != "unknown"]
        ),
        "geo_organization": geo_organization,
    }
    if phone:
        patient["phone_number"] = phone
    return patient, registration


def set_up_facility(base, facility_id, unit_code):
    """The unit code (UHID prefix) and the emergency desk number."""
    base.set_unit_code(facility_id, unit_code)
    base.set_emergency_desk(facility_id, generate_phone_number())


def register_desk_patients(base, facility_id, geo_organization, count=10):
    """Desk-registered patients with UHIDs: adults, one child and one with no
    phone (registered on the desk number). Returns the Care patients."""
    kinds = [{"minor": True}, {"no_phone": True}]
    kinds += [{}] * max(0, count - len(kinds))
    patients = []
    for kind in kinds[:count]:
        patient, registration = desk_body(
            base, geo_organization, gender=choice(["male", "female"]), **kind
        )
        response = base.register_desk_patient(facility_id, patient, registration)
        patients.append(response.patient)
    return patients


def pre_register_patient(base, facility_id, geo_organization):
    """A pre-registration (PRE- reference). It has no encounter, so it keeps
    the PRE number until the patient arrives."""
    patient, registration = desk_body(base, geo_organization, gender="female")
    response = base.register_desk_patient(
        facility_id, patient, registration, pre_registration=True
    )
    return response.patient


def register_emergencies(base, facility_id, geo_organization, emergency_org_id):
    """Emergency arrivals: two unidentified (one an MLC) and one quick-registered."""
    organizations = [emergency_org_id] if emergency_org_id else []
    common = {"organizations": organizations}
    base.register_emergency_patient(
        facility_id,
        {"name": "Unknown", "gender": "male", "age": 35},
        triage="red",
        is_medico_legal=True,
        mlc_nature="Road accident",
        police_station="Central Police Station",
        distinguishing_marks="Scar on left forearm",
        is_unidentified=True,
        brought_by={"name": "108 ambulance from NH-66"},
        **common,
    )
    base.register_emergency_patient(
        facility_id,
        {"name": "Unknown", "gender": "female", "age": 60},
        triage="green",
        distinguishing_marks="Wearing a blue saree",
        is_unidentified=True,
        brought_by={"name": "Passer-by", "phone_number": generate_phone_number()},
        **common,
    )
    fake = base.fake
    first_name = name_part(fake.first_name_male)
    last_name = name_part(fake.last_name, LAST_NAME)
    base.register_emergency_patient(
        facility_id,
        {
            "name": f"{first_name} {last_name}",
            "gender": "male",
            "age": 52,
            "geo_organization": geo_organization,
            "registration": {
                "prefix": "Mr",
                "first_name": first_name,
                "last_name": last_name,
            },
        },
        triage="yellow",
        is_unidentified=False,
        chief_complaint="Chest pain",
        mode_of_arrival="108 ambulance",
        brought_by={
            "name": name_part(fake.first_name_female),
            "phone_number": generate_phone_number(),
            "relationship": "Spouse",
        },
        **common,
    )


def load_poh(base, facility_id, geo_organization, departments):
    """Pre-registration and emergency arrivals (desk patients are created by
    the caller, as they feed encounters, billing and scheduling)."""
    pre_register_patient(base, facility_id, geo_organization)
    emergency = departments.get("Emergency")
    register_emergencies(
        base, facility_id, geo_organization, emergency.id if emergency else None
    )
