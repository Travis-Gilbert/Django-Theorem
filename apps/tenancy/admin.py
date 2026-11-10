from django.contrib import admin

from .models import EnvironmentHost, Project, Store, StorePlacement, Tenant


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


@admin.register(Store)
class StoreAdmin(admin.ModelAdmin):
    list_display = ("slug", "http_url", "grpc_url", "fly_app")
    search_fields = ("slug", "http_url")


@admin.register(StorePlacement)
class StorePlacementAdmin(admin.ModelAdmin):
    """Placements are read here; moves go through ``move_tenant_store`` and
    provisioning through ``provision_tenant_store``, so each step is a checked
    transition rather than a free edit."""

    list_display = ("tenant", "store", "state", "target_store", "epoch", "updated_at")
    list_filter = ("state", "store")
    search_fields = ("tenant__slug", "store__slug")
    raw_id_fields = ("tenant",)
    readonly_fields = ("state", "target_store", "epoch", "last_error")

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return self.readonly_fields
        return ("tenant", "store", *self.readonly_fields)


@admin.register(EnvironmentHost)
class EnvironmentHostAdmin(admin.ModelAdmin):
    """Hosts are read here; provisioning goes through
    ``provision_tenant_environment``, which also binds the tenant's store."""

    list_display = ("tenant", "sprite", "state", "url", "host_sha256", "updated_at")
    list_filter = ("state",)
    search_fields = ("tenant__slug", "sprite")
    raw_id_fields = ("tenant",)
    readonly_fields = ("tenant", "sprite", "url", "host_sha256", "state", "last_error")
