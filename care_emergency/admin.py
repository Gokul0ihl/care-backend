from django.contrib import admin

from care_emergency.models import (
    ApiKey,
    ArtCouple,
    BreakGlassAccess,
    MlcSequence,
    PatientDocument,
    PatientNumberSequence,
    RegistrationAllergy,
    StatutoryFlag,
)


@admin.register(MlcSequence)
class MlcSequenceAdmin(admin.ModelAdmin):
    list_display = ("facility", "year", "last_value")
    raw_id_fields = ("facility",)


@admin.register(RegistrationAllergy)
class RegistrationAllergyAdmin(admin.ModelAdmin):
    list_display = ("substance", "severity", "patient")
    raw_id_fields = ("patient",)


@admin.register(PatientNumberSequence)
class PatientNumberSequenceAdmin(admin.ModelAdmin):
    """Lowering last_value would issue numbers again."""

    list_display = ("series", "facility", "period", "last_value")
    list_filter = ("series",)
    raw_id_fields = ("facility",)


@admin.register(PatientDocument)
class PatientDocumentAdmin(admin.ModelAdmin):
    list_display = ("category", "side", "capture_method", "patient", "created_date")
    list_filter = ("category", "capture_method")
    raw_id_fields = ("file", "patient", "created_by", "updated_by")


@admin.register(ArtCouple)
class ArtCoupleAdmin(admin.ModelAdmin):
    list_display = ("woman", "partner", "single_woman", "art_consent", "created_date")
    raw_id_fields = ("woman", "partner", "facility", "created_by", "updated_by")


@admin.register(StatutoryFlag)
class StatutoryFlagAdmin(admin.ModelAdmin):
    list_display = ("patient_id", "category", "created_by", "created_date")
    list_filter = ("category",)
    raw_id_fields = ("patient", "encounter", "created_by", "updated_by")


@admin.register(BreakGlassAccess)
class BreakGlassAccessAdmin(admin.ModelAdmin):
    """The audit trail: read-only."""

    list_display = ("user", "patient_id", "reason", "created_date", "expires_at")
    raw_id_fields = ("patient", "user")

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    """Keys are created in Settings → API keys, which shows the key once."""

    list_display = ("name", "prefix", "user", "expires_at", "revoked_at", "last_used_at")
    search_fields = ("name", "prefix", "user__username")
    raw_id_fields = ("user", "created_by", "updated_by")
    readonly_fields = ("prefix", "key_hash", "last_used_at")

    def has_add_permission(self, request):
        return False
