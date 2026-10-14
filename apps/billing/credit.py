"""The subscriber's credit for the current period: what it spent, what it may.

Spend is the ``credit_usd_micros`` meter that ``usage.accumulate`` bumps,
kept in millionths of a dollar because meters are integer counters. The
allowance is ``monthly_credit_usd`` in the active plan's limits; a plan that
does not set it has no stated limit, which is not the same as a zero one.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from apps.billing.models import Subscription, Usage

CREDIT_METER = "credit_usd_micros"
CREDIT_LIMIT = "monthly_credit_usd"
MICROS_PER_USD = Decimal(1_000_000)


def _meter_micros(counters: object) -> int:
    """Integer micros already spent; unreadable or negative counters are none."""
    if not isinstance(counters, dict):
        return 0
    raw = counters.get(CREDIT_METER, 0)
    try:
        micros = int(raw or 0)
    except (TypeError, ValueError):
        return 0
    return max(micros, 0)


def _plan_limit(raw: object) -> Decimal | None:
    """A non-negative finite allowance, or none when the plan does not state one."""
    if raw is None:
        return None
    try:
        limit = Decimal(str(raw))
    except (ArithmeticError, ValueError, TypeError):
        return None
    if not limit.is_finite() or limit < 0:
        return None
    return limit


@dataclass(frozen=True)
class CreditAllowance:
    period_start: date
    period_end: date
    used_usd: Decimal
    limit_usd: Decimal | None


def calendar_period(today: date) -> tuple[date, date]:
    """The calendar month holding ``today``, first and last day inclusive."""
    last = calendar.monthrange(today.year, today.month)[1]
    return today.replace(day=1), today.replace(day=last)


def credit_allowance(tenant_id: UUID | str, today: date) -> CreditAllowance:
    """Spend so far and the plan's allowance for the period holding ``today``.

    The usage row that covers ``today`` is the period; without one nothing has
    been spent yet, and the period is the calendar month.
    """
    usage = (
        Usage.objects.filter(
            tenant_id=tenant_id, period_start__lte=today, period_end__gte=today
        )
        .order_by("-period_start")
        .first()
    )
    if usage is None:
        period_start, period_end = calendar_period(today)
        micros = 0
    else:
        period_start, period_end = usage.period_start, usage.period_end
        micros = _meter_micros(usage.counters)

    subscription = (
        Subscription.objects.select_related("plan")
        .filter(tenant_id=tenant_id, status=Subscription.Status.ACTIVE)
        .order_by("-created_at")
        .first()
    )
    limit = None
    if subscription is not None:
        limit = _plan_limit((subscription.plan.limits or {}).get(CREDIT_LIMIT))

    return CreditAllowance(
        period_start=period_start,
        period_end=period_end,
        used_usd=Decimal(micros) / MICROS_PER_USD,
        limit_usd=limit,
    )
