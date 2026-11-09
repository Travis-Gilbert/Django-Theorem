"""Store placement: where a tenant's graph lives, and how it moves.

The Rust front reads these rows (read-only) to route each request to the
tenant's store. A tenant without a placement has no store and is refused.
"""

from __future__ import annotations

from io import StringIO
from uuid import uuid4

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction

from apps.tenancy.models import (
    PlacementTransitionError,
    Store,
    StorePlacement,
    Tenant,
)

pytestmark = pytest.mark.django_db


def _tenant(name: str = "placed") -> Tenant:
    return Tenant.objects.create(slug=f"{name}-{uuid4()}", display_name=name)


def _store(name: str) -> Store:
    slug = f"{name}-{uuid4().hex[:8]}"
    url = f"http://{slug}.internal:8380"
    return Store.objects.create(slug=slug, http_url=url, grpc_url=url)


def test_personal_tenant_is_seeded_on_todays_store():
    placement = StorePlacement.objects.select_related("tenant", "store").get(
        tenant__slug="Travis-Gilbert"
    )
    assert placement.store.slug == "travis-theorem-personal-store"
    assert placement.store.http_url == "http://travis-theorem-personal-store.internal:8380"
    assert placement.store.grpc_url == placement.store.http_url
    assert placement.state == StorePlacement.State.SERVING
    assert placement.target_store is None
    assert placement.epoch == 1


def test_a_tenant_without_a_placement_has_none():
    tenant = _tenant("unplaced")
    assert not StorePlacement.objects.filter(tenant=tenant).exists()


def test_tenants_can_sit_on_different_stores():
    a, b = _store("store-a"), _store("store-b")
    first = StorePlacement.objects.create(tenant=_tenant("first"), store=a)
    second = StorePlacement.objects.create(tenant=_tenant("second"), store=b)
    assert first.store != second.store


def test_a_tenant_has_one_placement():
    tenant, store = _tenant(), _store("only")
    StorePlacement.objects.create(tenant=tenant, store=store)
    with pytest.raises(IntegrityError), transaction.atomic():
        StorePlacement.objects.create(tenant=tenant, store=store)


def test_serving_placement_cannot_name_a_target():
    a, b = _store("a"), _store("b")
    with pytest.raises(IntegrityError), transaction.atomic():
        StorePlacement.objects.create(tenant=_tenant(), store=a, target_store=b)


def test_a_moving_placement_needs_a_different_target():
    a = _store("a")
    with pytest.raises(IntegrityError), transaction.atomic():
        StorePlacement.objects.create(
            tenant=_tenant(), store=a, state=StorePlacement.State.COPYING
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        StorePlacement.objects.create(
            tenant=_tenant(), store=a, state=StorePlacement.State.COPYING, target_store=a
        )


def test_a_move_runs_copy_drain_complete_and_bumps_the_epoch():
    a, b = _store("a"), _store("b")
    placement = StorePlacement.objects.create(tenant=_tenant(), store=a)

    placement.begin_move(b)
    assert (placement.state, placement.store, placement.target_store) == ("copying", a, b)
    placement.drain()
    assert placement.state == "draining"
    placement.complete_move()
    placement.refresh_from_db()
    assert (placement.state, placement.store, placement.target_store) == ("serving", b, None)
    assert placement.epoch == 2


def test_an_aborted_move_stays_on_the_source():
    a, b = _store("a"), _store("b")
    placement = StorePlacement.objects.create(tenant=_tenant(), store=a)
    placement.begin_move(b)
    placement.drain()
    placement.abort_move()
    placement.refresh_from_db()
    assert (placement.state, placement.store, placement.target_store) == ("serving", a, None)
    assert placement.epoch == 1


def test_out_of_order_steps_are_refused():
    a, b = _store("a"), _store("b")
    placement = StorePlacement.objects.create(tenant=_tenant(), store=a)
    with pytest.raises(PlacementTransitionError):
        placement.drain()
    with pytest.raises(PlacementTransitionError):
        placement.complete_move()
    with pytest.raises(PlacementTransitionError):
        placement.abort_move()
    with pytest.raises(PlacementTransitionError):
        placement.begin_move(a)
    placement.begin_move(b)
    with pytest.raises(PlacementTransitionError):
        placement.begin_move(b)
    with pytest.raises(PlacementTransitionError):
        placement.complete_move()


def test_move_command_walks_a_tenant_to_a_new_store():
    a, b = _store("a"), _store("b")
    tenant = _tenant()
    StorePlacement.objects.create(tenant=tenant, store=a)
    out = StringIO()
    call_command("move_tenant_store", tenant.slug, "begin", b.slug, stdout=out)
    call_command("move_tenant_store", tenant.slug, "drain", stdout=out)
    call_command("move_tenant_store", tenant.slug, "complete", stdout=out)
    placement = StorePlacement.objects.get(tenant=tenant)
    assert placement.store == b and placement.state == "serving"


def test_move_command_refuses_an_unplaced_tenant_and_a_bad_step():
    tenant = _tenant()
    with pytest.raises(CommandError, match="no store placement"):
        call_command("move_tenant_store", tenant.slug, "drain")
    StorePlacement.objects.create(tenant=tenant, store=_store("a"))
    with pytest.raises(CommandError, match="must be draining"):
        call_command("move_tenant_store", tenant.slug, "complete")
    with pytest.raises(CommandError, match="no store missing"):
        call_command("move_tenant_store", tenant.slug, "begin", "missing")
