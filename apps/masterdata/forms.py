"""Chinese, company-scoped forms for Sprint 1 master data."""

from django import forms
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import PermissionDenied, ValidationError
from django.db.models import Q
from django.utils import timezone

from apps.accounts.roles import ROLE_CHOICES
from apps.masterdata.models import (
    AssetCategory,
    AssetCodingScheme,
    AssetCodingSegment,
    Company,
    Department,
    Employee,
    Location,
    UserDepartmentScope,
)
from apps.masterdata.permissions import can_manage_masterdata, require_manage_masterdata
from apps.masterdata.services import SAFE_ATTACHMENT_EXTENSIONS


def _bootstrap_widgets(form):
    for field in form.fields.values():
        widget = field.widget
        if isinstance(widget, forms.CheckboxSelectMultiple):
            widget.attrs.pop("class", None)
        elif isinstance(widget, forms.CheckboxInput):
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
        if self.instance.pk:
            self.fields["employment_status"].disabled = True
            self.fields["termination_date"].disabled = True
            self.fields["employment_status"].help_text = (
                "任职状态只能通过离职资产清退流程变更。"
            )

    def clean_department(self):
        department = self.cleaned_data.get("department")
        if department and department.company_id != self.company.pk:
            raise ValidationError("部门不属于当前公司。")
        return department

    def clean(self):
        cleaned = super().clean()
        status = cleaned.get("employment_status")
        termination_date = cleaned.get("termination_date")
        if self.instance.pk:
            if status != self.instance.employment_status:
                self.add_error("employment_status", "任职状态只能通过离职资产清退流程变更。")
            if termination_date != self.instance.termination_date:
                self.add_error("termination_date", "实际离职日期只能由清退完成动作写入。")
        else:
            if status != "active":
                self.add_error("employment_status", "新建员工必须是正常在职状态。")
            if termination_date is not None:
                self.add_error("termination_date", "新建员工不能填写实际离职日期。")
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
            "default_coding_scheme",
        )
        labels = {
            "code": "分类编码",
            "name": "实物分类名称",
            "parent": "上级分类",
            "category_type": "实物类型",
            "is_maintenance_required_default": "默认需要保养",
            "default_coding_scheme": "默认编码方案版本",
        }
        help_texts = {
            "category_type": "这里只表达实物管理分类，不表示是否为固定资产。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not can_manage_masterdata(self.actor, "coding_scheme"):
            # equipment may maintain the physical category, but selecting a
            # coding version is a separate system_admin-only control.
            self.fields.pop("default_coding_scheme", None)
        self.fields["parent"].queryset = AssetCategory.objects.filter(
            company=self.company, is_active=True
        ).order_by("category_level", "normalized_code")
        if self.instance.pk:
            self.fields["parent"].queryset = self.fields["parent"].queryset.exclude(
                pk=self.instance.pk
            )
        if "default_coding_scheme" in self.fields:
            today = timezone.localdate()
            self.fields["default_coding_scheme"].queryset = AssetCodingScheme.objects.filter(
                company=self.company,
                status=AssetCodingScheme.Status.ACTIVE,
                effective_from__lte=today,
            ).filter(
                Q(effective_to__isnull=True) | Q(effective_to__gte=today)
            ).order_by("scheme_key", "-version")

    def clean_parent(self):
        parent = self.cleaned_data.get("parent")
        if parent and parent.company_id != self.company.pk:
            raise ValidationError("上级分类不属于当前公司。")
        return parent

    def clean_default_coding_scheme(self):
        scheme = self.cleaned_data.get("default_coding_scheme")
        if scheme is not None and scheme.company_id != self.company.pk:
            raise ValidationError("默认编码方案不属于当前公司。")
        return scheme


class AssetCodingSchemeForm(forms.ModelForm):
    class Meta:
        model = AssetCodingScheme
        fields = (
            "scheme_key",
            "name",
            "description",
            "reset_mode",
            "sequence_start",
            "category_scope_level",
            "effective_from",
            "effective_to",
        )
        labels = {
            "scheme_key": "方案稳定键",
            "name": "方案名称",
            "description": "说明",
            "reset_mode": "流水重置模式",
            "sequence_start": "首个可签发流水值",
            "category_scope_level": "分类作用域层级",
            "effective_from": "生效开始日",
            "effective_to": "生效结束日",
        }
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "effective_from": forms.DateInput(attrs={"type": "date"}),
            "effective_to": forms.DateInput(attrs={"type": "date"}),
        }
        help_texts = {
            "scheme_key": "同一方案的各版本使用相同稳定键。",
            "reset_mode": "选择“按分类”模式时，必须同时选择分类作用域层级。",
            "sequence_start": "这是首个预览/未来签发值，不是计数器初值。",
            "category_scope_level": "仅按分类重置时填写；其他重置模式会自动忽略。",
            "effective_to": "结束日当天仍有效；留空表示无结束日。",
        }

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None:
            raise PermissionDenied("表单必须绑定当前操作用户。")
        require_manage_masterdata(actor, "coding_scheme")
        self.actor = actor
        self.company = company
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)

    def clean_scheme_key(self):
        value = self.cleaned_data["scheme_key"]
        if value != value.strip() or not value.strip():
            raise ValidationError("方案稳定键不能为空或包含首尾空白。")
        return value

    def clean(self):
        cleaned = super().clean()
        reset_mode = cleaned.get("reset_mode")
        category_scope_level = cleaned.get("category_scope_level")
        category_modes = {
            AssetCodingScheme.ResetMode.CATEGORY_YEARLY,
            AssetCodingScheme.ResetMode.CATEGORY_MONTHLY,
        }
        ordinary_modes = {
            AssetCodingScheme.ResetMode.NEVER,
            AssetCodingScheme.ResetMode.YEARLY,
            AssetCodingScheme.ResetMode.MONTHLY,
        }
        if reset_mode in category_modes and category_scope_level is None:
            self.add_error(
                "category_scope_level",
                "按分类重置时必须选择大类、小类或叶级分类。",
            )
        elif reset_mode in ordinary_modes:
            cleaned["category_scope_level"] = None

        sequence_start = cleaned.get("sequence_start")
        if sequence_start is not None and sequence_start < 0:
            self.add_error("sequence_start", "首个可签发流水值不得为负数。")

        effective_from = cleaned.get("effective_from")
        effective_to = cleaned.get("effective_to")
        if effective_to and not effective_from:
            self.add_error("effective_from", "填写生效结束日时必须同时填写开始日。")
        elif effective_from and effective_to and effective_to < effective_from:
            self.add_error("effective_to", "生效结束日不得早于开始日。")
        return cleaned


