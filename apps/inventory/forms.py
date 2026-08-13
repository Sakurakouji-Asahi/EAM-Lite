"""Chinese forms for Sprint 8 inventory actions.

Derived result/cache fields and all finance fields are deliberately absent.
Services always repeat authorization and state validation after taking locks.
"""

from __future__ import annotations

import uuid

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.inventory.permissions import (
    can_close_inventory_task,
    can_manage_inventory_attachment,
    can_reconcile_inventory_task,
    can_scan_inventory_task,
)
from apps.masterdata.models import AssetCategory, Department, Employee, Location


def _style(form):
    for field in form.fields.values():
        if isinstance(field.widget, (forms.HiddenInput, forms.CheckboxInput)):
            continue
        field.widget.attrs.setdefault(
            "class",
            "form-select" if isinstance(field.widget, forms.Select) else "form-control",
        )


class InventoryTaskForm(forms.Form):
    name = forms.CharField(label="任务名称", max_length=200)
    inventory_type = forms.ChoiceField(
        label="盘点类型",
        choices=(
            ("department", "部门盘点"),
            ("full", "财务全盘"),
            ("special", "专项盘点"),
        ),
    )
    scope_type = forms.ChoiceField(
        label="盘点范围",
        choices=(
            ("company", "全公司"),
            ("department", "部门"),
            ("category", "实物分类"),
            ("location", "位置"),
            ("selected_assets", "勾选资产"),
        ),
    )
    scope_department = forms.ModelChoiceField(
        label="范围部门", required=False, queryset=Department.objects.none()
    )
    scope_category = forms.ModelChoiceField(
        label="范围分类", required=False, queryset=AssetCategory.objects.none()
    )
    scope_location = forms.ModelChoiceField(
        label="范围位置", required=False, queryset=Location.objects.none()
    )
    selected_asset_ids = forms.CharField(
        label="已选资产", required=False, widget=forms.HiddenInput
    )
    planned_start = forms.DateField(
        label="计划开始", widget=forms.DateInput(attrs={"type": "date"})
    )
    planned_end = forms.DateField(
        label="计划结束", widget=forms.DateInput(attrs={"type": "date"})
    )
    assignees = forms.ModelMultipleChoiceField(
        label="执行人", required=False, queryset=get_user_model().objects.none()
    )
    remark = forms.CharField(
        label="备注", required=False, max_length=2000, widget=forms.Textarea
    )

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None or company is None:
            raise PermissionDenied("盘点任务表单必须绑定用户和公司。")
        self.actor, self.company = actor, company
        super().__init__(*args, **kwargs)
        self.fields["scope_department"].queryset = Department.objects.filter(
            company=company, is_active=True
        )
        self.fields["scope_category"].queryset = AssetCategory.objects.filter(
            company=company, is_active=True
        )
        self.fields["scope_location"].queryset = Location.objects.filter(
            company=company, is_active=True
        )
        self.fields["assignees"].queryset = get_user_model().objects.filter(
            is_active=True, is_superuser=False
        )
        _style(self)

    def clean_selected_asset_ids(self):
        raw = (self.cleaned_data.get("selected_asset_ids") or "").strip()
        if not raw:
            return []
        values = []
        for token in raw.split(","):
            try:
                values.append(uuid.UUID(token.strip()))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ValidationError("已选资产包含无效 ID。") from exc
        if len(values) != len(set(values)):
            raise ValidationError("已选资产不能重复。")
        return values

    def clean(self):
        cleaned = super().clean()
        start, end = cleaned.get("planned_start"), cleaned.get("planned_end")
        if start and end and end < start:
            self.add_error("planned_end", "计划结束日不得早于开始日。")
        scope = cleaned.get("scope_type")
        matrix = {
            "department": "scope_department",
            "category": "scope_category",
            "location": "scope_location",
            "selected_assets": "selected_asset_ids",
        }
        required_field = matrix.get(scope)
        if required_field and not cleaned.get(required_field):
            self.add_error(required_field, "此范围必须选择具体对象。")
        if scope == "company" and any(
            cleaned.get(name)
            for name in (
                "scope_department",
                "scope_category",
                "scope_location",
                "selected_asset_ids",
            )
        ):
            raise ValidationError("全公司范围不得同时提交其他范围字段。")
        if cleaned.get("inventory_type") == "department" and scope != "department":
            self.add_error("scope_type", "部门盘点只允许指定一个部门范围。")
        if cleaned.get("inventory_type") == "full" and scope != "company":
            self.add_error("scope_type", "财务全盘必须使用全公司范围。")
        return cleaned


class InventoryScanForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    actual_location = forms.ModelChoiceField(
        label="实际位置", required=False, queryset=Location.objects.none()
    )
    actual_employee = forms.ModelChoiceField(
        label="实际责任人", required=False, queryset=Employee.objects.none()
    )
    actual_status = forms.ChoiceField(label="实际状态", choices=())
    other_mismatch = forms.BooleanField(label="其他异常", required=False)
    note = forms.CharField(label="异常说明", required=False, widget=forms.Textarea)

    def __init__(self, *args, actor=None, task=None, supplemental=False, **kwargs):
        if actor is None or task is None:
            raise PermissionDenied("扫码表单必须绑定用户和盘点任务。")
        if supplemental:
            if not can_reconcile_inventory_task(actor, task):
                raise PermissionDenied("您没有执行受控补盘的权限。")
        elif not can_scan_inventory_task(actor, task):
            raise PermissionDenied("您没有执行此盘点任务的权限。")
        self.actor, self.task, self.supplemental = actor, task, supplemental
        super().__init__(*args, **kwargs)
        from apps.assets.models import Asset

        self.fields["actual_location"].queryset = Location.objects.filter(
            company=task.company, is_active=True, children__isnull=True
        ).distinct()
        self.fields["actual_employee"].queryset = Employee.objects.filter(
            company=task.company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        )
        self.fields["actual_status"].choices = Asset.AssetStatus.choices
        if not self.is_bound:
            self.initial.setdefault("idempotency_key", uuid.uuid4().hex)
        _style(self)

    def clean_idempotency_key(self):
        return (self.cleaned_data.get("idempotency_key") or uuid.uuid4().hex).strip()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("other_mismatch") and not (cleaned.get("note") or "").strip():
            self.add_error("note", "选择其他异常时必须填写说明。")
        return cleaned


class SupplementalInventoryScanForm(InventoryScanForm):
    supplement_reason = forms.CharField(
        label="补盘原因", max_length=1000, widget=forms.Textarea
    )

    def __init__(self, *args, **kwargs):
        kwargs["supplemental"] = True
        super().__init__(*args, **kwargs)


class InventoryStopForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    reason = forms.CharField(label="停止扫码原因", max_length=1000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认停止扫码并进入差异处理", required=True)

    def __init__(self, *args, actor=None, task=None, **kwargs):
        if actor is None or task is None or not can_reconcile_inventory_task(actor, task):
            raise PermissionDenied("您没有停止此任务扫码的权限。")
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)


class InventoryResolutionForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    resolution_type = forms.ChoiceField(
        label="处理结论",
        choices=(
            ("master_updated", "已执行主档变动"),
            ("master_confirmed", "确认主档无误"),
            ("loss_confirmed", "确认盘亏"),
            ("other", "其他处理"),
        ),
    )
    conclusion = forms.CharField(label="结论说明", max_length=2000, widget=forms.Textarea)
    to_department = forms.ModelChoiceField(
        label="目标部门", required=False, queryset=Department.objects.none()
    )
    to_responsible_employee = forms.ModelChoiceField(
        label="新责任人", required=False, queryset=Employee.objects.none()
    )
    to_location = forms.ModelChoiceField(
        label="新位置", required=False, queryset=Location.objects.none()
    )
    to_status = forms.ChoiceField(
        label="新状态",
        required=False,
        choices=(("in_use", "在用"), ("idle", "闲置")),
    )
    effective_at = forms.DateTimeField(
        label="变动生效时间",
        required=False,
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
    )

    def __init__(self, *args, actor=None, task=None, **kwargs):
        if actor is None or task is None or not can_reconcile_inventory_task(actor, task):
            raise PermissionDenied("您没有处理此任务差异的权限。")
        super().__init__(*args, **kwargs)
        company = task.company
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
        ).distinct()
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("resolution_type") == "master_updated":
            if not any(
                cleaned.get(field)
                for field in (
                    "to_department",
                    "to_responsible_employee",
                    "to_location",
                    "to_status",
                )
            ):
                raise ValidationError("执行主档变动时必须提交至少一项目标值。")
            if cleaned.get("effective_at") is None:
                self.add_error("effective_at", "执行主档变动时必须填写生效时间。")
        return cleaned


class InventorySurplusForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    temporary_name = forms.CharField(label="临时名称", max_length=200)
    temporary_category_text = forms.CharField(label="实物分类建议", max_length=200)
    temporary_location_text = forms.CharField(label="发现位置", max_length=500)
    remark = forms.CharField(label="说明", required=False, max_length=2000, widget=forms.Textarea)

    def __init__(self, *args, actor=None, task=None, **kwargs):
        if actor is None or task is None or not can_scan_inventory_task(actor, task):
            raise PermissionDenied("您没有在此盘点任务登记盘盈的权限。")
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)


class InventorySurplusResolutionForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    resolution_status = forms.ChoiceField(
        label="盘盈处理",
        choices=(
            ("not_company", "非公司资产"),
            ("duplicate", "重复记录"),
            ("other", "其他"),
        ),
    )
    remark = forms.CharField(label="处理说明", max_length=2000, widget=forms.Textarea)


class InventoryTaskCloseForm(forms.Form):
    idempotency_key = forms.CharField(widget=forms.HiddenInput, required=False)
    confirm = forms.BooleanField(label="确认关闭盘点任务", required=True)

    def __init__(self, *args, actor=None, task=None, **kwargs):
        if actor is None or task is None or not can_close_inventory_task(actor, task):
            raise PermissionDenied("您没有关闭此盘点任务的权限。")
        super().__init__(*args, **kwargs)
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)


class InventoryTaskCancelForm(InventoryTaskCloseForm):
    reason = forms.CharField(label="取消原因", max_length=1000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认取消并保留全部证据", required=True)


class InventoryResolutionCorrectionForm(InventoryResolutionForm):
    correction_reason = forms.CharField(label="更正原因", max_length=1000, widget=forms.Textarea)

    def __init__(self, *args, actor=None, task=None, **kwargs):
        if actor is None or task is None or not can_close_inventory_task(actor, task):
            raise PermissionDenied("您没有新增关闭后更正结论的权限。")
        # Bypass InventoryResolutionForm's reconciliation-only constructor.
        forms.Form.__init__(self, *args, **kwargs)
        company = task.company
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
        ).distinct()
        if not self.is_bound:
            self.initial["idempotency_key"] = uuid.uuid4().hex
        _style(self)


class InventoryAttachmentUploadForm(forms.Form):
    uploaded_file = forms.FileField(label="盘点证据")

    def __init__(self, *args, actor=None, target=None, **kwargs):
        if actor is None or target is None or not can_manage_inventory_attachment(actor, target):
            raise PermissionDenied("您没有上传此盘点证据的权限。")
        super().__init__(*args, **kwargs)
        _style(self)


class InventoryAttachmentVoidForm(forms.Form):
    reason = forms.CharField(label="作废原因", max_length=1000, widget=forms.Textarea)
    confirm = forms.BooleanField(label="确认作废此盘点证据", required=True)


__all__ = [
    "InventoryAttachmentUploadForm",
    "InventoryAttachmentVoidForm",
    "InventoryResolutionCorrectionForm",
    "InventoryResolutionForm",
    "InventoryScanForm",
    "InventoryStopForm",
    "InventorySurplusForm",
    "InventorySurplusResolutionForm",
    "InventoryTaskCancelForm",
    "InventoryTaskCloseForm",
    "InventoryTaskForm",
    "SupplementalInventoryScanForm",
]
