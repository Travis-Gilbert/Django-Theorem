from django.contrib import admin

from .models import Project, Tenant


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ("slug", "display_name", "is_active", "workos_organization_id")
    list_filter = ("is_active",)
    search_fields = ("slug", "display_name", "workos_organization_id")


@admin.register(Project)
class ProjectAdmin(admin.ModelAdmin):
    list_display = ("slug", "display_name", "tenant")
    list_filter = ("tenant",)
    search_fields = ("slug", "display_name")
    raw_id_fields = ("tenant",)