class AssetCodingSegmentForm(forms.ModelForm):
    class Meta:
        model = AssetCodingSegment
        fields = (
            "sequence_order",
            "segment_type",
            "fixed_value",
            "sequence_length",
            "zero_pad",
        )
        labels = {
            "sequence_order": "顺序",
            "segment_type": "片段类型",
            "fixed_value": "固定值",
            "sequence_length": "流水位数",
            "zero_pad": "左侧补零",
        }
        help_texts = {
            "fixed_value": (
                "仅固定文本、自定义固定文本或分隔符填写；公司编码等来源片段自动读取主数据。"
            ),
            "sequence_length": "仅顺序号填写，必须为 1–12。",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["zero_pad"].required = False
        _bootstrap_widgets(self)

    def full_clean(self):
        super().full_clean()
        if getattr(self, "cleaned_data", {}).get("DELETE"):
            self._errors.clear()

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("DELETE"):
            return cleaned
        from apps.coding.domain import validate_segment_fields

        segment_type = cleaned.get("segment_type")
        if not segment_type:
            return cleaned
        fixed_value_types = {
            AssetCodingSegment.SegmentType.FIXED_TEXT,
            AssetCodingSegment.SegmentType.CUSTOM_TEXT,
            AssetCodingSegment.SegmentType.SEPARATOR,
        }
        if segment_type == AssetCodingSegment.SegmentType.SEQUENCE:
            cleaned["fixed_value"] = None
        elif segment_type in fixed_value_types:
            cleaned["sequence_length"] = None
            cleaned["zero_pad"] = None
        elif segment_type:
            cleaned["fixed_value"] = None
            cleaned["sequence_length"] = None
            cleaned["zero_pad"] = None

        try:
            validate_segment_fields(
                segment_type=segment_type,
                fixed_value=cleaned.get("fixed_value"),
                format_string=None,
                sequence_length=cleaned.get("sequence_length"),
                zero_pad=cleaned.get("zero_pad"),
            )
        except ValidationError as exc:
            raise exc
        return cleaned


AssetCodingSegmentFormSet = forms.inlineformset_factory(
    AssetCodingScheme,
    AssetCodingSegment,
    form=AssetCodingSegmentForm,
    extra=1,
    can_delete=True,
    min_num=1,
    validate_min=True,
)


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
        choices=ROLE_CHOICES,
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
        help_text="授予或移除系统管理员、财务角色时必须填写。",
    )

    def __init__(self, *args, actor=None, **kwargs):
        if actor is None:
            raise PermissionDenied("表单必须绑定当前操作用户。")
        require_manage_masterdata(actor, "user_permissions")
        self.actor = actor
        super().__init__(*args, **kwargs)
        _bootstrap_widgets(self)


