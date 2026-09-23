from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("assets", "0021_custody_correction_guards")]

    operations = [
        migrations.AddField(
            model_name="asset",
            name="equipment_number",
            field=models.CharField("设备编号", max_length=200, blank=True, default=""),
        ),
    ]
