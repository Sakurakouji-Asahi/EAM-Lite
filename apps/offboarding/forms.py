"""Bound Chinese forms for employee asset-clearance actions."""

from __future__ import annotations

import uuid

from django import forms
from django.core.exceptions import PermissionDenied

from apps.assets.lifecycle_permissions import can_lifecycle_action
from apps.masterdata.models import Department, Employee, Location
from apps.masterdata.permissions import role_names_for
from apps.offboarding.domain import business_date
from apps.offboarding.permissions import (
    can_complete_clearance,
    can_create_supplemental_clearance,
    can_initiate_clearance,
    can_manage_clearance_attachment,
    can_refresh_clearance,
)


def _style(form):
    for field in form.fields.values():
        if isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput)):
            continue
        field.widget.attrs.setdefault(
            "class",
            "form-select" if isinstance(field.widget, forms.Select) else "form-control",
        )


class ClearanceInitiateForm(forms.Form):
    employee = forms.ModelChoiceField(label="员工", queryset=Employee.objects.none())
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    remark = forms.CharField(label="备注", required=False, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认将员工置为离职处理中并建立清退单", required=True)

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None or company is None or not can_initiate_clearance(actor):
            raise PermissionDenied("只有 hr 可以发起员工离职资产清退。")
        self.actor, self.company = actor, company
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = Employee.objects.filter(
            company=company,
            employment_status="active",
        ).select_related("department").order_by("normalized_employee_no")
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)


class ClearanceRefreshForm(forms.Form):
    reason = forms.CharField(label="重新核对原因", max_length=2000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认刷新并核对当前关联", required=True)

    def __init__(self, *args, actor=None, clearance=None, **kwargs):
        if actor is None or clearance is None or not can_refresh_clearance(actor, clearance):
            raise PermissionDenied("只有 hr 可以刷新或核对清退单。")
        super().__init__(*args, **kwargs)
        _style(self)


class SupplementalClearanceForm(forms.Form):
    reason = forms.CharField(label="补充清退原因", max_length=2000, widget=forms.Textarea)
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    confirm = forms.BooleanField(label="确认建立独立补充清退单", required=True)

    def __init__(self, *args, actor=None, original_clearance=None, **kwargs):
        if (
            actor is None
            or original_clearance is None
            or not can_create_supplemental_clearance(actor, original_clearance)
        ):
            raise PermissionDenied("只有 hr 可以建立补充清退单。")
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)


class ClearanceCompleteForm(forms.Form):
    termination_date = forms.DateField(
        label="实际离职日期",
        required=False,
        widget=forms.DateInput(attrs={"type": "date"}),
    )
    confirm = forms.BooleanField(label="确认完成清退", required=True)

    def __init__(self, *args, actor=None, clearance=None, **kwargs):
        if actor is None or clearance is None or not can_complete_clearance(actor, clearance):
            raise PermissionDenied("只有 hr 可以完成员工离职资产清退。")
        self.clearance = clearance
        super().__init__(*args, **kwargs)
        if clearance.supplements_clearance_id:
            self.fields["termination_date"].widget = forms.HiddenInput()
            self.fields["termination_date"].disabled = True
        _style(self)

    def clean_termination_date(self):
        value = self.cleaned_data.get("termination_date")
        if self.clearance.supplements_clearance_id:
            return None
        if value is None:
            raise forms.ValidationError("首次清退必须填写实际离职日期。")
        if self.clearance.employee.hire_date and value < self.clearance.employee.hire_date:
            raise forms.ValidationError("实际离职日期不得早于入职日期。")
        if value > business_date():
            raise forms.ValidationError("实际离职日期不得晚于当前上海业务日。")
        return value


class ClearanceItemReturnForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    returned_at = forms.DateTimeField(
        label="实际归还时间",
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    received_by_employee = forms.ModelChoiceField(label="接收人", queryset=Employee.objects.none())
    return_department = forms.ModelChoiceField(label="接收部门", queryset=Department.objects.none())
    return_responsible_employee = forms.ModelChoiceField(label="归还后责任人", queryset=Employee.objects.none())
    return_location = forms.ModelChoiceField(label="归还位置", queryset=Location.objects.none())
    return_asset_status = forms.ChoiceField(
        label="归还后状态", choices=(("in_use", "在用"), ("idle", "闲置"))
    )
    remark = forms.CharField(label="备注", required=False, widget=forms.Textarea)

    def __init__(self, *args, actor=None, item=None, **kwargs):
        has_active_loan = bool(
            item is not None
            and item.asset.loans.filter(status="active").exists()
        )
        action = "loan_return" if has_active_loan else "assignment_return"
        if (
            actor is None
            or item is None
            or item.clearance.status not in {"open", "blocked"}
            or item.resolution not in {"pending", "disposal_in_progress"}
            or not can_lifecycle_action(
                actor,
                item.asset,
                action,
                target_department=item.asset.department,
            )
        ):
            raise PermissionDenied("您没有处理此清退归还项目的权限。")
        self.actor, self.item = actor, item
        super().__init__(*args, **kwargs)
        company = item.company
        employees = Employee.objects.filter(
            company=company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        )
        self.fields["received_by_employee"].queryset = employees
        self.fields["return_responsible_employee"].queryset = employees
        self.fields["return_department"].queryset = Department.objects.filter(
            company=company, is_active=True
        )
        self.fields["return_location"].queryset = Location.objects.filter(
            company=company, is_active=True, children__isnull=True
        )
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)

    def clean(self):
        cleaned = super().clean()
        department = cleaned.get("return_department")
        responsible = cleaned.get("return_responsible_employee")
        if department is not None and responsible is not None:
            if responsible.department_id != department.pk:
                self.add_error(
                    "return_responsible_employee", "归还后责任人必须属于接收部门。"
                )
            action = (
                "loan_return"
                if self.item.asset.loans.filter(status="active").exists()
                else "assignment_return"
            )
            if not can_lifecycle_action(
                self.actor,
                self.item.asset,
                action,
                target_department=department,
            ):
                raise PermissionDenied("您没有向所选部门执行此归还动作的权限。")
        return cleaned


class ClearanceItemTransferForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    to_department = forms.ModelChoiceField(label="目标部门", queryset=Department.objects.none())
    to_responsible_employee = forms.ModelChoiceField(label="新责任人", queryset=Employee.objects.none())
    to_location = forms.ModelChoiceField(label="新位置", queryset=Location.objects.none())
    effective_at = forms.DateTimeField(
        label="生效时间",
        input_formats=("%Y-%m-%dT%H:%M",),
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )
    reason = forms.CharField(label="原因", max_length=2000, widget=forms.Textarea)
    remark = forms.CharField(label="备注", required=False, widget=forms.Textarea)

    def __init__(self, *args, actor=None, item=None, **kwargs):
        if (
            actor is None
            or item is None
            or item.clearance.status not in {"open", "blocked"}
            or item.resolution not in {"pending", "disposal_in_progress"}
            or not can_lifecycle_action(
                actor,
                item.asset,
                "transfer",
                target_department=item.asset.department,
            )
        ):
            raise PermissionDenied("您没有处理此清退转交项目的权限。")
        self.actor, self.item = actor, item
        super().__init__(*args, **kwargs)
        company = item.company
        self.fields["to_department"].queryset = Department.objects.filter(
            company=company, is_active=True
        )
        self.fields["to_responsible_employee"].queryset = Employee.objects.filter(
            company=company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        )
        self.fields["to_location"].queryset = Location.objects.filter(
            company=company, is_active=True, children__isnull=True
        )
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)

    def clean(self):
        cleaned = super().clean()
        department = cleaned.get("to_department")
        responsible = cleaned.get("to_responsible_employee")
        if department is not None and responsible is not None:
            if responsible.department_id != department.pk:
                self.add_error("to_responsible_employee", "新责任人必须属于目标部门。")
            if not can_lifecycle_action(
                self.actor,
                self.item.asset,
                "transfer",
                target_department=department,
            ):
                raise PermissionDenied("您没有向所选部门执行此转交动作的权限。")
        return cleaned


class ClearanceAttachmentUploadForm(forms.Form):
    uploaded_file = forms.FileField(label="清退证据")
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
                can_manage_clearance_attachment(actor, target, security_class="A0")
                or can_manage_clearance_attachment(actor, target, security_class="A1")
            )
        ):
            raise PermissionDenied("您没有上传此清退证据的权限。")
        super().__init__(*args, **kwargs)
        if "finance" not in role_names_for(actor):
            self.fields["security_class"].choices = (("A0", "普通附件（A0）"),)
            self.fields["security_class"].widget = forms.HiddenInput()
            self.initial.setdefault("security_class", "A0")
        _style(self)

    def clean_security_class(self):
        return self.cleaned_data.get("security_class") or "A0"


class ClearanceAttachmentVoidForm(forms.Form):
    reason = forms.CharField(label="作废原因", max_length=1000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认作废此清退证据", required=True)


__all__ = [
    "ClearanceAttachmentUploadForm",
    "ClearanceAttachmentVoidForm",
    "ClearanceCompleteForm",
    "ClearanceInitiateForm",
    "ClearanceItemReturnForm",
    "ClearanceItemTransferForm",
    "ClearanceRefreshForm",
    "SupplementalClearanceForm",
]
