"""Tenant-authenticated internal graph layout API."""

from __future__ import annotations

import subprocess

from django.http import HttpResponse
from ninja import Router
from ninja.errors import HttpError

from apps.keys.auth import LAYOUT_COMPUTE_SCOPE, require_machine_key
from apps.layout.contracts import LayoutRequest
from apps.layout.service import (
    LayoutExecutionError,
    LayoutExecutionTimeout,
    compute_layout,
)

router = Router(tags=["layout"])


@router.post("/compute")
def layout_compute(request, body: LayoutRequest):
    principal = require_machine_key(request, scope=LAYOUT_COMPUTE_SCOPE)
    try:
        payload = compute_layout(body, tenant_slug=principal.tenant.slug)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from exc
    except LayoutExecutionTimeout as exc:
        raise HttpError(504, str(exc)) from exc
    except (LayoutExecutionError, OSError, subprocess.SubprocessError) as exc:
        raise HttpError(503, "layout engine unavailable") from exc
    return HttpResponse(payload, content_type="application/json")
