# Emergency Flow – What CARE Already Has vs What Is Missing

For each of **our reception screens** this document lists:

- **See it in CARE** – the existing OHC Network page that does something similar
  (open it in the app, and the source file to read)
- **Tables** – the database tables used (checked in the local `care` database)
- **Fields** – every field on our screen, where it is stored, and whether it
  **exists** ✅ or is **missing** ❌ (🟡 = exists but needs a workaround)

Tables and columns below were read directly from the local Postgres `care`
database (`information_schema`). API routes come from the frontend
`src/types/**/*Api.ts` files.

---

## Quick answer

| Our screen | Can CARE do it today? | What is missing |
|---|---|---|
| 1. Dashboard (4 cards + search) | 🟡 Partly | No dashboard/stats API; no waiting-time value; search by UHID not set up |
| 2. Add patient – mobile number | ✅ Yes | "Same as WhatsApp" flag |
| 3. Emergency patient details | 🟡 Partly | Distinguishing marks, police station, triage colour, medico-legal case number |
| 4. Accompanying person details | ❌ No | Whole thing (only one emergency phone number exists) |
| 5. Register & Print | 🟡 Partly | One API that saves everything together |

---

## Screen 1 – Dashboard

Cards: Total Patients · Total Bookings · Active Cases · Avg Waiting Time · Search

### See it in CARE

