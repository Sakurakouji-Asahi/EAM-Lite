"""Bound Chinese forms for preventive-maintenance actions."""

from __future__ import annotations

import uuid

from django import forms
from apps.core.multi_upload import MultiFileField
from apps.core.query_forms import DateRangeQueryForm
from apps.core.form_widgets import normalize_date_widgets
from django.core.exceptions import PermissionDenied
from django.core.exceptions import ValidationError

from apps.assets.models import Asset
from apps.maintenance.domain import business_date
from apps.maintenance.completion_instance import (
    INVALID_INSTANCE_MESSAGE, completion_instance_token, decode_completion_instance,
)
from apps.maintenance.models import MaintenanceProblem, MaintenanceRecord
from apps.maintenance.plan_revision import decode_plan_edit_revision
from apps.maintenance.permissions import (
    can_close_maintenance_problem,
    can_complete_maintenance,
    can_manage_maintenance_attachment,
    can_manage_maintenance_plan,
    can_void_maintenance_record,
)
from apps.masterdata.models import Employee
from apps.masterdata.permissions import role_names_for


def _style(form):
    normalize_date_widgets(form)
    for field in form.fields.values():
        if isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput)):
            continue
        field.widget.attrs.setdefault(
            "class",
            "form-select" if isinstance(field.widget, forms.Select) else "form-control",
        )


class MaintenanceRecordFilterForm(DateRangeQueryForm):
    q = forms.CharField(label="设备或保养计划", required=False, max_length=200,
                        widget=forms.TextInput(attrs={"placeholder": "资产编号、设备编号、名称或保养计划"}))
    status = forms.ChoiceField(label="记录状态", required=False,
                               choices=(("", "全部状态"), *MaintenanceRecord.Status.choices))
    page_size = forms.TypedChoiceField(label="每页显示", required=False, coerce=int, initial=25,
                                       choices=((25,"25 条"),(50,"50 条"),(100,"100 条"),(200,"200 条")))
    field_order = ("q", "date_from", "date_to", "status", "page_size")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date_from"].label = "完成开始日期"
        self.fields["date_to"].label = "完成结束日期"
        _style(self)


class MaintenanceProblemFilterForm(DateRangeQueryForm):
    owner_employee = forms.ModelChoiceField(label="跟进负责人",required=False,queryset=Employee.objects.none())
    due_scope = forms.ChoiceField(label="期限情况",required=False,choices=(
        ('','全部期限'),('overdue','已逾期未关闭'),('today','今日到期未关闭'),
        ('week','未来 7 天到期未关闭'),('unassigned','待分派或未设期限'),
    ))
    mine = forms.BooleanField(label="只看本人跟进",required=False)
    q = forms.CharField(
        label="设备或问题", required=False, max_length=200,
        widget=forms.TextInput(attrs={"placeholder": "资产编号、设备编号、名称、计划或问题处理内容"}),
    )
    status = forms.ChoiceField(
        label="跟进状态", required=False,
        choices=(("", "全部状态"), *MaintenanceProblem.Status.choices),
    )
    page_size = forms.TypedChoiceField(
        label="每页显示", required=False, coerce=int, initial=25,
        choices=((25, "25 条"), (50, "50 条"), (100, "100 条"), (200, "200 条")),
    )
    field_order = ("q", "status", "owner_employee", "due_scope", "mine", "date_from", "date_to", "page_size")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["date_from"].label = "保养开始日期"
        self.fields["date_to"].label = "保养结束日期"
        _style(self)


