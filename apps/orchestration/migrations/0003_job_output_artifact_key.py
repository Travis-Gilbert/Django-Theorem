from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("orchestration", "0002_job_tenant_operation_id_unique"),
    ]

    operations = [
        migrations.AddField(
            model_name="job",
            name="output_artifact_key",
            field=models.CharField(blank=True, default="", max_length=512),
        ),
    ]
