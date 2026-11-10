"""Tenant and project models — D3 read-model surface for Rust."""

from __future__ import annotations

import uuid

from django.db import models, transaction


class Tenant(models.Model):
    """control_tenant — Rust SELECT: id, slug, display_name, is_active."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=128, unique=True)
    display_name = models.CharField(max_length=255)
    is_active = models.BooleanField(default=True)
    workos_organization_id = models.CharField(
        max_length=128, blank=True, default="", db_index=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_tenant"
        ordering = ["slug"]

    def __str__(self) -> str:
        return f"{self.slug} ({self.display_name})"


class Project(models.Model):
    """control_project — Rust SELECT: tenant_id, slug, display_name."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="projects", db_column="tenant_id"
    )
    slug = models.SlugField(max_length=128)
    display_name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_project"
        unique_together = [("tenant", "slug")]
        ordering = ["slug"]

    def __str__(self) -> str:
        return f"{self.tenant.slug}/{self.slug}"


class Store(models.Model):
    """control_store — one RustyRed store a front can route a tenant to.

    Rust SELECT: id, slug, http_url, grpc_url. The front holds no store of its
    own; it reads ``StorePlacement`` to find each tenant's.

    ``fly_app`` names the Fly app the provisioner created for this store, empty
    for a store an operator registered by hand. Rust does not read it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    slug = models.SlugField(max_length=128, unique=True)
    http_url = models.URLField(max_length=512)
    grpc_url = models.URLField(max_length=512)
    fly_app = models.CharField(max_length=63, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_store"
        ordering = ["slug"]

    def __str__(self) -> str:
        return self.slug


class PlacementTransitionError(Exception):
    """A move step was asked of a placement in the wrong state."""


class StorePlacement(models.Model):
    """control_storeplacement — which store holds a tenant's graph.

    Rust SELECT: tenant_id, store_id, state, target_store_id, epoch.

    A new tenant's store is built first:

    - ``provisioning``: the provisioner is creating ``store`` on Fly; the
      front refuses with a retryable error.
    - ``failed``: provisioning stopped with ``last_error``; the front refuses
      until an operator retries (``manage.py provision_tenant_store``).
    - ``serving`` once the store answers its readiness check.

    A move between stores is four states the Rust front reads and routes by:

    - ``serving``: ``store`` answers everything; no move is in flight.
    - ``copying``: the graph is being copied to ``target_store``; ``store``
      still answers everything.
    - ``draining``: the final delta is copying; the tenant answers nothing and
      the front refuses with a retryable error.
    - back to ``serving`` on ``target_store``, with ``epoch`` bumped.

    The front caches each placement for 30 seconds, so the operator waits that
    long after each step before taking the next. Use the transition methods
    (or ``manage.py move_tenant_store``) rather than editing the row.
    """

    class State(models.TextChoices):
        SERVING = "serving", "Serving"
        COPYING = "copying", "Copying to target"
        DRAINING = "draining", "Draining to target"
        PROVISIONING = "provisioning", "Provisioning"
        FAILED = "failed", "Provisioning failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name="placement", db_column="tenant_id"
    )
    store = models.ForeignKey(
        Store, on_delete=models.PROTECT, related_name="placements", db_column="store_id"
    )
    state = models.CharField(max_length=16, choices=State.choices, default=State.SERVING)
    target_store = models.ForeignKey(
        Store,
        on_delete=models.PROTECT,
        related_name="incoming_placements",
        db_column="target_store_id",
        null=True,
        blank=True,
    )
    epoch = models.PositiveBigIntegerField(default=1)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_storeplacement"
        ordering = ["tenant__slug"]
        constraints = [
            models.CheckConstraint(
                name="control_storeplacement_target_only_while_moving",
                condition=(
                    models.Q(
                        state__in=["serving", "provisioning", "failed"],
                        target_store__isnull=True,
                    )
                    | (
                        models.Q(state__in=["copying", "draining"], target_store__isnull=False)
                        & ~models.Q(target_store=models.F("store"))
                    )
                ),
            ),
        ]

    def __str__(self) -> str:
        if self.target_store_id:
            return f"{self.tenant.slug} on {self.store.slug} ({self.state} to {self.target_store.slug})"
        return f"{self.tenant.slug} on {self.store.slug}"

    def mark_provisioned(self) -> None:
        """Serve the tenant from its newly built store."""
        with transaction.atomic():
            locked = self._locked()
            if locked.state != self.State.PROVISIONING:
                raise PlacementTransitionError(f"{locked} is not provisioning")
            locked.state = self.State.SERVING
            locked.last_error = ""
            locked.save(update_fields=["state", "last_error", "updated_at"])
        self.refresh_from_db()

    def mark_failed(self, error: str) -> None:
        """Stop provisioning and keep the reason for the operator."""
        with transaction.atomic():
            locked = self._locked()
            if locked.state != self.State.PROVISIONING:
                raise PlacementTransitionError(f"{locked} is not provisioning")
            locked.state = self.State.FAILED
            locked.last_error = error
            locked.save(update_fields=["state", "last_error", "updated_at"])
        self.refresh_from_db()

    def retry_provisioning(self) -> None:
        """Send a failed placement back to the provisioner."""
        self._step(self.State.FAILED, self.State.PROVISIONING)

    def begin_move(self, target: Store) -> None:
        """Start copying this tenant's graph to ``target``."""
        with transaction.atomic():
            locked = self._locked()
            if locked.state != self.State.SERVING:
                raise PlacementTransitionError(f"{locked} must be serving, not {locked.state}")
            if target.pk == locked.store_id:
                raise PlacementTransitionError(f"{locked.tenant.slug} is already on {target.slug}")
            locked.state = self.State.COPYING
            locked.target_store = target
            locked.save(update_fields=["state", "target_store", "updated_at"])
        self.refresh_from_db()

    def drain(self) -> None:
        """Stop serving the tenant while the final delta copies."""
        self._step(self.State.COPYING, self.State.DRAINING)

    def complete_move(self) -> None:
        """Serve the tenant from the target store under a new epoch."""
        with transaction.atomic():
            locked = self._locked()
            if locked.state != self.State.DRAINING:
                raise PlacementTransitionError(f"{locked} must be draining to complete")
            locked.store = locked.target_store
            locked.target_store = None
            locked.state = self.State.SERVING
            locked.epoch += 1
            locked.save(update_fields=["store", "target_store", "state", "epoch", "updated_at"])
        self.refresh_from_db()

    def abort_move(self) -> None:
        """Keep the tenant on its source store and forget the target."""
        with transaction.atomic():
            locked = self._locked()
            if locked.state not in (self.State.COPYING, self.State.DRAINING):
                raise PlacementTransitionError(f"{locked} is not moving")
            locked.state = self.State.SERVING
            locked.target_store = None
            locked.save(update_fields=["state", "target_store", "updated_at"])
        self.refresh_from_db()

    def _step(self, expected: str, to: str) -> None:
        with transaction.atomic():
            locked = self._locked()
            if locked.state != expected:
                raise PlacementTransitionError(f"{locked} must be {expected}, not {locked.state}")
            locked.state = to
            locked.save(update_fields=["state", "updated_at"])
        self.refresh_from_db()

    def _locked(self) -> "StorePlacement":
        # Lock the placement row only: Postgres refuses FOR UPDATE on the
        # nullable side of the target_store outer join.
        return (
            StorePlacement.objects.select_for_update(of=("self",))
            .select_related("tenant", "store", "target_store")
            .get(pk=self.pk)
        )


