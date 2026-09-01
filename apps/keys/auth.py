"""Tenant-bound machine-key authentication for internal control-plane APIs."""

from __future__ import annotations

from dataclasses import dataclass

from django.http import HttpRequest
from ninja.errors import HttpError

from apps.keys.mint import KEY_PREFIX, verify_api_key
from apps.keys.models import ApiKey
from apps.tenancy.models import Tenant


AUTH_SCHEME = "Bearer"
OFFLOAD_INVOKE_SCOPE = "offload:invoke"
OFFLOAD_READ_SCOPE = "offload:read"
OFFLOAD_CANCEL_SCOPE = "offload:cancel"
OFFLOAD_WILDCARD_SCOPE = "offload:*"
COMPETENCE_FIT_SCOPE = "competence:fit"
COMPETENCE_READ_SCOPE = "competence:read"
COMPETENCE_CLEANUP_SCOPE = "competence:cleanup"
COMPETENCE_WILDCARD_SCOPE = "competence:*"
LAYOUT_COMPUTE_SCOPE = "layout:compute"
LAYOUT_WILDCARD_SCOPE = "layout:*"
RENDERING_RENDER_SCOPE = "rendering:render"
RENDERING_WILDCARD_SCOPE = "rendering:*"
EXTRACTION_SUBMIT_SCOPE = "extraction:submit"
EXTRACTION_READ_SCOPE = "extraction:read"
EXTRACTION_REVIEW_SCOPE = "extraction:review"
EXTRACTION_WILDCARD_SCOPE = "extraction:*"


@dataclass(frozen=True)
class ApiKeyPrincipal:
    """The admitted tenant and key for one authenticated machine request."""

    tenant: Tenant
    api_key: ApiKey


def _bearer_token(request: HttpRequest) -> str:
    """Return a well-formed Theorem machine key, otherwise an empty string."""
    header = request.headers.get("Authorization", "").strip()
    scheme, _, token = header.partition(" ")
    if scheme.lower() != AUTH_SCHEME.lower() or not token or " " in token:
        return ""
    return token if token.startswith(KEY_PREFIX) else ""


def _scope_allows(scopes: object, required_scope: str) -> bool:
    if not isinstance(scopes, list):
        return False
    granted = {scope for scope in scopes if isinstance(scope, str)}
    domain, separator, _action = required_scope.partition(":")
    wildcard = f"{domain}:*" if separator else ""
    return "*" in granted or required_scope in granted or wildcard in granted


def require_machine_key(request: HttpRequest, *, scope: str) -> ApiKeyPrincipal:
    """Admit an active tenant-scoped key with the requested control-plane scope."""
    plaintext = _bearer_token(request)
    if not plaintext:
        raise HttpError(401, "machine API key required")

    candidates = ApiKey.objects.select_related("tenant").filter(
        key_prefix=plaintext[:12],
        revoked_at__isnull=True,
    )
    for api_key in candidates:
        if api_key.is_expired or not verify_api_key(plaintext, api_key.key_hash):
            continue
        if not api_key.tenant.is_active:
            raise HttpError(403, "tenant is inactive")
        if not _scope_allows(api_key.scopes, scope):
            raise HttpError(403, f"machine API key lacks {scope} scope")
        return ApiKeyPrincipal(tenant=api_key.tenant, api_key=api_key)

    # Do not distinguish a missing, revoked, expired, or invalid key to callers.
    raise HttpError(401, "machine API key required")
