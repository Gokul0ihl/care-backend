"""What the front desk records on a booking beyond patient, note and tags
(POH v12.1 D46, D48-D51, I04): the APT number, consultation type, visit
purpose, booking channel and the "not sure which department" guidance."""

from enum import Enum


class ConsultationTypes(str, Enum):
    in_person = "in_person"
    video = "video"
    home_visit = "home_visit"


# Copied to Encounter.encounter_class when the booking is checked in.
CONSULTATION_ENCOUNTER_CLASS = {
    ConsultationTypes.in_person.value: "amb",
    ConsultationTypes.video.value: "vr",
    ConsultationTypes.home_visit.value: "hh",
}


class VisitPurposes(str, Enum):
    new = "new"
    follow_up = "follow_up"
    report_review = "report_review"
    procedure_review = "procedure_review"


class BookingChannels(str, Enum):
    walk_in = "walk_in"
    front_desk = "front_desk"
    phone = "phone"
    # The patient's own booking through the OTP flow.
    online = "online"


# Phone bookings get an SMS this long before the slot starts.
REMINDER_LEAD_TIME_HOURS = 2

# "I don't know whom to meet" (D50). The lists match the desk dialog.
GUIDANCE_AREAS = [
    "Heart / chest",
    "Breathing / lungs",
    "Stomach / digestion",
    "Bones / joints / back",
    "Brain / nerves / headache",
    "Skin / hair",
    "Ear / nose / throat",
    "Eyes",
    "Kidney / urine",
    "Women's health / pregnancy",
    "Child's health",
    "Mind / mood / sleep",
    "Teeth / mouth",
    "Fever / general weakness",
    "Not sure",
]
GUIDANCE_SINCE = [
    "Today",
    "1-3 days",
    "4-7 days",
    "1-4 weeks",
    "More than a month",
]
GUIDANCE_RED_FLAGS = [
    "Chest pain or pressure",
    "Breathless at rest",
    "Sudden weakness, face droop or slurred speech",
    "Fainting or unconsciousness",
    "Fits / seizure",
    "Heavy bleeding",
    "Severe injury or burn",
    "Thoughts of self-harm",
]
