import logging

from django.contrib.auth.signals import user_logged_in, user_login_failed
from django.dispatch import receiver

from apps.audit.services import request_audit_context, write_audit_log


logger = logging.getLogger("eam_lite.security")


@receiver(user_logged_in, dispatch_uid="eam_lite.audit.login_succeeded")
def audit_login_succeeded(sender, request, user, **kwargs):
    context = request_audit_context(request)
    write_audit_log(
        user=user,
        action="auth.login_succeeded",
        object_type="UserAuthentication",
        object_id=user.pk,
        old_data={},
        new_data={"username": user.get_username()},
        **context,
    )
    logger.info("登录成功 user_id=%s ip=%s", user.pk, context["ip_address"])


@receiver(user_login_failed, dispatch_uid="eam_lite.audit.login_failed")
def audit_login_failed(sender, credentials, request, **kwargs):
    context = request_audit_context(request)
    attempted_username = credentials.get("username", "")
    write_audit_log(
        action="auth.login_failed",
        object_type="UserAuthentication",
        object_id="anonymous",
        old_data={},
        new_data={"username": attempted_username},
        **context,
    )
    logger.warning("登录失败 ip=%s", context["ip_address"])
