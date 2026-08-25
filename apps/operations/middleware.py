from django.db import DatabaseError
from django.http import HttpResponse


class BackupWriteFreezeMiddleware:
    """Freeze ordinary HTTP mutations during a database/media backup window."""

    SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
    EXEMPT_PATHS = frozenset({"/login/", "/logout/"})

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.method not in self.SAFE_METHODS and request.path not in self.EXEMPT_PATHS:
            try:
                from apps.operations.models import BackupSet

                frozen = BackupSet.objects.filter(status=BackupSet.Status.PENDING).exists()
            except DatabaseError:
                frozen = False
            if frozen:
                response = HttpResponse(
                    "系统正在生成数据库与附件一致性备份，请稍后重试本次写操作。",
                    status=503,
                    content_type="text/plain; charset=utf-8",
                )
                response["Retry-After"] = "60"
                response["Cache-Control"] = "no-store"
                return response
        return self.get_response(request)
