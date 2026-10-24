from django.apps import AppConfig


class PohConfig(AppConfig):
    name = "hmis"
    verbose_name = "Pacific OneHealth"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from care.emr.registries.extensions.registry import ExtensionRegistry

        from hmis import signals, tasks  # noqa: F401
        from hmis.billing_rules import BillingExtension
        from hmis.restricted import mask_patient
        from hmis.contact import hide_no_phone, patient_phone_allowed

        ExtensionRegistry.register(BillingExtension())

        # Masks restricted patients (D30) wherever Care shows a patient.
        from care.emr.resources.patient.hooks import (
            patient_serialization_hooks,
            patient_phone_hooks,
        )

        if hide_no_phone not in patient_serialization_hooks:
            patient_serialization_hooks.insert(0, hide_no_phone)
        if patient_phone_allowed not in patient_phone_hooks:
            patient_phone_hooks.append(patient_phone_allowed)
        if mask_patient not in patient_serialization_hooks:
            patient_serialization_hooks.append(mask_patient)
