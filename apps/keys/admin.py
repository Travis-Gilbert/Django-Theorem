from django.contrib import admin, messages
from django.utils import timezone

from .mint import mint_api_key
from .models import ApiKey
from .valkey_publish import publish_key_revocation


@admin.register(ApiKey)
class ApiKeyAdmin(admin.ModelAdmin):
    list_display = (
        "key_prefix",
        "tenant",
        "label",
        "scopes",
        "revoked_at",
        "expires_at",
        "created_at",
    )
    list_filter = ("tenant",)
    search_fields = ("key_prefix", "label", "id")
    raw_id_fields = ("tenant",)
    readonly_fields = ("key_hash", "key_prefix", "created_at")
    actions = ["revoke_key"]

    @admin.action(description="Revoke key (publish Valkey eviction)")
    def revoke_key(self, request, queryset):
        now = timezone.now()
        for key in queryset.filter(revoked_at__isnull=True):
            key.revoked_at = now
            key.save(update_fields=["revoked_at"])
            publish_key_revocation(
                str(key.id),
                key.key_prefix,
                tenant_slug=key.tenant.slug,
            )
        messages.success(request, f"Revoked {queryset.count()} key(s).")

    def save_model(self, request, obj, form, change):
        if not change and not obj.key_hash:
            minted = mint_api_key(
                obj.tenant,
                scopes=obj.scopes or [],
                label=obj.label,
                expires_at=obj.expires_at,
            )
            messages.warning(
                request,
                f"Plaintext key (shown once): {minted.plaintext}",
            )
            # mint_api_key already persisted; skip duplicate save of empty shell
            return
        super().save_model(request, obj, form, change)
