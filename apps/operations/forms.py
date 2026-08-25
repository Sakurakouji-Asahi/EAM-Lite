import uuid

from django import forms
from django.core.exceptions import ValidationError


class ManualBackupForm(forms.Form):
    current_password = forms.CharField(
        label="当前登录密码",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "class": "form-control"}
        ),
    )
    backup_passphrase = forms.CharField(
        label="备份加密口令",
        strip=False,
        min_length=12,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "new-password", "class": "form-control"}
        ),
        help_text="至少 12 个字符；口令不会保存，遗失后无法恢复备份。",
    )
    backup_passphrase_confirm = forms.CharField(
        label="再次输入备份加密口令",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "new-password", "class": "form-control"}
        ),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, actor, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.initial.setdefault("idempotency_key", f"manual-{uuid.uuid4()}")

    def clean_current_password(self):
        value = self.cleaned_data["current_password"]
        if not self.actor.check_password(value):
            raise ValidationError("当前登录密码不正确。")
        return value

    def clean(self):
        cleaned = super().clean()
        first = cleaned.get("backup_passphrase")
        second = cleaned.get("backup_passphrase_confirm")
        if first and second and first != second:
            self.add_error("backup_passphrase_confirm", "两次输入的备份口令不一致。")
        return cleaned


class BackupDownloadAuthorizationForm(forms.Form):
    current_password = forms.CharField(
        label="当前登录密码",
        strip=False,
        widget=forms.PasswordInput(
            attrs={"autocomplete": "current-password", "class": "form-control"}
        ),
    )
    idempotency_key = forms.CharField(widget=forms.HiddenInput)

    def __init__(self, *args, actor, **kwargs):
        self.actor = actor
        super().__init__(*args, **kwargs)
        self.initial.setdefault("idempotency_key", f"download-{uuid.uuid4()}")

    def clean_current_password(self):
        value = self.cleaned_data["current_password"]
        if not self.actor.check_password(value):
            raise ValidationError("当前登录密码不正确。")
        return value
