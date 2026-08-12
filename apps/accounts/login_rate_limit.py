from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from apps.audit.models import AuditLog


def login_rate_limit_scope(*, ip_address, username):
    cutoff = timezone.now() - timedelta(
        seconds=settings.LOGIN_FAILURE_WINDOW_SECONDS
    )
    failures = AuditLog.objects.filter(
        action="auth.login_failed",
        ip_address=ip_address,
        created_at__gte=cutoff,
    )
    username = str(username or "").strip()
    if not username:
        return None

    latest_success = (
        AuditLog.objects.filter(
            action="auth.login_succeeded",
            ip_address=ip_address,
            new_data_json__username=username,
            created_at__gte=cutoff,
        )
        .order_by("-created_at")
        .values_list("created_at", flat=True)
        .first()
    )
    pair_failures = failures.filter(new_data_json__username=username)
    if latest_success is not None:
        pair_failures = pair_failures.filter(created_at__gt=latest_success)
    if pair_failures.count() >= settings.LOGIN_FAILURE_PAIR_LIMIT:
        return "pair"
    return None
