"""Bound Chinese forms for preventive-maintenance actions."""

from __future__ import annotations

import uuid

from django import forms
from django.core.exceptions import PermissionDenied

from apps.assets.models import Asset
from apps.maintenance.domain import business_date
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
    for field in form.fields.values():
        if isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput)):
            continue
        field.widget.attrs.setdefault(
            "class",
            "form-select" if isinstance(field.widget, forms.Select) else "form-control",
        )


class MaintenancePlanForm(forms.Form):
    asset = forms.ModelChoiceField(label="资产", queryset=Asset.objects.none())
    name = forms.CharField(label="计划名称", max_length=200)
    cycle_value = forms.IntegerField(label="周期数值", min_value=1)
    cycle_unit = forms.ChoiceField(
        label="周期单位",
        choices=(("day", "日"), ("week", "周"), ("month", "月"), ("year", "年")),
    )
    responsible_employee = forms.ModelChoiceField(
        label="责任人", queryset=Employee.objects.none()
    )
    advance_notice_days = forms.IntegerField(label="提前提醒天数", min_value=0)
    standard_content = forms.CharField(label="标准内容", widget=forms.Textarea)
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
        if instance is not None and not self.is_bound:
            self.initial.update(
                {
                    "asset": instance.asset_id,
                    "name": instance.name,
                    "cycle_value": instance.cycle_value,
                    "cycle_unit": instance.cycle_unit,
                    "responsible_employee": instance.responsible_employee_id,
                    "advance_notice_days": instance.advance_notice_days,
                    "standard_content": instance.standard_content,
                    "first_due_date": instance.first_due_date,
                }
            )
            self.fields["asset"].disabled = True
        _style(self)


class MaintenanceCompletionForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    scheduled_date = forms.DateField(
        label="计划日期", widget=forms.DateInput(attrs={"type": "date"})
    )
    completed_date = forms.DateField(
        label="实际完成日期", widget=forms.DateInput(attrs={"type": "date"})
    )
    actual_content = forms.CharField(label="实际内容", widget=forms.Textarea)
    result = forms.ChoiceField(
        label="结果", choices=(("normal", "正常"), ("problem_found", "发现问题"))
    )
    problem_description = forms.CharField(
        label="问题说明", required=False, widget=forms.Textarea
    )
    remark = forms.CharField(label="备注", required=False, widget=forms.Textarea)
    uploaded_file = forms.FileField(label="照片/附件", required=False)
    security_class = forms.ChoiceField(
        label="附件安全分类",
        choices=(("A0", "普通附件（A0）"), ("A1", "财务附件（A1）")),
        initial="A0",
        required=False,
    )

    def __init__(self, *args, actor=None, plan=None, **kwargs):
        if actor is None or plan is None or not can_complete_maintenance(actor, plan):
            raise PermissionDenied("您没有完成此保养计划的权限。")
        self.actor, self.plan = actor, plan
        super().__init__(*args, **kwargs)
        self.fields["scheduled_date"].disabled = True
        self.initial.setdefault("scheduled_date", plan.next_maintenance_date)
        if "finance" not in role_names_for(actor):
            self.fields["security_class"].choices = (("A0", "普通附件（A0）"),)
            self.fields["security_class"].widget = forms.HiddenInput()
            self.initial.setdefault("security_class", "A0")
        if not self.is_bound:
            self.initial.setdefault("idempotency_key", uuid.uuid4().hex)
        _style(self)

    def clean_security_class(self):
        return self.cleaned_data.get("security_class") or "A0"

    def clean(self):
        cleaned = super().clean()
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
    uploaded_file = forms.FileField(label="保养证据")
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
