from django.contrib import admin

from .models import SupportNote


@admin.register(SupportNote)
class SupportNoteAdmin(admin.ModelAdmin):
    list_display = ("subject", "tenant", "author", "created_at")
    search_fields = ("subject", "body")
    raw_id_fields = ("tenant", "author")
