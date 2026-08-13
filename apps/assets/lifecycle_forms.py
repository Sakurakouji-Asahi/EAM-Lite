"""Chinese, action-specific forms for Sprint 7 lifecycle services."""

from __future__ import annotations

import uuid

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.assets.lifecycle_permissions import can_lifecycle_action
from apps.masterdata.models import AssetCodingScheme, Department, Employee, Location


def _style(form):
    for field in form.fields.values():
        if isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput)):
            continue
        field.widget.attrs.setdefault("class", "form-select" if isinstance(
            field.widget, forms.Select
        ) else "form-control")


class LifecycleActionForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    expected_status = forms.CharField(widget=forms.HiddenInput, max_length=32)

    def __init__(self, *args, actor=None, asset=None, action=None, **kwargs):
        if actor is None or asset is None or action is None:
            raise PermissionDenied("生命周期表单必须绑定操作用户、资产和动作。")
        if not can_lifecycle_action(actor, asset, action):
            raise PermissionDenied("您没有执行此生命周期动作的权限。")
        self.actor, self.asset, self.action = actor, asset, action
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial.setdefault("idempotency_key", uuid.uuid4().hex)
            self.initial.setdefault("expected_status", self.asset.asset_status)
        _style(self)

    def clean_idempotency_key(self):
        return (self.cleaned_data.get("idempotency_key") or uuid.uuid4().hex).strip()


class ReasonedLifecycleForm(LifecycleActionForm):
    effective_at = forms.DateTimeField(
        label="生效时间", widget=forms.DateTimeInput(attrs={"type": "datetime-local"})
    )
    reason = forms.CharField(label="原因", max_length=1000, widget=forms.Textarea)
    remark = forms.CharField(
        label="备注", required=False, max_length=2000, widget=forms.Textarea
    )

    def clean_effective_at(self):
        value = self.cleaned_data["effective_at"]
        if timezone.is_naive(value) or value > timezone.now():
            raise ValidationError("生效时间必须包含时区且不得晚于当前时间。")
        return value


