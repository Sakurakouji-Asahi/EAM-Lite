"""Chinese, permission-bound forms for the Sprint 3 asset master."""

from django import forms
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone

from apps.assets.models import Asset, AssetCustomField, AttachmentLink
from apps.assets.permissions import (
    ASSET_GLOBAL_WRITE_ROLES,
    can_create_attachment_link,
    can_delete_asset_draft,
    can_set_requested_coding_scheme,
    can_submit_asset,
    can_void_attachment_link,
    can_withdraw_asset,
    require_edit_asset_draft,
)
from apps.assets.services import FINANCIAL_FIELD_NAMES
from apps.masterdata.models import (
    AssetCategory,
    AssetCodingScheme,
    Department,
    Employee,
    Location,
)
from apps.masterdata.permissions import resolve_department_ids, role_names_for


def _bootstrap_widgets(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


class AssetDraftForm(forms.ModelForm):
    quantity = forms.IntegerField(
        label="数量",
        initial=1,
        min_value=1,
        max_value=1,
        widget=forms.NumberInput(attrs={"readonly": "readonly"}),
        help_text="V1 每条记录代表一件实物；相同多件请分别建档。",
    )

    class Meta:
        model = Asset
        fields = (
            "asset_name",
            "category",
            "brand",
            "model",
            "manufacturer",
            "serial_number",
            "factory_number",
            "historical_code",
            "unit",
            "description",
            "department",
            "responsible_employee",
            "location",
            "acquisition_date",
            "commissioning_date",
            "is_maintenance_required",
            "notes",
        )
        labels = {
            "asset_name": "资产名称",
            "category": "实物分类",
            "brand": "品牌",
            "model": "型号",
            "manufacturer": "厂家",
            "serial_number": "序列号",
            "factory_number": "出厂编号",
            "historical_code": "历史参考编号",
            "unit": "单位",
            "description": "说明",
            "department": "当前部门",
            "responsible_employee": "责任人",
            "location": "当前位置",
            "acquisition_date": "购置日期",
            "commissioning_date": "达到可使用状态日期",
            "is_maintenance_required": "需要保养",
            "notes": "备注",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "notes": forms.Textarea(attrs={"rows": 3}),
            "acquisition_date": forms.DateInput(attrs={"type": "date"}),
            "commissioning_date": forms.DateInput(attrs={"type": "date"}),
        }

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None or company is None:
            raise PermissionDenied("资产表单必须绑定当前操作用户和公司。")
        self.actor = actor
        self.company = company
        super().__init__(*args, **kwargs)
        # ModelForm runs ``Asset.clean()`` before the Service executes; bind
        # the immutable company boundary now so cross-company validation is
        # accurate for a new unsaved draft.
        if self.instance._state.adding:
            self.instance.company = company
        roles = role_names_for(actor)
        if not self.instance._state.adding:
            require_edit_asset_draft(actor, self.instance)
        elif not roles.intersection(
            ASSET_GLOBAL_WRITE_ROLES | {"department_manager"}
        ):
            raise PermissionDenied("您没有新建资产草稿的权限。")

        department_ids = None
        if not roles.intersection(ASSET_GLOBAL_WRITE_ROLES):
            department_ids = resolve_department_ids(actor, company)
        departments = Department.objects.filter(company=company, is_active=True)
        if department_ids is not None:
            departments = departments.filter(pk__in=department_ids)
        self.fields["department"].queryset = departments.order_by("normalized_code")
        self.fields["responsible_employee"].queryset = Employee.objects.filter(
            company=company,
            department__in=departments,
            department__is_active=True,
            employment_status=Employee.EmploymentStatus.ACTIVE,
            is_active=True,
        ).order_by("normalized_employee_no")
        self.fields["category"].queryset = AssetCategory.objects.filter(
            company=company, is_active=True
        ).order_by("category_level", "normalized_code")
        self.fields["location"].queryset = (
            Location.objects.filter(company=company, is_active=True)
            .filter(children__isnull=True)
            .order_by("level", "normalized_code")
        )
        self.fields["department"].required = False
        self.fields["responsible_employee"].required = False
        self.fields["location"].required = False
        self.fields["category"].required = True
        self.fields["asset_name"].required = True
        _bootstrap_widgets(self)

        if self.is_bound:
            forbidden = set(FINANCIAL_FIELD_NAMES.intersection(self.data))
            forbidden.update(
                name
                for name in (
                    "asset_status",
                    "record_status",
                    "asset_code",
                    "current_issued_code",
                    "requested_coding_scheme",
                    "tracking_mode",
                )
                if name in self.data
            )
            if forbidden:
                self._forbidden_post_fields = sorted(forbidden)
            else:
                self._forbidden_post_fields = []
        else:
            self._forbidden_post_fields = []

    def clean_quantity(self):
        if self.cleaned_data.get("quantity") != 1:
            raise ValidationError("V1 每条资产记录数量必须为 1。")
        return 1

    def clean(self):
        cleaned = super().clean()
        if self._forbidden_post_fields:
            raise ValidationError(
                "请求包含无权写入字段："
                + "、".join(self._forbidden_post_fields)
                + "。"
            )
        department = cleaned.get("department")
        employee = cleaned.get("responsible_employee")
        location = cleaned.get("location")
        category = cleaned.get("category")
        for field, value in (
            ("department", department),
            ("responsible_employee", employee),
            ("location", location),
            ("category", category),
        ):
            if value is not None and value.company_id != self.company.pk:
                self.add_error(field, "所选记录不属于当前公司。")
        if employee and department and employee.department_id != department.pk:
            self.add_error("responsible_employee", "责任人必须属于当前部门。")
        if location and location.children.exists():
            self.add_error("location", "资产必须选择树形位置的叶级节点。")
        return cleaned


class RequestedCodingSchemeForm(forms.Form):
    requested_coding_scheme = forms.ModelChoiceField(
        label="指定编码方案版本",
        queryset=AssetCodingScheme.objects.none(),
        required=False,
        help_text="留空时由财务确认事务按分类默认、公司默认解析。",
    )

    def __init__(self, *args, actor=None, asset=None, **kwargs):
        if actor is None or asset is None or not can_set_requested_coding_scheme(
            actor, asset
        ):
            raise PermissionDenied("只有 system_admin 可在正式化前指定编码方案。")
        self.actor = actor
        self.asset = asset
        super().__init__(*args, **kwargs)
        today = timezone.localdate()
        self.fields["requested_coding_scheme"].queryset = (
            AssetCodingScheme.objects.filter(
                company=asset.company,
                status=AssetCodingScheme.Status.ACTIVE,
                effective_from__lte=today,
            )
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
            .order_by("scheme_key", "-version")
        )
        self.fields["requested_coding_scheme"].initial = (
            asset.requested_coding_scheme_id
        )
        _bootstrap_widgets(self)


def _build_custom_value_field(custom_field):
    common = {"label": custom_field.name, "required": custom_field.required}
    field_type = custom_field.field_type
    if field_type == AssetCustomField.FieldType.TEXT:
        return forms.CharField(**common)
    if field_type == AssetCustomField.FieldType.DECIMAL:
        return forms.DecimalField(**common, max_digits=30, decimal_places=8)
    if field_type == AssetCustomField.FieldType.DATE:
        return forms.DateField(
            **common, widget=forms.DateInput(attrs={"type": "date"})
        )
    if field_type == AssetCustomField.FieldType.BOOLEAN:
        return forms.TypedChoiceField(
            **common,
            choices=(("", "---------"), ("true", "是"), ("false", "否")),
            coerce=lambda value: value == "true",
            empty_value=None,
        )
    if field_type == AssetCustomField.FieldType.SELECT:
        return forms.ChoiceField(
            **common,
            choices=[("", "---------")]
            + [(option, option) for option in custom_field.options_json],
        )
    raise ValidationError("不支持的动态字段类型。")


class AssetCustomValueForm(forms.Form):
    """One typed value form bound to a concrete approved custom field."""

    def __init__(self, *args, custom_field=None, **kwargs):
        if custom_field is None or not custom_field.is_active:
            raise ValidationError("动态字段不存在或已停用。")
        self.custom_field = custom_field
        super().__init__(*args, **kwargs)
        self.fields["value"] = _build_custom_value_field(custom_field)
        _bootstrap_widgets(self)


class AssetSubmitForm(forms.Form):
    confirm = forms.BooleanField(label="确认提交财务确认", required=True)

    def __init__(self, *args, actor=None, asset=None, **kwargs):
        if actor is None or asset is None or not can_submit_asset(actor, asset):
            raise PermissionDenied("您没有提交此资产的权限。")
        self.actor = actor
        self.asset = asset
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class AssetWithdrawForm(forms.Form):
    reason = forms.CharField(
        label="撤回/退回原因", max_length=500, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args, actor=None, asset=None, **kwargs):
        if actor is None or asset is None or not can_withdraw_asset(actor, asset):
            raise PermissionDenied("您没有撤回或退回此资产的权限。")
        self.actor = actor
        self.asset = asset
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class AssetDeleteForm(forms.Form):
    reason = forms.CharField(
        label="删除原因", max_length=500, widget=forms.Textarea(attrs={"rows": 3})
    )
    confirm = forms.BooleanField(label="确认删除未提交草稿", required=True)

    def __init__(self, *args, actor=None, asset=None, **kwargs):
        if actor is None or asset is None or not can_delete_asset_draft(actor, asset):
            raise PermissionDenied("您没有删除此资产草稿的权限。")
        self.actor = actor
        self.asset = asset
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class AssetAttachmentUploadForm(forms.Form):
    role = forms.ChoiceField(label="附件用途", choices=())
    security_class = forms.ChoiceField(label="安全分类", choices=())
    file = forms.FileField(label="选择文件")

    def __init__(self, *args, actor=None, asset=None, **kwargs):
        if actor is None or asset is None:
            raise PermissionDenied("附件表单必须绑定当前操作用户和资产。")
        self.actor = actor
        self.asset = asset
        super().__init__(*args, **kwargs)
        roles = role_names_for(actor)
        if "finance" in roles and asset.asset_status in {"draft", "pending_finance"}:
            self.fields["role"].choices = AttachmentLink.Role.choices
            self.fields["security_class"].choices = AttachmentLink.SecurityClass.choices
        elif can_create_attachment_link(actor, asset, "A0"):
            self.fields["role"].choices = [
                choice
                for choice in AttachmentLink.Role.choices
                if choice[0]
                in {
                    AttachmentLink.Role.COVER,
                    AttachmentLink.Role.PHOTO,
                    AttachmentLink.Role.CERTIFICATE,
                    AttachmentLink.Role.MANUAL,
                    AttachmentLink.Role.OTHER,
                }
            ]
            self.fields["security_class"].choices = [
                (AttachmentLink.SecurityClass.A0, "普通附件")
            ]
        else:
            raise PermissionDenied("您没有上传资产附件的权限。")
        _bootstrap_widgets(self)

    def clean(self):
        cleaned = super().clean()
        role = cleaned.get("role")
        security_class = cleaned.get("security_class")
        if role in {AttachmentLink.Role.COVER, AttachmentLink.Role.PHOTO} and security_class != "A0":
            self.add_error("security_class", "封面和资产照片只能使用 A0 普通分类。")
        if role in {
            AttachmentLink.Role.INVOICE,
            AttachmentLink.Role.CONTRACT,
            AttachmentLink.Role.ACCEPTANCE,
        } and security_class != "A1":
            self.add_error("security_class", "该财务附件用途必须使用 A1 分类。")
        if role == AttachmentLink.Role.OTHER and security_class == "A1" and (
            "finance" not in role_names_for(self.actor)
        ):
            raise PermissionDenied("只有 finance 可以创建 A1 财务附件。")
        if security_class and not can_create_attachment_link(
            self.actor, self.asset, security_class
        ):
            raise PermissionDenied("您没有上传此安全分类附件的权限。")
        return cleaned


class AssetAttachmentVoidForm(forms.Form):
    reason = forms.CharField(
        label="作废原因", max_length=500, widget=forms.Textarea(attrs={"rows": 3})
    )

    def __init__(self, *args, actor=None, link=None, **kwargs):
        if actor is None or link is None or not can_void_attachment_link(actor, link):
            raise PermissionDenied("您没有作废此附件的权限。")
        self.actor = actor
        self.link = link
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)
