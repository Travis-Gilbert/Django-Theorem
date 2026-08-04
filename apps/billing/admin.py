from django.contrib import admin, messages

from .models import Plan, Subscription, Usage


@admin.register(Plan)
class PlanAdmin(admin.ModelAdmin):
    list_display = ("code", "display_name", "limits")
    search_fields = ("code", "display_name")


@admin.register(Subscription)
class SubscriptionAdmin(admin.ModelAdmin):
    list_display = ("tenant", "plan", "status", "stripe_subscription_id")
    list_filter = ("status", "plan")
    raw_id_fields = ("tenant", "plan")


@admin.register(Usage)
class UsageAdmin(admin.ModelAdmin):
    list_display = ("tenant", "period_start", "period_end", "counters")
    list_filter = ("period_start",)
    raw_id_fields = ("tenant",)
    actions = ["reset_usage"]

    @admin.action(description="Reset usage counters")
    def reset_usage(self, request, queryset):
        updated = queryset.update(counters={})
        messages.success(request, f"Reset counters on {updated} usage row(s).")
