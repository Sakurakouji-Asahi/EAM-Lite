from django.contrib.auth.models import Group


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


def ensure_fixed_roles():
    return [Group.objects.get_or_create(name=name)[0] for name in ROLE_NAMES]
