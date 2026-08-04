from django.contrib import admin, messages
from django.utils import timezone

from apps.support.models import ImpersonationGrant

from .models import Membership, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "workos_user_id", "display_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("email", "workos_user_id", "display_name")
    actions = ["impersonate"]

    @admin.action(description="Impersonate (mint short-lived scoped token, logged)")
    def impersonate(self, request, queryset):
        """D11: mint ImpersonationGrant (<=30m), show token once, keep audit log."""
        for user in queryset:
            membership = user.memberships.select_related("tenant").first()
            if membership is None:
                messages.error(
                    request,
                    f"Cannot impersonate {user.workos_user_id}: no tenant membership",
                )
                continue
            grant, plaintext = ImpersonationGrant.mint(
                tenant=membership.tenant,
                subject_user_id=user.workos_user_id,
                created_by=request.user,
            )
            messages.warning(
                request,
                f"[audit] impersonate {user.workos_user_id} tenant={membership.tenant.slug} "
                f"token_id={grant.token_id} expires_at={grant.expires_at.isoformat()} "
                f"by={request.user} at={timezone.now().isoformat()} "
                f"— plaintext (shown once): {plaintext}",
            )


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "role", "workos_membership_id")
    list_filter = ("role", "tenant")
    search_fields = ("user__email", "tenant__slug")
    raw_id_fields = ("user", "tenant")
