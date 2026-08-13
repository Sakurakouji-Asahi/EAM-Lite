"""Validated filters for the read-only AuditLog query page."""

from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.audit.permissions import AUDIT_OBJECT_TYPE_REGISTRY


AUDIT_PAGE_SIZE_DEFAULT = 50
AUDIT_PAGE_SIZE_MAX = 100
_DATETIME_INPUT_FORMAT = "%Y-%m-%dT%H:%M"
_DATETIME_DEFAULT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def _default_time_range():
    end_at = timezone.localtime(timezone.now())
    return end_at - timedelta(days=7), end_at


class AuditLogFilterForm(forms.Form):
    start_at = forms.DateTimeField(
        label="开始时间（上海）",
        input_formats=(_DATETIME_INPUT_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d"),
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"}, format=_DATETIME_INPUT_FORMAT
        ),
    )
    end_at = forms.DateTimeField(
        label="结束时间（上海）",
        input_formats=(_DATETIME_INPUT_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d"),
        widget=forms.DateTimeInput(
            attrs={"type": "datetime-local"}, format=_DATETIME_INPUT_FORMAT
        ),
    )
    actor = forms.ModelChoiceField(
        label="操作者",
        required=False,
        queryset=get_user_model().objects.none(),
        empty_label="全部操作者",
    )
    action = forms.CharField(label="动作（精确）", required=False, max_length=100)
    object_type = forms.ChoiceField(
        label="对象类型（精确）",
        required=False,
        choices=(),
    )
    object_id = forms.CharField(
        label="对象 ID（精确）", required=False, max_length=255
    )
    correlation_id = forms.UUIDField(label="关联 ID（精确）", required=False)
    page_size = forms.IntegerField(
        label="每页条数",
        min_value=1,
        max_value=AUDIT_PAGE_SIZE_MAX,
        initial=AUDIT_PAGE_SIZE_DEFAULT,
    )

    def __init__(self, data=None, *, company=None, actor_queryset=None, **kwargs):
        start_at, end_at = _default_time_range()
        if data is not None:
            data = data.copy()
            if not data.get("start_at"):
                data["start_at"] = start_at.strftime(_DATETIME_DEFAULT_FORMAT)
            if not data.get("end_at"):
                data["end_at"] = end_at.strftime(_DATETIME_DEFAULT_FORMAT)
            if not data.get("page_size"):
                data["page_size"] = str(AUDIT_PAGE_SIZE_DEFAULT)
        kwargs.setdefault(
            "initial",
            {
                "start_at": start_at,
                "end_at": end_at,
                "page_size": AUDIT_PAGE_SIZE_DEFAULT,
            },
        )
        super().__init__(data=data, **kwargs)
        self.fields["object_type"].choices = [
            ("", "全部对象类型"),
            *sorted(
                AUDIT_OBJECT_TYPE_REGISTRY.items(),
                key=lambda item: (item[1], item[0]),
            ),
        ]
        if actor_queryset is not None:
            self.fields["actor"].queryset = actor_queryset
        elif company is not None:
            self.fields["actor"].queryset = (
                get_user_model()
                .objects.filter(audit_logs__company=company)
                .distinct()
                .order_by("username")
            )

    def clean(self):
        cleaned = super().clean()
        start_at = cleaned.get("start_at")
        end_at = cleaned.get("end_at")
        if start_at and end_at and start_at > end_at:
            raise forms.ValidationError("开始时间不能晚于结束时间。")
        return cleaned


__all__ = [
    "AUDIT_PAGE_SIZE_DEFAULT",
    "AUDIT_PAGE_SIZE_MAX",
    "AuditLogFilterForm",
]
