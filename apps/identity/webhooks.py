"""WorkOS webhook endpoint — signature verify stub (D4 / A5)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from django.conf import settings
from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.identity.models import Membership, User
from apps.tenancy.models import Tenant
from apps.tenancy.tenant_cache import invalidate_membership, set_tenant_slug_for_org

logger = logging.getLogger(__name__)
router = Router(tags=["webhooks"])


def verify_workos_signature(payload: bytes, signature_header: str | None, secret: str) -> bool:
    """Refuse bad signatures. Accepts HMAC-SHA256 of the raw body.

    WorkOS sends `WorkOS-Signature: t=<ts>,v1=<hex>`. For local tests the
    helper `make_test_signature` produces a simple `v1=<hex>` of body-only HMAC.
    Live WorkOS verification can replace this stub without changing callers.
    """
    if not signature_header or not secret:
        return False
    # Extract v1= hex (last segment if comma-separated WorkOS form).
    token = signature_header
    for part in signature_header.split(","):
        part = part.strip()
        if part.startswith("v1="):
            token = part[3:]
            break
    expected = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token)


def make_test_signature(payload: bytes, secret: str | None = None) -> str:
    """HMAC test helper used by tests and local tooling."""
    secret = secret or settings.WORKOS_WEBHOOK_SECRET
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"v1={digest}"


def _apply_event(event: dict) -> None:
    """Write shadow rows from WorkOS events. Stub mapping — no live WorkOS call."""
    event_type = event.get("event") or event.get("type") or ""
    data = event.get("data") or {}

    if event_type in ("user.created", "user.updated"):
        workos_id = data.get("id") or ""
        if not workos_id:
            return
        User.objects.update_or_create(
            workos_user_id=workos_id,
            defaults={
                "email": data.get("email") or "",
                "display_name": data.get("first_name", "") + " " + data.get("last_name", ""),
                "is_active": True,
            },
        )
    elif event_type == "user.deleted":
        workos_id = data.get("id") or ""
        User.objects.filter(workos_user_id=workos_id).update(is_active=False)
    elif event_type in ("organization.created", "organization.updated"):
        org_id = data.get("id") or ""
        if not org_id:
            return
        slug = (data.get("slug") or data.get("name") or org_id).lower().replace(" ", "-")[:128]
        tenant, _ = Tenant.objects.update_or_create(
            workos_organization_id=org_id,
            defaults={
                "slug": slug,
                "display_name": data.get("name") or slug,
                "is_active": True,
            },
        )
        # Warm org→tenant_slug cache on bind (D4).
        set_tenant_slug_for_org(org_id, tenant.slug)
    elif event_type in (
        "organization_membership.created",
        "organization_membership.updated",
    ):
        org_id = data.get("organization_id") or ""
        user_id = data.get("user_id") or ""
        membership_id = data.get("id") or ""
        if not org_id or not user_id:
            return
        tenant = Tenant.objects.filter(workos_organization_id=org_id).first()
        user, _ = User.objects.get_or_create(workos_user_id=user_id)
        if tenant is None:
            logger.warning("membership event for unknown org %s", org_id)
            return
        role = (data.get("role") or {}).get("slug") if isinstance(data.get("role"), dict) else data.get("role")
        role = role or Membership.Role.MEMBER
        if role not in Membership.Role.values:
            role = Membership.Role.MEMBER
        Membership.objects.update_or_create(
            tenant=tenant,
            user=user,
            defaults={"role": role, "workos_membership_id": membership_id},
        )
        invalidate_membership(tenant.slug, user.workos_user_id)
    elif event_type == "organization_membership.deleted":
        membership_id = data.get("id") or ""
        org_id = data.get("organization_id") or ""
        user_id = data.get("user_id") or ""
        membership = Membership.objects.filter(workos_membership_id=membership_id).select_related(
            "tenant", "user"
        ).first()
        tenant_slug = ""
        workos_user_id = user_id
        if membership is not None:
            tenant_slug = membership.tenant.slug
            workos_user_id = membership.user.workos_user_id
            membership.delete()
        elif org_id and user_id:
            tenant = Tenant.objects.filter(workos_organization_id=org_id).first()
            if tenant is not None:
                tenant_slug = tenant.slug
        if tenant_slug and workos_user_id:
            invalidate_membership(tenant_slug, workos_user_id)


@router.post("/workos")
def workos_webhook(request: HttpRequest):
    payload = request.body
    signature = request.headers.get("WorkOS-Signature") or request.headers.get("X-WorkOS-Signature")
    if not verify_workos_signature(payload, signature, settings.WORKOS_WEBHOOK_SECRET):
        raise HttpError(401, "invalid webhook signature")
    try:
        event = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HttpError(400, f"invalid json: {exc}") from exc
    _apply_event(event)
    return {"ok": True}
