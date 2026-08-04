"""D11 — ImpersonationGrant mint + audit."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.identity.models import Membership, User
from apps.support.models import IMPERSONATION_MAX_TTL, ImpersonationGrant
from apps.tenancy.models import Tenant


@pytest.mark.django_db
def test_impersonation_grant_mint_capped_at_30m():
    tenant = Tenant.objects.create(slug="imp-tenant", display_name="Imp")
    AuthUser = get_user_model()
    admin = AuthUser.objects.create_user(username="ops", password="x")
    shadow = User.objects.create(workos_user_id="user_imp", email="u@example.com")
    Membership.objects.create(tenant=tenant, user=shadow, role=Membership.Role.MEMBER)

    grant, plaintext = ImpersonationGrant.mint(
        tenant=tenant,
        subject_user_id=shadow.workos_user_id,
        created_by=admin,
        ttl=timedelta(hours=2),  # must clamp to 30m
    )
    assert plaintext.startswith(grant.token_id + ".")
    assert grant.token_hash
    assert plaintext not in (grant.token_hash, grant.audit_note)
    assert grant.expires_at <= timezone.now() + IMPERSONATION_MAX_TTL + timedelta(seconds=5)
    assert "impersonate user_imp" in grant.audit_note
    assert grant.scopes == ["support:impersonate"]
