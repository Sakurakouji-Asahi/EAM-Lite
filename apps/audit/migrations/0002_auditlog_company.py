import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("audit", "0001_initial"),
        ("masterdata", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="auditlog",
            name="company",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="audit_logs",
                to="masterdata.company",
                verbose_name="公司",
            ),
        ),
    ]
