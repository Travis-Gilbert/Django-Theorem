"""Extraction ledger, shard candidates, and append-only review controls."""

from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import path, reverse

from apps.orchestration.artifacts import (
    ArtifactConfigurationError,
    ArtifactStorageError,
    ArtifactStore,
    ArtifactValidationError,
)
from apps.orchestration.tasks import cancel_job_task, re_run_job

from .models import ExtractionJob, ExtractionReview, ExtractionShard
from .reviews import candidate_digest


class ExtractionShardInline(admin.TabularInline):
    model = ExtractionShard
    extra = 0
    can_delete = False
    show_change_link = True
    fields = (
        "index",
        "status",
        "input_rows",
        "output_rows",
        "output_digest",
        "orchestration_job",
    )
    readonly_fields = fields


@admin.register(ExtractionJob)
class ExtractionJobAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "tenant",
        "operation",
        "source_kind",
        "status",
        "shard_count",
        "rows_total",
        "error",
        "created_at",
    )
    list_filter = ("status", "operation", "source_kind", "tenant")
    search_fields = ("id", "params_hash", "contract_version")
    raw_id_fields = ("tenant",)
    readonly_fields = (
        "id",
        "params_hash",
        "status",
        "shard_count",
        "rows_total",
        "created_at",
        "updated_at",
    )
    inlines = (ExtractionShardInline,)
    actions = ("rerun_failed_shards", "cancel_jobs")

    @admin.action(description="Re-run failed shards")
    def rerun_failed_shards(self, request: HttpRequest, queryset) -> None:
        rerun = 0
        for job in queryset:
            with transaction.atomic():
                locked_job = ExtractionJob.objects.select_for_update().get(id=job.id)
                failed_shards = list(
                    locked_job.shards.select_for_update().filter(
                        status=ExtractionShard.Status.FAILED,
                        orchestration_job__isnull=False,
                    )
                )
                for shard in failed_shards:
                    orchestration_job_id = str(shard.orchestration_job_id)
                    shard.status = ExtractionShard.Status.QUEUED
                    shard.error = ""
                    shard.save(update_fields=["status", "error", "updated_at"])
                    transaction.on_commit(
                        lambda job_id=orchestration_job_id: re_run_job.delay(job_id)
                    )
                if failed_shards and locked_job.status in {
                    ExtractionJob.Status.FAILED,
                    ExtractionJob.Status.PARTIAL,
                }:
                    locked_job.status = ExtractionJob.Status.RUNNING
                    locked_job.save(update_fields=["status", "updated_at"])
                rerun += len(failed_shards)
        messages.success(request, f"Re-queued {rerun} failed shard(s).")

    @admin.action(description="Cancel extraction jobs")
    def cancel_jobs(self, request: HttpRequest, queryset) -> None:
        canceled = 0
        terminal = {
            ExtractionJob.Status.SUCCEEDED,
            ExtractionJob.Status.FAILED,
            ExtractionJob.Status.CANCELED,
        }
        for job in queryset.exclude(status__in=terminal):
            for shard in job.shards.exclude(
                status__in={
                    ExtractionShard.Status.SUCCEEDED,
                    ExtractionShard.Status.FAILED,
                    ExtractionShard.Status.CANCELED,
                    ExtractionShard.Status.SUPERSEDED,
                }
            ):
                if shard.orchestration_job_id:
                    cancel_job_task.delay(str(shard.orchestration_job_id))
                shard.status = ExtractionShard.Status.CANCELED
                shard.save(update_fields=["status", "updated_at"])
            job.status = ExtractionJob.Status.CANCELED
            job.save(update_fields=["status", "updated_at"])
            canceled += 1
        messages.success(request, f"Canceled {canceled} extraction job(s).")