class MaintenancePlanForm(forms.Form):
    expected_revision = forms.CharField(required=False, widget=forms.HiddenInput(),
        error_messages={"required": "缺少编辑页面版本，请重新打开最新编辑页面后核对。"})
    asset = forms.ModelChoiceField(label="资产", queryset=Asset.objects.none())
    name = forms.CharField(label="计划名称", max_length=200)
    cycle_value = forms.IntegerField(label="周期数值", min_value=1,
        help_text="与周期单位一起填写，例如 1 月或 2 周。编辑时可先预览下次到期日。")
    cycle_unit = forms.ChoiceField(
        label="周期单位",
        choices=(("day", "日"), ("week", "周"), ("month", "月"), ("year", "年")),
    )
    responsible_employee = forms.ModelChoiceField(
        label="责任人", queryset=Employee.objects.none()
    )
    advance_notice_days = forms.IntegerField(label="提前提醒天数", min_value=0,
        help_text="0 表示到期当天提醒；此项不会改变计划到期日。")
    standard_content = forms.CharField(label="标准内容", widget=forms.Textarea(attrs={"rows": 5}),
        help_text="写明需要检查、清洁或更换的项目和合格标准，供责任人现场执行时参考。")
    first_due_date = forms.DateField(
        label="首次到期日", widget=forms.DateInput(attrs={"type": "date"})
    )

    def __init__(self, *args, actor=None, company=None, instance=None, **kwargs):
        if actor is None or company is None:
            raise PermissionDenied("保养计划表单必须绑定用户和公司。")
        target = instance or Asset(company=company)
        if not can_manage_maintenance_plan(actor, target):
            raise PermissionDenied("只有 equipment 可以维护保养计划。")
        self.actor, self.company, self.instance = actor, company, instance
        super().__init__(*args, **kwargs)
        if instance is None:
            self.fields.pop("expected_revision")
        else:
            self.fields["expected_revision"].required = True
        self.fields["asset"].queryset = Asset.objects.filter(
            company=company,
            is_maintenance_required=True,
            record_status="active",
            asset_status__in=("in_use", "idle", "loaned", "under_repair"),
        )
        self.fields["responsible_employee"].queryset = Employee.objects.filter(
            company=company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        )
        if instance is not None:
            # Browsers omit disabled controls on POST. Bind the original asset
            # on every edit request and ignore any attempted replacement value.
            self.initial["asset"] = instance.asset_id
            self.fields["asset"].disabled = True
        if instance is not None and not self.is_bound:
            self.initial.update(
                {
                    "name": instance.name,
                    "cycle_value": instance.cycle_value,
                    "cycle_unit": instance.cycle_unit,
                    "responsible_employee": instance.responsible_employee_id,
                    "advance_notice_days": instance.advance_notice_days,
                    "standard_content": instance.standard_content,
                    "first_due_date": instance.first_due_date,
                }
            )
        _style(self)

    def clean_expected_revision(self):
        return decode_plan_edit_revision(token=self.cleaned_data["expected_revision"],
            actor=self.actor, company=self.company, plan=self.instance)


class MaintenanceCompletionForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    completion_instance = forms.CharField(
        widget=forms.HiddenInput, error_messages={"required": INVALID_INSTANCE_MESSAGE}
    )
    scheduled_date = forms.DateField(
        label="计划日期", widget=forms.DateInput(attrs={"type": "date"})
    )
    completed_date = forms.DateField(
        label="实际完成日期", widget=forms.DateInput(attrs={"type": "date"})
    )
    actual_content = forms.CharField(
        label="实际完成内容", widget=forms.Textarea(attrs={"rows": 5}),
        help_text="按现场实际完成情况填写；可引用上方标准内容后补充检查结果。",
    )
    result = forms.ChoiceField(
        label="结果", choices=(("normal", "正常"), ("problem_found", "发现问题"))
    )
    problem_description = forms.CharField(
        label="问题说明", required=False, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="发现问题时必填，说明现象、位置和影响；正常结果请保持为空。",
    )
    remark = forms.CharField(label="备注", required=False, widget=forms.Textarea(attrs={"rows": 3}))
    uploaded_file = MultiFileField(label="照片/附件", required=False)
    security_class = forms.ChoiceField(
        label="附件安全分类",
        choices=(("A0", "普通附件（A0）"), ("A1", "财务附件（A1）")),
        initial="A0",
        required=False,
    )

    def __init__(self, *args, actor=None, plan=None, require_current_instance=True, **kwargs):
        if actor is None or plan is None or not can_complete_maintenance(actor, plan):
            raise PermissionDenied("您没有完成此保养计划的权限。")
        self.actor, self.plan = actor, plan
        self.require_current_instance = require_current_instance
        super().__init__(*args, **kwargs)
        today = business_date()
        self.fields["completed_date"].widget.attrs["max"] = today.isoformat()
        if not self.is_bound:
            self.initial.setdefault("completed_date", today)
            self.initial.setdefault("result", "normal")
        self.fields["scheduled_date"].disabled = True
        self.initial.setdefault("scheduled_date", plan.next_maintenance_date)
        self.redo_date = self.initial["scheduled_date"] if not require_current_instance else None
        if not self.is_bound:
            # Capture once: both the disabled display and signature use this date.
            self.initial["completion_instance"] = completion_instance_token(
                actor=actor, plan=plan, scheduled_date=self.initial["scheduled_date"],
                require_current_instance=require_current_instance,
            )
        else:
            # Keep authentic old dates visible on all error responses. Never sign POST input.
            try:
                self.initial["scheduled_date"] = self._decode_instance(
                    self.data.get("completion_instance", "")
                )
            except ValidationError:
                pass
        if "finance" not in role_names_for(actor):
            self.fields["security_class"].choices = (("A0", "普通附件（A0）"),)
            self.fields["security_class"].widget = forms.HiddenInput()
            self.initial.setdefault("security_class", "A0")
        if not self.is_bound:
            self.initial.setdefault("idempotency_key", uuid.uuid4().hex)
        _style(self)

    def _decode_instance(self, token):
        return decode_completion_instance(
            token=token, actor=self.actor, plan=self.plan,
            require_current_instance=self.require_current_instance, redo_date=self.redo_date,
        )

    def clean_completion_instance(self):
        return self._decode_instance(self.cleaned_data["completion_instance"])

    def clean_security_class(self):
        return self.cleaned_data.get("security_class") or "A0"

    def clean(self):
        cleaned = super().clean()
        if "completion_instance" in self.errors:
            # The common template renders hidden values, but not hidden-field errors.
            self.add_error(None, self.errors["completion_instance"])
        elif cleaned.get("completion_instance") is not None:
            cleaned["scheduled_date"] = cleaned["completion_instance"]
        if (
            cleaned.get("result") == "problem_found"
            and not (cleaned.get("problem_description") or "").strip()
        ):
            self.add_error("problem_description", "发现问题时必须填写问题说明。")
        if cleaned.get("completed_date") and cleaned["completed_date"] > business_date():
            self.add_error("completed_date", "实际完成日期不得晚于当前上海业务日。")
        return cleaned


class MaintenanceRecordVoidForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    reason = forms.CharField(label="作废原因", max_length=2000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认作废该保养完成记录", required=True)

    def __init__(self, *args, actor=None, record=None, **kwargs):
        if actor is None or record is None or not can_void_maintenance_record(actor, record):
            raise PermissionDenied("只有 equipment 可以作废保养完成记录。")
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)


class MaintenanceProblemCloseForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    closure_note = forms.CharField(label="处理说明", max_length=2000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认关闭问题跟进", required=True)

    def __init__(self, *args, actor=None, problem=None, **kwargs):
        if actor is None or problem is None or not can_close_maintenance_problem(actor, problem):
            raise PermissionDenied("您没有关闭此问题跟进的权限。")
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)


class MaintenanceAttachmentUploadForm(forms.Form):
    uploaded_file = MultiFileField(label="保养证据")
    security_class = forms.ChoiceField(
        label="附件安全分类",
        choices=(("A0", "普通附件（A0）"), ("A1", "财务附件（A1）")),
        initial="A0",
        required=False,
    )

    def __init__(self, *args, actor=None, target=None, **kwargs):
        if (
            actor is None
            or target is None
            or not (
                can_manage_maintenance_attachment(actor, target, security_class="A0")
                or can_manage_maintenance_attachment(
                    actor, target, security_class="A1"
                )
            )
        ):
            raise PermissionDenied("您没有上传此保养证据的权限。")
        super().__init__(*args, **kwargs)
        if "finance" not in role_names_for(actor):
            self.fields["security_class"].choices = (("A0", "普通附件（A0）"),)
            self.fields["security_class"].widget = forms.HiddenInput()
            self.initial.setdefault("security_class", "A0")
        _style(self)

    def clean_security_class(self):
        return self.cleaned_data.get("security_class") or "A0"


class MaintenanceAttachmentVoidForm(forms.Form):
    reason = forms.CharField(label="作废原因", max_length=1000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认作废此保养证据", required=True)


class MaintenancePlanStatusForm(forms.Form):
    status = forms.ChoiceField(
        label="目标状态",
        choices=(("active", "启用"), ("suspended", "暂停"), ("ended", "终止")),
    )
    reason = forms.CharField(label="原因", required=False, max_length=2000, widget=forms.Textarea)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("status") == "ended" and not (cleaned.get("reason") or "").strip():
            self.add_error("reason", "终止计划必须填写原因。")
        return cleaned


__all__ = [
    "MaintenanceCompletionForm",
    "MaintenanceAttachmentUploadForm",
    "MaintenanceAttachmentVoidForm",
    "MaintenancePlanForm",
    "MaintenancePlanStatusForm",
    "MaintenanceProblemCloseForm",
    "MaintenanceRecordVoidForm",
]
