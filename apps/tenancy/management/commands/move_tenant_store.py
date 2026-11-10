"""Move a tenant's graph between stores, one step at a time.

    manage.py move_tenant_store Travis-Gilbert begin travis-theorem-store-2
    (copy the graph to the target store)
    manage.py move_tenant_store Travis-Gilbert drain
    (copy the final delta)
    manage.py move_tenant_store Travis-Gilbert complete

No front starts a store call on a placement older than two minutes, and no call
runs longer than one more: wait three minutes after each step.
``abort`` returns a moving tenant to its source store.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from apps.tenancy.models import PlacementTransitionError, Store, StorePlacement


class Command(BaseCommand):
    help = "Advance a tenant's store move: begin <store>, drain, complete, or abort."

    def add_arguments(self, parser):
        parser.add_argument("tenant", help="Tenant slug")
        parser.add_argument("step", choices=["begin", "drain", "complete", "abort"])
        parser.add_argument("store", nargs="?", help="Target store slug (begin only)")

    def handle(self, *args, tenant: str, step: str, store: str | None, **options):
        try:
            placement = StorePlacement.objects.select_related("tenant", "store").get(
                tenant__slug=tenant
            )
        except StorePlacement.DoesNotExist as error:
            raise CommandError(f"tenant {tenant} has no store placement") from error
        try:
            if step == "begin":
                if not store:
                    raise CommandError("begin needs a target store slug")
                try:
                    target = Store.objects.get(slug=store)
                except Store.DoesNotExist as error:
                    raise CommandError(f"no store {store}") from error
                placement.begin_move(target)
            elif step == "drain":
                placement.drain()
            elif step == "complete":
                placement.complete_move()
            else:
                placement.abort_move()
        except PlacementTransitionError as error:
            raise CommandError(str(error)) from error
        self.stdout.write(f"{placement} (epoch {placement.epoch})")
