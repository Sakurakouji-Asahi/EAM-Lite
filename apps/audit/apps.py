from django.apps import AppConfig


class AuditConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.audit"
    verbose_name = "审计"

    def ready(self):
        from apps.audit import signals  # noqa: F401
