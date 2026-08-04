from django.contrib import admin, messages

from .models import ImpersonationGrant, SupportNote


@admin.register(SupportNote)
class SupportNoteAdmin(admin.ModelAdmin):
    list_display = ("subject", "tenant", "author", "created_at")
    search_fields = ("subject", "body")
    raw_id_fields = ("tenant", "author")


@admin.register(ImpersonationGrant)
class ImpersonationGrantAdmin(admin.ModelAdmin):
    list_display = ("token_id", "tenant", "subject_user_id", "expires_at", "created_at")
    list_filter = ("tenant",)
    search_fields = ("token_id", "subject_user_id", "audit_note")
    raw_id_fields = ("tenant", "created_by")
    readonly_fields = (
        "token_id",
        "token_hash",
        "expires_at",
        "audit_note",
        "created_at",
    )