| What | App URL | Source file |
|---|---|---|
| Facility overview (CARE's own dashboard) | `/facility/<facilityId>/overview` | `src/pages/Facility/overview.tsx` |
| Patient search page | `/facility/<facilityId>/patients` | `src/components/Patient/PatientIndex.tsx` |
| Encounters list (active cases) | `/facility/<facilityId>/encounters` | `src/pages/Encounters/EncountersOverview.tsx` |
| Queues / tokens (waiting) | `/facility/<facilityId>/queues` | `src/pages/Facility/queues/QueuesIndex.tsx` |

### Tables

| Table | Used for |
|---|---|
| `emr_patient` | Total Patients, search |
| `emr_tokenbooking` | Total Bookings (appointments) |
| `emr_encounter` | Active Cases |
| `emr_token`, `emr_tokenqueue` | Waiting |

### Fields

| Card / feature | Where it would come from | Status | Note |
|---|---|---|---|
| Total Patients | `count(*)` of `emr_patient` | 🟡 | No API returns this number directly; `GET /api/v1/patient/` returns `count` |
| Total Patients "+12% vs last month" | `emr_patient.created_date` | ❌ | No API compares months |
| Total Bookings | `count(*)` of `emr_tokenbooking` (`booked_on` in range) | 🟡 | `GET /api/v1/facility/{id}/appointments/?date_after=&date_before=` → `count` |
| Active Cases | `emr_encounter` where `status='in_progress'` (and `encounter_class='emer'`) | 🟡 | `GET /api/v1/encounter/?status=in_progress` → `count` |
| Avg Waiting Time | `emr_token.created_date` → time it was served | ❌ | `emr_token` has only `created_date`/`modified_date`; no "called at"/"served at" time, and no API computes an average |
| Search by name | `emr_patient.name` | ✅ | `GET /api/v1/patient/?name=` |
| Search by mobile | `emr_patient.phone_number` | ✅ | `POST /api/v1/patient/search/` `{ "phone_number": "+91…" }` |
| Search by UHID | `emr_patientidentifier.value` | ❌ | No UHID identifier config exists yet (see "Identifier configs" below) |

**Missing:** one dashboard API that returns the 4 numbers with the
month-over-month %, plus a way to calculate waiting time.

---

## Screen 2 – Add patient (mobile number)

Fields: Country code · Mobile Number · Same as WhatsApp · Continue · Register to Emergency

### See it in CARE

| What | App URL | Source file |
|---|---|---|
| Patient registration form (phone, name, gender, age…) | `/facility/<facilityId>/patient/create` | `src/components/Patient/PatientRegistration.tsx` |
| Search patient by phone | `/facility/<facilityId>/patients` | `src/components/Patient/PatientIndex.tsx` |

### Tables

`emr_patient`

### Fields

| Our field | Table.column | Type | Status |
|---|---|---|---|
| Country code + Mobile Number | `emr_patient.phone_number` | varchar, NOT NULL (stored as `+919876543210`) | ✅ |
| Same as WhatsApp Number | – | – | ❌ (could go in `emr_patient.extensions` jsonb) |
| Find "Members on this number" | `emr_patient.phone_number` | – | ✅ API: `POST /api/v1/patient/search/` |

---

## Screen 3 – Emergency patient details

Fields: Gender · Estimate Age · Distinguishing marks · MC-2026 (auto) ·
Police Station Informed · Red / Yellow / Green

### See it in CARE

| What | App URL | Source file |
|---|---|---|
| Register patient | `/facility/<facilityId>/patient/create` | `src/components/Patient/PatientRegistration.tsx` |
| Start a visit (choose class **Emergency** + priority) | Patient page → "Create Encounter" | `src/components/Encounter/CreateEncounterForm.tsx` |
| Patient page | `/facility/<facilityId>/patients/home` | `src/pages/Patient/PatientHome.tsx` |

### Tables

| Table | Holds |
|---|---|
| `emr_patient` | The person (gender, age, phone…) |
| `emr_encounter` | This emergency visit (class, priority, status) |
| `emr_patientidentifier` + `emr_patientidentifierconfig` | UHID / case numbers |

### Fields

| Our field | Table.column | Type / allowed values | Status |
|---|---|---|---|
| Gender | `emr_patient.gender` | `male`, `female`, `non_binary`, `transgender` | ✅ |
| Estimate Age | `emr_patient.year_of_birth` (API field `age`) | integer | ✅ |
| Distinguishing marks | – | – | ❌ **missing** |
| MC-2026-… (medico-legal case no.) | `emr_encounter.external_identifier` | varchar, free text | 🟡 column exists, but CARE does **not** auto-generate `MC-<year>-<number>` |
| Police Station Informed | – | – | ❌ **missing** |
| Triage Red / Yellow / Green | `emr_encounter.priority` | `emergency`, `urgent`, `routine`, … (13 values) | 🟡 no colours; map Red→`emergency`, Yellow→`urgent`, Green→`routine` |
| (it is an emergency visit) | `emr_encounter.encounter_class` | `emer` | ✅ (2 such rows already in local data) |
| (visit status) | `emr_encounter.status` | `in_progress`, `completed`, … | ✅ |

**CARE requires these, but our screen does not ask for them:**

| Column | Why it matters | Suggested default |
|---|---|---|
| `emr_patient.name` (NOT NULL) | Unknown emergency patients have no name | `Unknown Male` / `Unknown Female` |
| `emr_patient.geo_organization_id` (required by API) | Region of the patient | The facility's region |
| `emr_patient.address`, `permanent_address` (NOT NULL text) | Not collected in emergency | Empty string or enable quick registration |
| `emr_encounter.facility_id`, `patient_id` (NOT NULL) | Links the visit | Current facility / new patient |

---

## Screen 4 – Accompanying person details

Fields: Title · First / Middle / Last Name · Country code + phone · Relationship

### See it in CARE

Nothing similar. The closest is the **"Emergency contact number"** field on
`/facility/<facilityId>/patient/create`
(`src/components/Patient/PatientRegistration.tsx`), shown on the patient page in
`src/components/Patient/PatientDetailsTab/Demography.tsx`.

### Tables

No table for related people / guardians / next of kin exists in the database.

### Fields

| Our field | Table.column | Status |
|---|---|---|
| Title | – | ❌ **missing** |
| First Name | – | ❌ **missing** |
| Middle Name | – | ❌ **missing** |
| Last Name | – | ❌ **missing** |
| Phone number | `emr_patient.emergency_phone_number` (number only) | 🟡 exists, but not linked to a name/relationship |
| Relationship | – | ❌ **missing** |

---

## Screen 5 – Register & Print

| Step | Existing API | Status |
|---|---|---|
| Create patient | `POST /api/v1/patient/` → `emr_patient` | ✅ |
| Create emergency visit | `POST /api/v1/encounter/` → `emr_encounter` | ✅ |
| Save emergency details + accompanying person | – | ❌ |
| Do all of it in one call (all-or-nothing) | – | ❌ |
| Print slip | – | ❌ (frontend only) |

---

## Existing table schemas (local `care` database)

### `emr_patient`

| Column | Type | Null? | Used by our flow |
|---|---|---|---|
| `id` | bigint | no | internal |
| `external_id` | uuid | no | API `id` |
| `name` | varchar | no | placeholder for unknown patient |
| `gender` | varchar | no | **Gender** |
| `phone_number` | varchar | no | **Mobile Number** |
| `emergency_phone_number` | varchar | no | accompanying person phone (partial) |
| `address`, `permanent_address` | text | no | default |
| `pincode` | integer | yes | – |
| `date_of_birth` | date | yes | – |
| `year_of_birth` | integer | yes | **Estimate Age** |
| `deceased_datetime` | timestamptz | yes | – |
| `blood_group` | varchar | no | – |
| `geo_organization_id` | bigint | yes | default to facility region |
| `instance_identifiers`, `facility_identifiers` | jsonb | yes | UHID cache |
| `instance_tags` | array | no | – |
| `facility_tags` | jsonb | yes | – |
| `extensions` | jsonb | no | *could* hold missing fields |
| `meta`, `history` | jsonb | no | internal |
| `created_by_id`, `updated_by_id` | bigint | yes | audit |
| `created_date`, `modified_date` | timestamptz | yes | dashboard "vs last month" |
| `deleted` | boolean | no | soft delete |

### `emr_encounter`

| Column | Type | Null? | Used by our flow |
|---|---|---|---|
| `id`, `external_id` | bigint, uuid | no | |
| `patient_id` | bigint | no | → `emr_patient` |
| `facility_id` | bigint | no | → `facility_facility` |
| `encounter_class` | varchar | yes | `emer` |
| `priority` | varchar | yes | **Triage** (mapped) |
| `status` | varchar | yes | **Active Cases** (`in_progress`) |
| `status_history` | jsonb | no | |
| `period` | jsonb | no | start/end time |
| `hospitalization` | jsonb | no | `admit_source: "emd"` |
| `external_identifier` | varchar | yes | **MLC number** |
| `appointment_id` | bigint | yes | – |
| `current_location_id` | bigint | yes | – |
| `care_team`, `care_team_users` | jsonb, array | no | – |
| `tags` | array | no | – |
| `extensions` | jsonb | no | *could* hold missing fields |
| `discharge_summary_advice` | text | yes | – |
| `created_date`, `modified_date` | timestamptz | yes | |

### `emr_patientidentifierconfig` / `emr_patientidentifier`

`emr_patientidentifierconfig`: `id`, `external_id`, `status`, `config` (jsonb:
`use`, `display`, `system`, `required`, `unique`, `regex`, `auto_maintained`,
`retrieve_config`), `facility_id`.
`emr_patientidentifier`: `id`, `patient_id`, `config_id`, `facility_id`, `value`.

Configs currently in the local database (only 2):

| Display | System | Auto | Scope |
|---|---|---|---|
| Patient Phone Number | `system.care.ohc.network/patient-phone-number` | yes | instance |
| Patient Name | `system.care.ohc.network/patient-name` | yes | facility 1 |

➡ **No UHID config exists.** One must be created (e.g. display "UHID",
system `…/uhid`, unique) before patients can be searched by UHID.

### `emr_token` / `emr_tokenqueue` / `emr_tokenbooking`

- `emr_token`: `number`, `status`, `is_next`, `note`, `patient_id`,
  `queue_id`, `sub_queue_id`, `category_id`, `booking_id`, `facility_id`,
  `created_date`, `modified_date`
- `emr_tokenqueue`: `name`, `date`, `is_primary`, `system_generated`,
  `resource_id`, `facility_id`
- `emr_tokenbooking` (appointments): `booked_on`, `status`, `note`,
  `patient_id`, `token_slot_id`, `token_id`, `associated_encounter_id`, `tags`

---

## All missing fields (what the new backend must add)

| # | Field | Screen | Type | Required |
|---|---|---|---|---|
| 1 | Distinguishing marks | Emergency details | text | yes |
| 2 | Police station informed | Emergency details | varchar(255) | yes |
| 3 | Triage level (`red` / `yellow` / `green`) | Emergency details | varchar | yes |
| 4 | Medico-legal case number (auto `MC-<year>-<seq>`) | Emergency details | varchar, unique | auto |
| 5 | Accompanying person – title | Accompanying | varchar | yes |
| 6 | Accompanying person – first name | Accompanying | varchar | yes |
| 7 | Accompanying person – middle name | Accompanying | varchar | yes |
| 8 | Accompanying person – last name | Accompanying | varchar | yes |
| 9 | Accompanying person – country code | Accompanying | varchar | yes |
| 10 | Accompanying person – phone | Accompanying | varchar | yes |
| 11 | Accompanying person – relationship | Accompanying | varchar | yes |
| 12 | Same as WhatsApp | Add patient | boolean | no |
| 13 | UHID identifier config | All | config row | setup |
| 14 | Dashboard summary (4 KPIs + % change) | Dashboard | API | – |
| 15 | Token served/called time (for waiting time) | Dashboard | timestamp | – |

**Suggested storage:** one new table, e.g. `reception_emergencyregistration`,
linked to `emr_encounter` (one-to-one), holding fields 1–11 (+ 12).
Everything else reuses `emr_patient` and `emr_encounter`.
