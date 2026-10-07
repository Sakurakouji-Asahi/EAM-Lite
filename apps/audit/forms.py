"""Validated filters for the read-only AuditLog query page."""

from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.audit.permissions import AUDIT_OBJECT_TYPE_REGISTRY


AUDIT_PAGE_SIZE_DEFAULT = 50
AUDIT_PAGE_SIZE_MAX = 100
_DATETIME_INPUT_FORMAT = "%Y-%m-%dT%H:%M"
_DATETIME_DEFAULT_FORMAT = "%Y-%m-%dT%H:%M:%S.%f"


def _default_time_range():
    end_at = timezone.localtime(timezone.now())
    end_at = end_at.replace(microsecond=end_at.microsecond // 1000 * 1000)
    return end_at - timedelta(days=7), end_at


class AuditDateTimeInput(forms.DateTimeInput):
    """Display defaults and offset-bearing links as valid datetime-local values."""

    def format_value(self, value):
        if isinstance(value, str):
            try:
                value = parse_datetime(value) or value
            except ValueError:
                pass
        if hasattr(value, "strftime"):
            if timezone.is_aware(value):
                value = timezone.localtime(value)
            return value.strftime(_DATETIME_DEFAULT_FORMAT)[:-3]
        return super().format_value(value)


class AuditLogFilterForm(forms.Form):
    q = forms.CharField(label='业务编号、名称或中文动作',required=False,max_length=200,
        help_text='支持资产/设备编号、库存单据号、员工编号/姓名、盘点任务编号/名称、保养计划名称和中文动作。',
        widget=forms.TextInput(attrs={'placeholder':'输入编号、人员、任务或计划名称'}))
    start_at = forms.DateTimeField(
        label="开始时间（上海）",
        input_formats=(_DATETIME_INPUT_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d"),
        widget=AuditDateTimeInput(
            attrs={"type": "datetime-local", "step": "0.001"}, format=_DATETIME_INPUT_FORMAT
        ),
    )
    end_at = forms.DateTimeField(
        label="结束时间（上海）",
        input_formats=(_DATETIME_INPUT_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d"),
        widget=AuditDateTimeInput(
            attrs={"type": "datetime-local", "step": "0.001"}, format=_DATETIME_INPUT_FORMAT
        ),
    )
    actor = forms.ModelChoiceField(
        label="操作者",
        required=False,
        queryset=get_user_model().objects.none(),
        empty_label="全部操作者",
    )
    action = forms.CharField(
        label="动作代码（精确）",
        required=False,
        max_length=100,
        help_text="可留空；需要精确筛选时，可从结果中的技术代码复制。",
    )
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
        for field in self.fields.values():
            field.widget.attrs.setdefault("class", "form-select" if isinstance(field.widget, forms.Select) else "form-control")

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