class ApplicationUserCreateForm(forms.Form):
    username = forms.CharField(label="用户名", max_length=150)
    display_name = forms.CharField(label="显示名称", max_length=100)
    email = forms.EmailField(label="电子邮箱", required=False)
    mobile = forms.CharField(label="手机号码", max_length=32, required=False)
    roles = forms.MultipleChoiceField(
        label="固定角色",
        choices=ROLE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        help_text="至少选择一个角色；角色权限由固定矩阵决定。",
    )
    initial_department = forms.ModelChoiceField(
        label="部门负责人初始范围",
        queryset=Department.objects.none(),
        required=False,
        empty_label="不配置",
        help_text="仅选择“部门负责人”角色时必填。",
    )
    include_descendants = forms.BooleanField(
        label="部门范围包含下级部门",
        required=False,
        initial=True,
    )
    password = forms.CharField(
        label="新用户密码",
        strip=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "new-password"},
        ),
    )
    password_confirm = forms.CharField(
        label="再次输入新用户密码",
        strip=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "new-password"},
        ),
    )
    reason = forms.CharField(
        label="创建原因",
        max_length=500,
        widget=forms.Textarea(attrs={"rows": 2}),
    )
    current_password = forms.CharField(
        label="当前操作人密码",
        strip=False,
        widget=forms.PasswordInput(
            render_value=False,
            attrs={"autocomplete": "current-password"},
        ),
        help_text="创建账号前必须再次确认当前系统管理员身份。",
    )

    def __init__(self, *args, actor=None, company=None, **kwargs):
        if actor is None or company is None:
            raise PermissionDenied("创建用户表单必须绑定当前操作人和公司。")
        require_manage_masterdata(actor, "user_permissions")
        self.actor = actor
        self.company = company
        super().__init__(*args, **kwargs)
        self.fields["initial_department"].queryset = Department.objects.filter(
            company=company,
            is_active=True,
        ).order_by("normalized_code", "pk")
        _bootstrap_widgets(self)

    def clean_username(self):
        value = get_user_model().normalize_username(
            self.cleaned_data["username"].strip()
        )
        if get_user_model().objects.filter(username__iexact=value).exists():
            raise ValidationError("该用户名已存在。")
        return value

    def clean_current_password(self):
        value = self.cleaned_data["current_password"]
        if not self.actor.check_password(value):
            raise ValidationError("当前操作人密码验证失败。")
        return value

    def clean(self):
        cleaned = super().clean()
        password = cleaned.get("password")
        confirmation = cleaned.get("password_confirm")
        if password and confirmation and password != confirmation:
            self.add_error("password_confirm", "两次输入的新用户密码不一致。")
        roles = set(cleaned.get("roles") or ())
        department = cleaned.get("initial_department")
        if "department_manager" in roles and department is None:
            self.add_error(
                "initial_department",
                "部门负责人必须同时配置一个启用部门范围。",
            )
        elif "department_manager" not in roles and department is not None:
            self.add_error(
                "initial_department",
                "只有部门负责人需要在创建时配置部门范围。",
            )
        if password and not self.errors.get("password_confirm"):
            User = get_user_model()
            candidate = User(
                username=cleaned.get("username", ""),
                display_name=cleaned.get("display_name", ""),
                email=cleaned.get("email", ""),
                mobile=cleaned.get("mobile", ""),
            )
            try:
                validate_password(password, user=candidate)
            except ValidationError as exc:
                self.add_error("password", exc)
        return cleaned


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
