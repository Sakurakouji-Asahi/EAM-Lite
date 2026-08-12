from django.db import migrations


ROLE_NAMES = (
    "system_admin",
    "finance",
    "equipment",
    "department_manager",
    "employee",
    "warehouse",
    "hr",
    "management",
)


def seed_fixed_roles(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    for role_name in ROLE_NAMES:
        Group.objects.get_or_create(name=role_name)


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_fixed_roles, migrations.RunPython.noop),
    ]
