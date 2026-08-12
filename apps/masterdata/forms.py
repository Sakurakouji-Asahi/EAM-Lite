"""Chinese, company-scoped forms for Sprint 1 master data."""

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError

from apps.accounts.roles import ROLE_NAMES
from apps.masterdata.models import (
    AssetCategory,
    Company,
    Department,
    Employee,
    Location,
    UserDepartmentScope,
)
from apps.masterdata.permissions import require_manage_masterdata
from apps.masterdata.services import SAFE_ATTACHMENT_EXTENSIONS


def _bootstrap_widgets(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxInput):
            widget.attrs.setdefault("class", "form-check-input")
        elif isinstance(widget, (forms.Select, forms.SelectMultiple)):
            widget.attrs.setdefault("class", "form-select")
        else:
            widget.attrs.setdefault("class", "form-control")


class AuthorizedModelForm(forms.ModelForm):
    resource = None

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None:
            raise PermissionDenied("表单必须绑定当前操作用户。")
        if self.resource:
            require_manage_masterdata(actor, self.resource)
        self.actor = actor
        self.company = company
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class CompanyForm(AuthorizedModelForm):
    resource = "company"

    class Meta:
        model = Company
        fields = ("code", "name", "short_name", "currency", "timezone")
        labels = {
            "code": "公司编码",
            "name": "公司全称",
            "short_name": "公司简称",
            "currency": "币种",
            "timezone": "业务时区",
        }
        help_texts = {
            "code": "保存时将执行 NFKC、去除首尾空格并按不区分大小写的规则判重。",
        }

    def clean_currency(self):
        value = self.cleaned_data["currency"].strip().upper()
        if value != "CNY":
            raise ValidationError("V1 公司币种必须为 CNY。")
        return value

    def clean_timezone(self):
        value = self.cleaned_data["timezone"].strip()
        if value != "Asia/Shanghai":
            raise ValidationError("V1 业务时区必须为 Asia/Shanghai。")
        return value


class DepartmentForm(AuthorizedModelForm):
    resource = "department"

    class Meta:
        model = Department
        fields = ("code", "name", "parent", "manager_employee")
        labels = {
            "code": "部门编码",
            "name": "部门名称",
            "parent": "上级部门",
            "manager_employee": "部门经理",
        }
        help_texts = {
            "manager_employee": "可选择公司内任意启用部门的在职且启用员工；不要求登录账号或部门经理角色。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = Department.objects.filter(
            company=self.company, is_active=True
        ).order_by("normalized_code")
        if self.instance.pk:
            self.fields["parent"].queryset = self.fields["parent"].queryset.exclude(
                pk=self.instance.pk
            )
        self.fields["manager_employee"].queryset = Employee.objects.filter(
            company=self.company,
            employment_status="active",
            is_active=True,
            department__is_active=True,
        ).select_related("department").order_by("normalized_employee_no")

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        if parent and parent.company_id != self.company.pk:
            raise ValidationError("上级部门不属于当前公司。")
        return parent

    def clean_manager_employee(self):
        manager = self.cleaned_data.get("manager_employee")
        if manager is None:
            return None
        if manager.company_id != self.company.pk:
            raise ValidationError("部门经理不属于当前公司。")
        if manager.employment_status != "active" or not manager.is_active:
            raise ValidationError("部门经理必须是在职且启用的员工。")
        if not manager.department.is_active:
            raise ValidationError("部门经理必须属于启用部门。")
        return manager


class EmployeeForm(AuthorizedModelForm):
    resource = "employee"

    class Meta:
        model = Employee
        fields = (
            "employee_no",
            "name",
            "department",
            "employment_status",
            "hire_date",
            "termination_date",
            "mobile",
            "remark",
        )
        labels = {
            "employee_no": "员工编号",
            "name": "姓名",
            "department": "所属部门",
            "employment_status": "任职状态",
            "hire_date": "入职日期",
            "termination_date": "实际离职日期",
            "mobile": "手机号码",
            "remark": "备注",
        }
        widgets = {
            "hire_date": forms.DateInput(attrs={"type": "date"}),
            "termination_date": forms.DateInput(attrs={"type": "date"}),
            "remark": forms.Textarea(attrs={"rows": 3}),
        }
        help_texts = {
            "employment_status": "进入离职处理中或已离职将明确停用本员工；账号启停不受静默联动。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.filter(
            company=self.company, is_active=True
        ).order_by("normalized_code")
        if self.instance.pk and self.instance.employment_status == "resigned":
            self.fields["employment_status"].disabled = True

    def clean_department(self):
        department = self.cleaned_data.get("department")
        if department and department.company_id != self.company.pk:
            raise ValidationError("部门不属于当前公司。")
        return department

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("employment_status")
        termination_date = cleaned.get("termination_date")
        if status == "resigned" and termination_date is None:
            self.add_error("termination_date", "已离职员工必须填写实际离职日期。")
        elif status in {"active", "leaving"} and termination_date is not None:
            self.add_error(
                "termination_date", "在职或离职处理中员工不能填写实际离职日期。"
            )
        if (
            self.instance.pk
            and self.instance.employment_status == "resigned"
            and status != "resigned"
        ):
            self.add_error("employment_status", "普通页面不允许已离职恢复为在职。")
        return cleaned


