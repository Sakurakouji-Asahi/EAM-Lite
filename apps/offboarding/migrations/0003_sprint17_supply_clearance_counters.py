from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("offboarding", "0002_postgresql_clearance_guards"),
    ]

    operations = [
        migrations.AddField(
            model_name="employeeassetclearance",
            name="total_supply_custodies_snapshot",
            field=models.PositiveIntegerField(
                default=0, verbose_name="数量型耐用品保管总数"
            ),
        ),
        migrations.AddField(
            model_name="employeeassetclearance",
            name="unresolved_supply_custodies",
            field=models.PositiveIntegerField(
                default=0, verbose_name="未解决数量型耐用品保管数"
            ),
        ),
        migrations.AddConstraint(
            model_name="employeeassetclearance",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    unresolved_supply_custodies__lte=models.F(
                        "total_supply_custodies_snapshot"
                    )
                ),
                name="ck_clearance_supply_counts",
            ),
        ),
    ]
