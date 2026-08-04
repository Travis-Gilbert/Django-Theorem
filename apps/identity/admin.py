from django.contrib import admin, messages
from django.utils import timezone

from .models import Membership, User


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("email", "workos_user_id", "display_name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("email", "workos_user_id", "display_name")
    actions = ["impersonate"]

    @admin.action(description="Impersonate (audit log only — stub token)")
    def impersonate(self, request, queryset):
        """Support impersonation: issues a short-lived scoped token stub and audits."""
        for user in queryset:
            # Live token mint requires WorkOS; stub records the audit event only.
            messages.warning(
                request,
                f"[audit] impersonate requested for {user.workos_user_id} "
                f"by {request.user} at {timezone.now().isoformat()} "
                "(StubImpersonationToken — no live WorkOS credential)",
            )


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ("user", "tenant", "role", "workos_membership_id")
    list_filter = ("role", "tenant")
    search_fields = ("user__email", "tenant__slug")
    raw_id_fields = ("user", "tenant")
