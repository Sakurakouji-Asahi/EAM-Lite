from apps.operations.permissions import can_manage_backups


def operations_navigation(request):
    user = getattr(request, "user", None)
    return {"operations_nav": {"can_manage_backups": can_manage_backups(user)}}