class AssetTransferForm(ReasonedLifecycleForm):
    expected_department_id = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    expected_responsible_employee_id = forms.IntegerField(
        widget=forms.HiddenInput, min_value=1
    )
    expected_location_id = forms.IntegerField(widget=forms.HiddenInput, min_value=1)
    to_department = forms.ModelChoiceField(label="目标部门", queryset=Department.objects.none())
    to_responsible_employee = forms.ModelChoiceField(
        label="新责任人", queryset=Employee.objects.none()
    )
    to_location = forms.ModelChoiceField(label="新位置", queryset=Location.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = self.asset.company
        if not self.is_bound:
            self.initial.setdefault("expected_department_id", self.asset.department_id)
            self.initial.setdefault(
                "expected_responsible_employee_id",
                self.asset.responsible_employee_id,
            )
            self.initial.setdefault("expected_location_id", self.asset.location_id)
            self.initial.setdefault("expected_status", self.asset.asset_status)
        self.fields["to_department"].queryset = Department.objects.filter(
            company=company, is_active=True
        )
        self.fields["to_responsible_employee"].queryset = Employee.objects.filter(
            company=company, employment_status="active", is_active=True,
            department__is_active=True,
        )
        self.fields["to_location"].queryset = Location.objects.filter(
            company=company, is_active=True, children__isnull=True
        ).distinct()

    def clean(self):
        cleaned = super().clean()
        employee = cleaned.get("to_responsible_employee")
        department = cleaned.get("to_department")
        if employee and department and employee.department_id != department.pk:
            self.add_error("to_responsible_employee", "新责任人必须属于目标部门。")
        return cleaned


class AssetStatusForm(ReasonedLifecycleForm):
    pass


class AssetLoanForm(LifecycleActionForm):
    borrower_type = forms.ChoiceField(
        label="借用方类型",
        choices=(("internal_employee", "内部员工"), ("external", "外部借用方")),
    )
    borrower_employee = forms.ModelChoiceField(
        label="内部借用员工", queryset=Employee.objects.none(), required=False
    )
    borrower_name = forms.CharField(label="外部借用人", max_length=200, required=False)
    borrower_organization = forms.CharField(label="外部单位", max_length=200, required=False)
    loan_date = forms.DateField(label="借出日期", widget=forms.DateInput(attrs={"type": "date"}))
    expected_return_date = forms.DateField(
        label="预计归还日", widget=forms.DateInput(attrs={"type": "date"})
    )
    reason = forms.CharField(label="借出原因", max_length=1000, widget=forms.Textarea)
    remark = forms.CharField(label="备注", required=False, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["borrower_employee"].queryset = Employee.objects.filter(
            company=self.asset.company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        )

    def clean(self):
        cleaned = super().clean()
        internal = cleaned.get("borrower_type") == "internal_employee"
        employee = cleaned.get("borrower_employee")
        external_name = (cleaned.get("borrower_name") or "").strip()
        organization = (cleaned.get("borrower_organization") or "").strip()
        if internal and (employee is None or external_name or organization):
            raise ValidationError("内部借用必须选择员工，且不能填写外部借用方字段。")
        if not internal and (employee is not None or not external_name):
            raise ValidationError("外部借用必须填写借用人，且不能选择内部员工。")
        if cleaned.get("loan_date") and cleaned.get("expected_return_date"):
            if cleaned["expected_return_date"] < cleaned["loan_date"]:
                self.add_error("expected_return_date", "预计归还日不得早于借出日。")
        return cleaned


class AssetLoanReturnForm(LifecycleActionForm):
    returned_at = forms.DateField(label="实际归还日", widget=forms.DateInput(attrs={"type": "date"}))
    received_by_employee = forms.ModelChoiceField(label="接收人", queryset=Employee.objects.none())
    return_department = forms.ModelChoiceField(label="接收部门", queryset=Department.objects.none())
    return_responsible_employee = forms.ModelChoiceField(label="责任人", queryset=Employee.objects.none())
    return_location = forms.ModelChoiceField(label="归还位置", queryset=Location.objects.none())
    return_asset_status = forms.ChoiceField(
        label="归还后状态", choices=(("in_use", "在用"), ("idle", "闲置"))
    )
    remark = forms.CharField(label="归还备注", required=False, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        company = self.asset.company
        employees = Employee.objects.filter(
            company=company, employment_status="active", is_active=True,
            department__is_active=True,
        )
        self.fields["received_by_employee"].queryset = employees
        self.fields["return_responsible_employee"].queryset = employees
        self.fields["return_department"].queryset = Department.objects.filter(company=company, is_active=True)
        self.fields["return_location"].queryset = Location.objects.filter(
            company=company, is_active=True, children__isnull=True
        ).distinct()

    def clean(self):
        cleaned = super().clean()
        employee = cleaned.get("return_responsible_employee")
        department = cleaned.get("return_department")
        if employee and department and employee.department_id != department.pk:
            self.add_error("return_responsible_employee", "归还责任人必须属于接收部门。")
        return cleaned


class DisposalInitiateForm(LifecycleActionForm):
    disposal_type = forms.ChoiceField(
        label="处置类型", choices=(("scrap", "报废"), ("sale", "出售"), ("other", "其他处置"))
    )
    application_date = forms.DateField(label="申请日期", widget=forms.DateInput(attrs={"type": "date"}))
    planned_disposal_date = forms.DateField(label="拟处置日期", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="原因", max_length=1000, widget=forms.Textarea)
    description = forms.CharField(label="说明", required=False, widget=forms.Textarea)
    recipient_name = forms.CharField(label="接收方/去向", required=False, max_length=200)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("application_date") and cleaned.get("planned_disposal_date"):
            if cleaned["planned_disposal_date"] < cleaned["application_date"]:
                self.add_error("planned_disposal_date", "拟处置日期不得早于申请日期。")
        return cleaned


class DisposalActualDetailsForm(LifecycleActionForm):
    actual_disposal_date = forms.DateField(label="实际完成日期", widget=forms.DateInput(attrs={"type": "date"}))
    recipient_name = forms.CharField(label="接收方/去向", required=False, max_length=200)


class DisposalFinanceLockForm(LifecycleActionForm):
    disposal_income = forms.DecimalField(label="处置收入", max_digits=18, decimal_places=2, min_value=0)


class DisposalCompleteForm(LifecycleActionForm):
    confirm = forms.BooleanField(label="确认完成处置并进入终态", required=True)


class DisposalAttachmentUploadForm(LifecycleActionForm):
    uploaded_file = forms.FileField(label="处置证据")
    security_class = forms.ChoiceField(
        label="安全分类",
        choices=(("A0", "A0 普通附件"), ("A1", "A1 财务附件")),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from apps.masterdata.permissions import role_names_for

        if "finance" not in role_names_for(self.actor):
            self.fields["security_class"].choices = (("A0", "A0 普通附件"),)
            self.fields["security_class"].initial = "A0"


class DisposalAttachmentVoidForm(LifecycleActionForm):
    reason = forms.CharField(label="作废原因", max_length=1000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认作废此处置附件", required=True)


class ReasonForm(LifecycleActionForm):
    reason = forms.CharField(label="原因", max_length=1000, widget=forms.Textarea)


class ArchiveAssetForm(ReasonForm):
    confirm = forms.BooleanField(label="确认执行", required=True)


class CorrectAssetCodeForm(LifecycleActionForm):
    effective_date = forms.DateField(label="编号生效日期", widget=forms.DateInput(attrs={"type": "date"}))
    coding_scheme = forms.ModelChoiceField(
        label="编码方案",
        queryset=AssetCodingScheme.objects.none(),
        required=False,
        help_text="留空时沿用当前正式编号的方案版本。",
    )
    reason = forms.CharField(label="更正原因", max_length=1000, widget=forms.Textarea)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["coding_scheme"].queryset = AssetCodingScheme.objects.filter(
            company=self.asset.company,
            status="active",
        )
        if not self.is_bound:
            self.initial.setdefault("effective_date", timezone.localdate())


class DisposalReversalForm(ReasonForm):
    replacement_responsible_employee = forms.ModelChoiceField(
        label="替代责任人",
        queryset=Employee.objects.none(),
        required=False,
        help_text="原责任人已不在职或停用时必填。",
    )
    confirm = forms.BooleanField(label="确认执行终态处置冲销", required=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["replacement_responsible_employee"].queryset = Employee.objects.filter(
            company=self.asset.company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        )


__all__ = [
    "ArchiveAssetForm", "AssetLoanForm", "AssetLoanReturnForm",
    "AssetStatusForm", "AssetTransferForm", "CorrectAssetCodeForm",
    "DisposalActualDetailsForm", "DisposalAttachmentUploadForm",
    "DisposalAttachmentVoidForm", "DisposalCompleteForm",
    "DisposalFinanceLockForm", "DisposalInitiateForm",
    "DisposalReversalForm", "LifecycleActionForm", "ReasonForm",
]