class EmployeeTechnicalLinkForm(AuthorizedModelForm):
    resource = "employee_user"

    class Meta:
        model = Employee
        fields = ("user",)
        labels = {"user": "关联登录账号"}
        help_texts = {
            "user": "仅建立技术关联；不会修改员工任职状态，也不会联动账号启停。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        User = get_user_model()
        used_ids = Employee.objects.exclude(pk=self.instance.pk).exclude(user=None).values_list(
            "user_id", flat=True
        )
        self.fields["user"].queryset = (
            User.objects.filter(is_superuser=False)
            .exclude(pk__in=used_ids)
            .order_by("username")
        )


class LocationForm(AuthorizedModelForm):
    resource = "location"

    class Meta:
        model = Location
        fields = ("code", "name", "parent", "location_type")
        labels = {
            "code": "位置编码",
            "name": "位置名称",
            "parent": "上级位置",
            "location_type": "位置类型",
        }
        help_texts = {"parent": "层级由父路径计算，数据库支持超过三层的树。"}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = Location.objects.filter(
            company=self.company, is_active=True
        ).order_by("level", "normalized_code")
        if self.instance.pk:
            self.fields["parent"].queryset = self.fields["parent"].queryset.exclude(
                pk=self.instance.pk
            )

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        if parent and parent.company_id != self.company.pk:
            raise ValidationError("上级位置不属于当前公司。")
        return parent


class AssetCategoryForm(AuthorizedModelForm):
    resource = "asset_category"

    class Meta:
        model = AssetCategory
        fields = (
            "code",
            "name",
            "parent",
            "category_type",
            "is_maintenance_required_default",
        )
        labels = {
            "code": "分类编码",
            "name": "实物分类名称",
            "parent": "上级分类",
            "category_type": "实物类型",
            "is_maintenance_required_default": "默认需要保养",
        }
        help_texts = {
            "category_type": "这里只表达实物管理分类，不表示是否为固定资产。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["parent"].queryset = AssetCategory.objects.filter(
            company=self.company, is_active=True
        ).order_by("category_level", "normalized_code")
        if self.instance.pk:
            self.fields["parent"].queryset = self.fields["parent"].queryset.exclude(
                pk=self.instance.pk
            )

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        if parent and parent.company_id != self.company.pk:
            raise ValidationError("上级分类不属于当前公司。")
        return parent


class SystemSettingForm(forms.Form):
    attachment_allowed_extensions = forms.MultipleChoiceField(
        label="允许的附件扩展名",
        choices=[(value, value.upper()) for value in sorted(SAFE_ATTACHMENT_EXTENSIONS)],
        widget=forms.CheckboxSelectMultiple,
    )
    attachment_max_size_bytes = forms.IntegerField(
        label="单个附件最大字节数",
        min_value=1,
        max_value=20 * 1024 * 1024,
        help_text="最大不超过 20971520 字节（20 MiB）。",
    )

    def __init__(self, *args, actor=None, **kwargs):
        if actor is None:
            raise PermissionDenied("表单必须绑定当前操作用户。")
        require_manage_masterdata(actor, "system_setting")
        self.actor = actor
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class UserRoleForm(forms.Form):
    roles = forms.MultipleChoiceField(
        label="固定角色",
        choices=[(role, role) for role in ROLE_NAMES],
        widget=forms.CheckboxSelectMultiple,
        required=False,
    )
    reason = forms.CharField(
        label="变更原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    current_password = forms.CharField(
        label="当前操作人密码",
        widget=forms.PasswordInput(render_value=False),
        required=False,
        help_text="授予或移除 system_admin / finance 时必须填写。",
    )

    def __init__(self, *args, actor=None, **kwargs):
        if actor is None:
            raise PermissionDenied("表单必须绑定当前操作用户。")
        require_manage_masterdata(actor, "user_permissions")
        self.actor = actor
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class ScopeAssignForm(forms.Form):
    department = forms.ModelChoiceField(
        label="授权根部门", queryset=Department.objects.none()
    )
    include_descendants = forms.BooleanField(
        label="包含下级部门", required=False, initial=True
    )
    reason = forms.CharField(
        label="授权原因", max_length=500, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None:
            raise PermissionDenied("表单必须绑定当前操作用户。")
        require_manage_masterdata(actor, "user_permissions")
        self.actor = actor
        self.company = company
        super().__init__(*args, **kwargs)
        self.fields["department"].queryset = Department.objects.filter(
            company=company, is_active=True
        ).order_by("normalized_code")
        _bootstrap_widgets(self)


class ScopeRevokeForm(forms.Form):
    reason = forms.CharField(
        label="撤销原因", max_length=500, widget=forms.Textarea(attrs={"rows": 2})
    )

    def __init__(self, *args, actor=None, **kwargs):
        if actor is None:
            raise PermissionDenied("表单必须绑定当前操作用户。")
        require_manage_masterdata(actor, "user_permissions")
        self.actor = actor
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class ConfirmStatusForm(forms.Form):
    confirm = forms.BooleanField(label="我确认执行此操作", required=True)

    def __init__(self, *args, actor=None, resource=None, **kwargs):
        if actor is None or resource is None:
            raise PermissionDenied("确认表单必须绑定操作用户和资源。")
        require_manage_masterdata(actor, resource)
        self.actor = actor
        self.resource = resource
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)