class EnvironmentHost(models.Model):
    """control_environmenthost — the coding environment a tenant works in.

    Each tenant gets one Fly Sprite running ``theorem-ide-host``. The tenant's
    store reaches it at ``url`` with a bearer token derived from
    ``ENVIRONMENT_TOKEN_KEY``; the binding travels to the store in its machine
    config, so Rust does not read this table.

    - ``provisioning``: the sprite is being created or brought up to date.
    - ``serving``: the host answered as this tenant's; ``host_sha256`` is the
      binary it runs.
    - ``failed``: provisioning stopped with ``last_error``; run
      ``manage.py provision_tenant_environment`` to retry.
    """

    class State(models.TextChoices):
        PROVISIONING = "provisioning", "Provisioning"
        SERVING = "serving", "Serving"
        FAILED = "failed", "Provisioning failed"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant = models.OneToOneField(
        Tenant, on_delete=models.CASCADE, related_name="environment_host", db_column="tenant_id"
    )
    sprite = models.CharField(max_length=63, unique=True)
    url = models.URLField(max_length=512, blank=True, default="")
    host_sha256 = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=16, choices=State.choices, default=State.PROVISIONING)
    last_error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "control_environmenthost"
        ordering = ["tenant__slug"]

    def __str__(self) -> str:
        return f"{self.tenant.slug} on sprite {self.sprite}"

    def mark_serving(self, *, url: str, host_sha256: str) -> None:
        """Record the host as answering for its tenant, new or brought up to date."""
        with transaction.atomic():
            locked = self._locked()
            if locked.state == self.State.FAILED:
                raise PlacementTransitionError(f"{locked} failed; retry it first")
            locked.state = self.State.SERVING
            locked.url = url
            locked.host_sha256 = host_sha256
            locked.last_error = ""
            locked.save(update_fields=["state", "url", "host_sha256", "last_error", "updated_at"])
        self.refresh_from_db()

    def mark_failed(self, error: str) -> None:
        """Stop provisioning and keep the reason for the operator."""
        with transaction.atomic():
            locked = self._locked()
            if locked.state != self.State.PROVISIONING:
                raise PlacementTransitionError(f"{locked} is not provisioning")
            locked.state = self.State.FAILED
            locked.last_error = error
            locked.save(update_fields=["state", "last_error", "updated_at"])
        self.refresh_from_db()

    def retry(self) -> None:
        """Send a failed host back to the provisioner."""
        with transaction.atomic():
            locked = self._locked()
            if locked.state != self.State.FAILED:
                raise PlacementTransitionError(f"{locked} must be failed, not {locked.state}")
            locked.state = self.State.PROVISIONING
            locked.save(update_fields=["state", "updated_at"])
        self.refresh_from_db()

    def _locked(self) -> "EnvironmentHost":
        return (
            EnvironmentHost.objects.select_for_update(of=("self",))
            .select_related("tenant")
            .get(pk=self.pk)
        )
