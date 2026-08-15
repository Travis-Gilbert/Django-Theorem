from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orchestration", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="job",
            name="operation_id",
            field=models.CharField(
                db_index=True,
                help_text="Tenant-scoped idempotency key from cache_key_for_operation",
                max_length=128,
            ),
        ),
        migrations.AddConstraint(
            model_name="job",
            constraint=models.UniqueConstraint(
                fields=("tenant", "operation_id"),
                name="control_job_tenant_operation_id_uniq",
            ),
        ),
    ]
