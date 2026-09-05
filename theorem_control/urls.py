"""URL configuration for theorem_control."""

from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from ninja import NinjaAPI

from apps.orchestration.api import router as offload_router
from apps.competence.api import router as competence_router
from apps.identity.webhooks import router as webhooks_router
from apps.layout.api import router as layout_router
from apps.rendering.api import router as rendering_router
from apps.extraction.api import router as extraction_router
from theorem_control.rl.api import router as rl_router

api = NinjaAPI(title="Theorem Control Plane", version="1.0.0")
api.add_router("/webhooks", webhooks_router)
api.add_router("/internal/offload", offload_router)
api.add_router("/internal/competence", competence_router)
api.add_router("/internal/layout", layout_router)
api.add_router("/internal/rendering", rendering_router)
api.add_router("/internal/extraction", extraction_router)
api.add_router("/internal/rl", rl_router)


def health(_request):
    return JsonResponse({"status": "ok", "service": "theorem-control-plane"})


urlpatterns = [
    path("admin/", admin.site.urls),
    path("healthz", health),
    path("", api.urls),
]
