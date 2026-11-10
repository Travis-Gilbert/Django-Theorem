"""Place the personal tenant on the store it already uses.

Before placements existed, the personal front pinned this store in its Fly
config (``THEOREM_STORE_URL``). This row is that pin, moved to where the front
now reads it. The store's port 8380 serves both HTTP and gRPC.
"""

from django.db import migrations

PERSONAL_TENANT = "Travis-Gilbert"
PERSONAL_STORE = "travis-theorem-personal-store"
PERSONAL_STORE_URL = "http://travis-theorem-personal-store.internal:8380"


def place_personal_tenant(apps, schema_editor):
    Tenant = apps.get_model("tenancy", "Tenant")
    Store = apps.get_model("tenancy", "Store")
    StorePlacement = apps.get_model("tenancy", "StorePlacement")
    tenant, _ = Tenant.objects.get_or_create(
        slug=PERSONAL_TENANT, defaults={"display_name": "Travis Gilbert"}
    )
    store, _ = Store.objects.get_or_create(
        slug=PERSONAL_STORE,
        defaults={"http_url": PERSONAL_STORE_URL, "grpc_url": PERSONAL_STORE_URL},
    )
    StorePlacement.objects.get_or_create(tenant=tenant, defaults={"store": store})


def unplace_personal_tenant(apps, schema_editor):
    Store = apps.get_model("tenancy", "Store")
    StorePlacement = apps.get_model("tenancy", "StorePlacement")
    StorePlacement.objects.filter(
        tenant__slug=PERSONAL_TENANT, store__slug=PERSONAL_STORE
    ).delete()
    Store.objects.filter(
        slug=PERSONAL_STORE,
        placements__isnull=True,
        incoming_placements__isnull=True,
    ).delete()


class Migration(migrations.Migration):
    dependencies = [("tenancy", "0002_store_placement")]

    operations = [migrations.RunPython(place_personal_tenant, unplace_personal_tenant)]
