from django.contrib import admin, messages

from .models import Job
from .tasks import re_run_job


@admin.register(Job)
class JobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "operation",
        "operation_id",
        "status",
        "tenant",
        "created_at",
        "ended_at",
    )
    list_filter = ("status", "operation")
    search_fields = ("operation_id", "operation", "celery_task_id")
    raw_id_fields = ("tenant",)
    readonly_fields = ("created_at", "updated_at", "celery_task_id")
    actions = ["re_run_job_action"]

    @admin.action(description="Re-run job (same operation_id)")
    def re_run_job_action(self, request, queryset):
        for job in queryset:
            re_run_job.delay(str(job.id))
        messages.success(request, f"Re-queued {queryset.count()} job(s).")
