"""Usage counter accumulation — D6.

Rust reports soft usage on the Valkey stream; Django accumulates into
``control_usage.counters`` and decides hard/soft outcomes.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.db import transaction

from apps.billing.models import Usage
from apps.tenancy.models import Tenant

logger = logging.getLogger(__name__)


def accumulate(
    tenant_id: UUID | str,
    meter: str,
    quantity: int | float | Decimal,
    period_start: date,
    period_end: date,
) -> Usage:
    """Bump ``control_usage.counters[meter]`` by ``quantity`` for the period.

    Creates the usage row when missing. Quantity is coerced to int (meters are
    integer counters in the control plane).
    """
    if not meter:
        raise ValueError("meter is required")
    qty = int(quantity)
    with transaction.atomic():
        tenant = Tenant.objects.select_for_update().get(pk=tenant_id)
        usage, _created = Usage.objects.select_for_update().get_or_create(
            tenant=tenant,
            period_start=period_start,
            period_end=period_end,
            defaults={"counters": {}},
        )
        counters: dict[str, Any] = dict(usage.counters or {})
        current = int(counters.get(meter, 0) or 0)
        counters[meter] = current + qty
        usage.counters = counters
        usage.save(update_fields=["counters", "updated_at"])
        logger.debug(
            "usage accumulate tenant=%s meter=%s +%s -> %s",
            tenant.slug,
            meter,
            qty,
            counters[meter],
        )
        return usage