@admin.register(ExtractionShard)
class ExtractionShardAdmin(admin.ModelAdmin):
    change_form_template = "admin/extraction/extractionshard/change_form.html"
    list_display = (
        "job",
        "index",
        "status",
        "input_rows",
        "output_rows",
        "updated_at",
    )
    list_filter = ("status", "job__tenant", "job__operation")
    search_fields = ("job__id", "output_digest", "input_digest")
    raw_id_fields = ("job", "orchestration_job")
    readonly_fields = (
        "input_artifact_key",
        "input_digest",
        "input_schema_json",
        "input_rows",
        "output_artifact_key",
        "output_rows",
        "output_digest",
        "created_at",
        "updated_at",
    )

    def get_urls(self):
        custom = [
            path(
                "<path:object_id>/review/",
                self.admin_site.admin_view(self.review_candidate),
                name="extraction_extractionshard_review",
            )
        ]
        return custom + super().get_urls()

    @staticmethod
    def _candidate_rows(shard: ExtractionShard) -> list[dict[str, Any]]:
        if not shard.output_artifact_key or not shard.output_digest:
            return []
        schema_json = (
            shard.orchestration_job.output_schema_json
            if shard.orchestration_job_id
            else ""
        )
        table = ArtifactStore.from_settings().read_table(
            shard.job.tenant_id,
            shard.output_artifact_key,
            expected_digest=shard.output_digest,
            expected_schema_json=schema_json,
            expected_rows=shard.output_rows,
        )
        rows = []
        for raw in table.to_pylist():
            row = dict(raw)
            row["candidate_digest"] = candidate_digest(str(shard.job.tenant_id), row)
            rows.append(row)
        return rows

    def changeform_view(
        self,
        request: HttpRequest,
        object_id: str | None = None,
        form_url: str = "",
        extra_context: dict[str, Any] | None = None,
    ) -> HttpResponse:
        context = dict(extra_context or {})
        if object_id is not None:
            shard = self.get_object(request, object_id)
            if shard is not None:
                try:
                    rows = self._candidate_rows(shard)
                except (
                    ArtifactConfigurationError,
                    ArtifactStorageError,
                    ArtifactValidationError,
                ) as exc:
                    rows = []
                    context["candidate_error"] = str(exc)
                page = Paginator(rows, 50).get_page(request.GET.get("candidate_page"))
                context["candidate_page"] = page
                candidate_query = request.GET.copy()
                candidate_query.pop("candidate_page", None)
                context["candidate_query"] = candidate_query.urlencode()
                context["review_url"] = reverse(
                    "admin:extraction_extractionshard_review",
                    args=[shard.pk],
                )
                context["decision_choices"] = ExtractionReview.Decision.choices
        return super().changeform_view(request, object_id, form_url, context)

    def review_candidate(self, request: HttpRequest, object_id: str) -> HttpResponse:
        if request.method != "POST":
            return redirect(
                "admin:extraction_extractionshard_change",
                object_id,
            )
        shard = get_object_or_404(
            ExtractionShard.objects.select_related("job", "orchestration_job"),
            pk=object_id,
        )
        if not self.has_change_permission(request, shard) or not request.user.has_perm(
            "extraction.add_extractionreview"
        ):
            raise PermissionDenied
        try:
            candidates = {
                row["candidate_digest"]: row for row in self._candidate_rows(shard)
            }
        except (
            ArtifactConfigurationError,
            ArtifactStorageError,
            ArtifactValidationError,
        ) as exc:
            messages.error(request, f"Could not verify candidate artifact: {exc}")
            return redirect("admin:extraction_extractionshard_change", object_id)
        digest = request.POST.get("candidate_digest", "")
        if digest not in candidates:
            messages.error(request, "Candidate is not present in this shard artifact.")
            return redirect("admin:extraction_extractionshard_change", object_id)
        try:
            with transaction.atomic():
                review = ExtractionReview(
                    tenant=shard.job.tenant,
                    job=shard.job,
                    candidate_digest=digest,
                    decision=request.POST.get("decision", ""),
                    merge_target_claim_id=(
                        request.POST.get("merge_target_claim_id") or None
                    ),
                    reason=request.POST.get("reason", ""),
                    reviewer=f"user:{request.user.pk}",
                )
                review.full_clean()
                review.save()
        except ValidationError as exc:
            messages.error(request, "; ".join(exc.messages))
            return redirect("admin:extraction_extractionshard_change", object_id)
        messages.success(request, "Review decision recorded.")
        return redirect("admin:extraction_extractionshard_change", object_id)


@admin.register(ExtractionReview)
class ExtractionReviewAdmin(admin.ModelAdmin):
    list_display = (
        "candidate_digest",
        "tenant",
        "decision",
        "job",
        "reviewer",
        "created_at",
    )
    list_filter = ("tenant", "decision", "job")
    search_fields = ("candidate_digest", "claim_id", "merge_target_claim_id", "reviewer")
    raw_id_fields = ("tenant", "job")
    readonly_fields = ("id", "created_at")

    def get_readonly_fields(self, request, obj=None):
        if obj is not None:
            return tuple(field.name for field in self.model._meta.fields)
        return super().get_readonly_fields(request, obj)

    def has_change_permission(self, request, obj=None):
        if obj is not None:
            return False
        return super().has_change_permission(request, obj)

    def has_delete_permission(self, request, obj=None):
        return False
