import logging

from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError

from apps.accounts.login_rate_limit import login_rate_limit_scope
from apps.audit.services import request_audit_context


logger = logging.getLogger("eam_lite.security")


class ChineseAuthenticationForm(AuthenticationForm):
    error_messages = {
        "invalid_login": "用户名或密码错误，请重新输入。",
        "inactive": "用户名或密码错误，请重新输入。",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["username"].label = "用户名"
        self.fields["password"].label = "密码"
        self.fields["username"].widget.attrs.update(
            {"class": "form-control", "autocomplete": "username"}
        )
        self.fields["password"].widget.attrs.update(
            {"class": "form-control", "autocomplete": "current-password"}
        )

    def clean(self):
        context = request_audit_context(self.request)
        scope = login_rate_limit_scope(
            ip_address=context["ip_address"],
            username=self.cleaned_data.get("username", ""),
        )
        if scope is not None:
            logger.warning(
                "登录尝试已限速 ip=%s scope=%s",
                context["ip_address"],
                scope,
            )
            raise ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
            )
        return super().clean()
