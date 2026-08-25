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

ROLE_LABELS = {
    "system_admin": "系统管理员",
    "finance": "财务",
    "equipment": "设备管理",
    "department_manager": "部门负责人",
    "employee": "员工",
    "warehouse": "仓库",
    "hr": "人事",
    "management": "管理层",
}

ROLE_CHOICES = tuple((name, ROLE_LABELS[name]) for name in ROLE_NAMES)


def ensure_fixed_roles():
    return [Group.objects.get_or_create(name=name)[0] for name in ROLE_NAMES]
