"""Billing reads for the tenant's own clients (the TheoremWeb status band)."""

from __future__ import annotations

from datetime import date

from django.utils import timezone
from ninja import Router, Schema

from apps.billing.credit import credit_allowance
from apps.keys.auth import BILLING_READ_SCOPE, require_machine_key

router = Router(tags=["billing"])


class CreditResponse(Schema):
    period_start: date
    period_end: date
    used_usd: float
    limit_usd: float | None


@router.get("/credit", response=CreditResponse)
def credit(request):
    """The authenticated tenant's credit for the current period."""
    principal = require_machine_key(request, scope=BILLING_READ_SCOPE)
    allowance = credit_allowance(principal.tenant.id, timezone.now().date())
    return CreditResponse(
        period_start=allowance.period_start,
        period_end=allowance.period_end,
        used_usd=float(allowance.used_usd),
        limit_usd=None if allowance.limit_usd is None else float(allowance.limit_usd),
    )
