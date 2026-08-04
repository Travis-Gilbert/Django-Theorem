from django.contrib import admin

from .models import FeatureFlag, Waitlist


@admin.register(FeatureFlag)
class FeatureFlagAdmin(admin.ModelAdmin):
    list_display = ("key", "enabled_globally", "updated_at")
    search_fields = ("key", "description")


@admin.register(Waitlist)
class WaitlistAdmin(admin.ModelAdmin):
    list_display = ("email", "invited_at", "created_at")
    search_fields = ("email",)
