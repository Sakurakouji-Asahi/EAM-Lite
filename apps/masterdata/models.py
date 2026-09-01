from __future__ import annotations

from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from .normalization import clean_display_identifier, normalize_identifier


HEX64_VALIDATOR = RegexValidator(
    regex=r"\A[0-9a-f]{64}\Z",
    message="必须是 64 位小写十六进制 SHA-256。",
)


class NormalizedCodeModel(models.Model):
    code = models.CharField("编码", max_length=100)
    normalized_code = models.CharField("规范化编码", max_length=100, editable=False)

    class Meta:
        abstract = True

    def _normalize_code(self):
        self.code = clean_display_identifier(self.code)
        self.normalized_code = normalize_identifier(self.code)

    def clean(self):
        super().clean()
        raw_code = self.code
        self._normalize_code()
        if raw_code not in (None, "") and not self.normalized_code:
            raise ValidationError({"code": "编码不能为空。"})

    def save(self, *args, **kwargs):
        self._normalize_code()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "code" in update_fields:
            kwargs["update_fields"] = {*update_fields, "normalized_code"}
        return super().save(*args, **kwargs)


class TimeStampedModel(models.Model):
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True


def _validate_tree_node(instance, *, level_field: str | None = None):
    parent = instance.parent
    if parent is None:
        if level_field:
            setattr(instance, level_field, 1)
        return
    if parent.pk == instance.pk and instance.pk is not None:
        raise ValidationError({"parent": "不能把自身设为上级。"})
    if parent.company_id != instance.company_id:
        raise ValidationError({"parent": "上级必须属于同一公司。"})
    seen = {instance.pk} if instance.pk is not None else set()
    ancestor = parent
    while ancestor is not None:
        if ancestor.pk in seen:
            raise ValidationError({"parent": "树形关系不能形成循环。"})
        seen.add(ancestor.pk)
        ancestor = ancestor.parent
    if level_field:
        setattr(instance, level_field, getattr(parent, level_field) + 1)


class Company(NormalizedCodeModel, TimeStampedModel):
    name = models.CharField("公司名称", max_length=200)
    short_name = models.CharField("公司简称", max_length=100)
    currency = models.CharField("币种", max_length=3, default="CNY")
    timezone = models.CharField("业务时区", max_length=64, default="Asia/Shanghai")
    is_active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "公司"
        verbose_name_plural = "公司"
        constraints = [
            models.UniqueConstraint(
                fields=("normalized_code",), name="uq_company_normalized_code"
            ),
            models.UniqueConstraint(
                fields=("is_active",),
                condition=Q(is_active=True),
                name="uq_company_single_active",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""), name="ck_company_code_nonempty"
            ),
            models.CheckConstraint(
                condition=Q(currency="CNY"), name="ck_company_currency_cny"
            ),
            models.CheckConstraint(
                condition=Q(timezone="Asia/Shanghai"),
                name="ck_company_timezone_shanghai",
            ),
        ]

    def __str__(self):
        return self.short_name or self.name


class Department(NormalizedCodeModel, TimeStampedModel):
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="departments",
    )
    name = models.CharField("部门名称", max_length=200)
    parent = models.ForeignKey(
        "self",
        verbose_name="上级部门",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    manager_employee = models.ForeignKey(
        "Employee",
        verbose_name="部门经理",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="managed_departments",
    )
    is_active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "部门"
        verbose_name_plural = "部门"
        ordering = ("company_id", "normalized_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_department_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""), name="ck_department_code_nonempty"
            ),
            models.CheckConstraint(
                condition=~Q(id=models.F("parent_id")),
                name="ck_department_not_self_parent",
            ),
        ]

    def clean(self):
        super().clean()
        _validate_tree_node(self)
        manager = self.manager_employee
        if manager is not None:
            if manager.company_id != self.company_id:
                raise ValidationError(
                    {"manager_employee": "部门经理必须属于同一公司。"}
                )
            if manager.employment_status != Employee.EmploymentStatus.ACTIVE:
                raise ValidationError(
                    {"manager_employee": "部门经理必须是在职员工。"}
                )
            if not manager.is_active:
                raise ValidationError(
                    {"manager_employee": "部门经理必须为启用状态。"}
                )
            if not manager.department_id or not manager.department.is_active:
                raise ValidationError(
                    {"manager_employee": "部门经理必须属于一个启用部门。"}
                )

    def __str__(self):
        return self.name


class EmployeeQuerySet(models.QuerySet):
    """Keep employment transitions behind the offboarding domain service."""

    def update(self, **kwargs):
        if {"employment_status", "termination_date"}.intersection(kwargs):
            raise ValidationError(
                "任职状态和实际离职日期只能通过受控离职清退 Service 修改。"
            )
        return super().update(**kwargs)


class Employee(TimeStampedModel):
    class EmploymentStatus(models.TextChoices):
        ACTIVE = "active", "在职"
        LEAVING = "leaving", "离职处理中"
        RESIGNED = "resigned", "已离职"

    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="employees",
    )
    employee_no = models.CharField("员工编号", max_length=100)
    normalized_employee_no = models.CharField(
        "规范化员工编号", max_length=100, editable=False
    )
    name = models.CharField("姓名", max_length=100)
    department = models.ForeignKey(
        Department,
        verbose_name="所属部门",
        on_delete=models.PROTECT,
        related_name="employees",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="登录账号",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="employees",
    )
    employment_status = models.CharField(
        "任职状态",
        max_length=16,
        choices=EmploymentStatus.choices,
        default=EmploymentStatus.ACTIVE,
    )
    hire_date = models.DateField("入职日期", null=True, blank=True)
    termination_date = models.DateField("实际离职日期", null=True, blank=True)
    mobile = models.CharField("手机号码", max_length=32, blank=True)
    remark = models.TextField("备注", blank=True)
    is_active = models.BooleanField("启用", default=True)

    objects = EmployeeQuerySet.as_manager()

    class Meta:
        verbose_name = "员工"
        verbose_name_plural = "员工"
        ordering = ("company_id", "normalized_employee_no")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_employee_no"),
                name="uq_employee_company_no",
            ),
            models.UniqueConstraint(
                fields=("user",),
                condition=Q(user__isnull=False),
                name="uq_employee_user",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_employee_no=""),
                name="ck_employee_no_nonempty",
            ),
            models.CheckConstraint(
                condition=(
                    Q(employment_status="active")
                    | Q(employment_status__in=("leaving", "resigned"), is_active=False)
                ),
                name="ck_employee_status_active_flag",
            ),
            models.CheckConstraint(
                condition=(
                    Q(employment_status="resigned", termination_date__isnull=False)
                    | Q(
                        employment_status__in=("active", "leaving"),
                        termination_date__isnull=True,
                    )
                ),
                name="ck_employee_termination_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(hire_date__isnull=True)
                    | Q(termination_date__isnull=True)
                    | Q(termination_date__gte=models.F("hire_date"))
                ),
                name="ck_employee_termination_after_hire",
            ),
            models.CheckConstraint(
                condition=Q(employment_status__in=("active", "leaving", "resigned")),
                name="ck_employee_employment_status_valid",
            ),
        ]

    def _normalize_employee_no(self):
        self.employee_no = clean_display_identifier(self.employee_no)
        self.normalized_employee_no = normalize_identifier(self.employee_no)

    @property
    def can_receive_new_responsibility(self):
        return bool(
            self.employment_status == self.EmploymentStatus.ACTIVE
            and self.is_active
            and self.company.is_active
            and self.department.is_active
        )

    def clean(self):
        super().clean()
        raw_employee_no = self.employee_no
        self._normalize_employee_no()
        if raw_employee_no not in (None, "") and not self.normalized_employee_no:
            raise ValidationError({"employee_no": "员工编号不能为空。"})
        if self.department_id and self.department.company_id != self.company_id:
            raise ValidationError({"department": "所属部门必须属于同一公司。"})
        if self.employment_status in {
            self.EmploymentStatus.LEAVING,
            self.EmploymentStatus.RESIGNED,
        } and self.is_active:
            raise ValidationError(
                {"is_active": "离职处理中或已离职员工不能处于启用状态。"}
            )
        if self.employment_status == self.EmploymentStatus.RESIGNED:
            if self.termination_date is None:
                raise ValidationError(
                    {"termination_date": "已离职员工必须填写实际离职日期。"}
                )
        elif self.termination_date is not None:
            raise ValidationError(
                {"termination_date": "仅已离职员工可以填写实际离职日期。"}
            )
        if (
            self.hire_date
            and self.termination_date
            and self.termination_date < self.hire_date
        ):
            raise ValidationError(
                {"termination_date": "实际离职日期不得早于入职日期。"}
            )

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self)._base_manager.filter(pk=self.pk).values(
                "employment_status", "termination_date"
            ).first()
            if previous is not None and (
                previous["employment_status"] != self.employment_status
                or previous["termination_date"] != self.termination_date
            ):
                raise ValidationError(
                    "任职状态和实际离职日期只能通过受控离职清退 Service 修改。"
                )
        self._normalize_employee_no()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "employee_no" in update_fields:
            kwargs["update_fields"] = {*update_fields, "normalized_employee_no"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name}（{self.employee_no}）"


class UserDepartmentScope(models.Model):
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="user_department_scopes",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="用户",
        on_delete=models.PROTECT,
        related_name="department_scopes",
    )
    department = models.ForeignKey(
        Department,
        verbose_name="授权根部门",
        on_delete=models.PROTECT,
        related_name="user_scopes",
    )
    include_descendants = models.BooleanField("包含下级部门", default=True)
    is_active = models.BooleanField("活动授权", default=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="分配人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="assigned_department_scopes",
    )
    assigned_at = models.DateTimeField("分配时间", default=timezone.now)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="撤销人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_department_scopes",
    )
    revoked_at = models.DateTimeField("撤销时间", null=True, blank=True)

    class Meta:
        verbose_name = "用户部门范围"
        verbose_name_plural = "用户部门范围"
        ordering = ("company_id", "user_id", "department_id", "-assigned_at")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "user", "department"),
                condition=Q(is_active=True),
                name="uq_user_scope_active_root",
            ),
            models.CheckConstraint(
                condition=(
                    Q(is_active=True, revoked_at__isnull=True)
                    | Q(is_active=False, revoked_at__isnull=False)
                ),
                name="ck_user_scope_revocation_state",
            ),
        ]

    def clean(self):
        super().clean()
        if self.department_id and self.department.company_id != self.company_id:
            raise ValidationError({"department": "授权部门必须属于同一公司。"})
        if self.is_active:
            if self.revoked_at is not None or self.revoked_by_id is not None:
                raise ValidationError("活动授权不能包含撤销信息。")
        elif self.revoked_at is None:
            raise ValidationError({"revoked_at": "撤销授权必须记录撤销时间。"})
        if self.user_id:
            employee_companies = set(
                Employee.objects.filter(user_id=self.user_id).values_list(
                    "company_id", flat=True
                )
            )
            if employee_companies and employee_companies != {self.company_id}:
                raise ValidationError({"user": "用户绑定员工与授权公司不一致。"})

    def __str__(self):
        return f"{self.user} - {self.department}"


class Location(NormalizedCodeModel, TimeStampedModel):
    class LocationType(models.TextChoices):
        SITE = "site", "厂区"
        WORKSHOP = "workshop", "车间"
        DEPARTMENT_AREA = "department_area", "部门区域"
        WAREHOUSE = "warehouse", "仓库"
        OFFICE = "office", "办公室"
        POSITION = "position", "具体位置"
        OTHER = "other", "其他"

    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="locations",
    )
    name = models.CharField("位置名称", max_length=200)
    parent = models.ForeignKey(
        "self",
        verbose_name="上级位置",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    level = models.PositiveIntegerField("层级", default=1, editable=False)
    location_type = models.CharField(
        "位置类型", max_length=32, choices=LocationType.choices
    )
    is_active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "位置"
        verbose_name_plural = "位置"
        ordering = ("company_id", "level", "normalized_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_location_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""), name="ck_location_code_nonempty"
            ),
            models.CheckConstraint(
                condition=~Q(id=models.F("parent_id")),
                name="ck_location_not_self_parent",
            ),
            models.CheckConstraint(
                condition=Q(level__gte=1), name="ck_location_level_positive"
            ),
            models.CheckConstraint(
                condition=Q(
                    location_type__in=(
                        "site",
                        "workshop",
                        "department_area",
                        "warehouse",
                        "office",
                        "position",
                        "other",
                    )
                ),
                name="ck_location_type_valid",
            ),
        ]

    def clean(self):
        super().clean()
        _validate_tree_node(self, level_field="level")

    def save(self, *args, **kwargs):
        if self.parent_id:
            self.level = self.parent.level + 1
        else:
            self.level = 1
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "parent" in update_fields:
            kwargs["update_fields"] = {*update_fields, "level"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class FixedAssetCategory(NormalizedCodeModel, TimeStampedModel):
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="fixed_asset_categories",
    )
    name = models.CharField("固定资产类别名称", max_length=200)
    useful_life_months_default = models.PositiveIntegerField("默认使用年限（月）")
    note = models.TextField("备注", blank=True)
    is_active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "固定资产会计类别"
        verbose_name_plural = "固定资产会计类别"
        ordering = ("company_id", "normalized_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_fixed_asset_category_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""),
                name="ck_fixed_asset_category_code_nonempty",
            ),
            models.CheckConstraint(
                condition=Q(useful_life_months_default__gt=0),
                name="ck_fixed_asset_category_life_positive",
            ),
        ]

    def __str__(self):
        return self.name


class AssetCategory(NormalizedCodeModel, TimeStampedModel):
    class CategoryType(models.TextChoices):
        EQUIPMENT = "equipment", "设备"
        MOLD = "mold", "模具"
        TOOL = "tool", "工具"
        INSPECTION_TOOL = "inspection_tool", "检具"
        OFFICE_EQUIPMENT = "office_equipment", "办公设备"
        OTHER = "other", "其他"

    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_categories",
    )
    name = models.CharField("分类名称", max_length=200)
    parent = models.ForeignKey(
        "self",
        verbose_name="上级分类",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="children",
    )
    category_level = models.PositiveIntegerField("分类层级", default=1, editable=False)
    category_type = models.CharField(
        "实物类型", max_length=32, choices=CategoryType.choices
    )
    is_maintenance_required_default = models.BooleanField(
        "默认需要保养", default=False
    )
    default_coding_scheme = models.ForeignKey(
        "AssetCodingScheme",
        verbose_name="默认编码方案",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="default_for_categories",
    )
    default_depreciation_policy = models.ForeignKey(
        "finance.DepreciationPolicy",
        verbose_name="默认折旧政策",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="default_for_asset_categories",
    )
    is_active = models.BooleanField("启用", default=True)

    class Meta:
        verbose_name = "实物分类"
        verbose_name_plural = "实物分类"
        ordering = ("company_id", "category_level", "normalized_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_asset_category_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""), name="ck_category_code_nonempty"
            ),
            models.CheckConstraint(
                condition=~Q(id=models.F("parent_id")),
                name="ck_category_not_self_parent",
            ),
            models.CheckConstraint(
                condition=Q(category_level__gte=1),
                name="ck_category_level_positive",
            ),
            models.CheckConstraint(
                condition=Q(
                    category_type__in=(
                        "equipment",
                        "mold",
                        "tool",
                        "inspection_tool",
                        "office_equipment",
                        "other",
                    )
                ),
                name="ck_category_type_valid",
            ),
        ]

    def clean(self):
        super().clean()
        _validate_tree_node(self, level_field="category_level")
        scheme = self.default_coding_scheme
        if scheme is not None:
            today = timezone.localdate()
            if scheme.company_id != self.company_id:
                raise ValidationError(
                    {"default_coding_scheme": "默认编码方案必须属于同一公司。"}
                )
            if (
                scheme.status != AssetCodingScheme.Status.ACTIVE
                or scheme.effective_from is None
                or scheme.effective_from > today
                or (scheme.effective_to is not None and scheme.effective_to < today)
            ):
                raise ValidationError(
                    {"default_coding_scheme": "默认编码方案必须为当前生效版本。"}
                )
        policy = self.default_depreciation_policy
        if policy is not None:
            today = timezone.localdate()
            if policy.company_id != self.company_id:
                raise ValidationError(
                    {"default_depreciation_policy": "默认折旧政策必须属于同一公司。"}
                )
            if (
                policy.status != "active"
                or policy.effective_from is None
                or policy.effective_from > today
                or (policy.effective_to is not None and policy.effective_to < today)
            ):
                raise ValidationError(
                    {"default_depreciation_policy": "默认折旧政策必须为当前生效版本。"}
                )

    def save(self, *args, **kwargs):
        if self.parent_id:
            self.category_level = self.parent.category_level + 1
        else:
            self.category_level = 1
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "parent" in update_fields:
            kwargs["update_fields"] = {*update_fields, "category_level"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class AssetCodingScheme(TimeStampedModel):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        ACTIVE = "active", "有效版本"
        RETIRED = "retired", "历史版本"

    class ResetMode(models.TextChoices):
        NEVER = "never", "永不重置"
        YEARLY = "yearly", "按年重置"
        MONTHLY = "monthly", "按月重置"
        CATEGORY_YEARLY = "category_yearly", "按分类和年重置"
        CATEGORY_MONTHLY = "category_monthly", "按分类和月重置"

    class CategoryScopeLevel(models.TextChoices):
        MAJOR = "major", "大类"
        MINOR = "minor", "小类"
        LEAF = "leaf", "叶级分类"

    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_coding_schemes",
    )
    name = models.CharField("方案名称", max_length=200)
    scheme_key = models.CharField("方案稳定键", max_length=100)
    version = models.PositiveIntegerField("版本")
    description = models.TextField("说明", blank=True)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    is_default = models.BooleanField("公司默认方案", default=False)
    reset_mode = models.CharField(
        "重置模式", max_length=32, choices=ResetMode.choices
    )
    sequence_start = models.BigIntegerField("流水起始值", default=1)
    category_scope_level = models.CharField(
        "分类作用域层级",
        max_length=16,
        choices=CategoryScopeLevel.choices,
        null=True,
        blank=True,
    )
    effective_from = models.DateField("生效开始日", null=True, blank=True)
    effective_to = models.DateField("生效结束日", null=True, blank=True)
    previous_version = models.ForeignKey(
        "self",
        verbose_name="上一版本",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="next_versions",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_asset_coding_schemes",
    )

    class Meta:
        verbose_name = "资产编码方案"
        verbose_name_plural = "资产编码方案"
        ordering = ("company_id", "scheme_key", "-version")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "scheme_key", "version"),
                name="uq_coding_scheme_company_key_version",
            ),
            models.UniqueConstraint(
                fields=("company",),
                condition=Q(status="active", is_default=True),
                name="uq_coding_scheme_active_default",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="ck_coding_scheme_version_positive"
            ),
            models.CheckConstraint(
                condition=Q(sequence_start__gte=0),
                name="ck_coding_scheme_sequence_start",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("draft", "active", "retired")),
                name="ck_coding_scheme_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    reset_mode__in=(
                        "never",
                        "yearly",
                        "monthly",
                        "category_yearly",
                        "category_monthly",
                    )
                ),
                name="ck_coding_scheme_reset_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        reset_mode__in=("category_yearly", "category_monthly"),
                        category_scope_level__in=("major", "minor", "leaf"),
                    )
                    | Q(
                        reset_mode__in=("never", "yearly", "monthly"),
                        category_scope_level__isnull=True,
                    )
                ),
                name="ck_coding_scheme_category_scope",
            ),
            models.CheckConstraint(
                condition=Q(effective_to__isnull=True)
                | Q(effective_from__isnull=False, effective_to__gte=models.F("effective_from")),
                name="ck_coding_scheme_effective_dates",
            ),
            models.CheckConstraint(
                condition=~Q(status="active") | Q(effective_from__isnull=False),
                name="ck_coding_scheme_active_from",
            ),
            models.CheckConstraint(
                condition=Q(is_default=False) | Q(status="active"),
                name="ck_coding_scheme_default_active",
            ),
            models.CheckConstraint(
                condition=~Q(id=models.F("previous_version_id")),
                name="ck_coding_scheme_not_self_previous",
            ),
        ]

    def clean(self):
        super().clean()
        if self.previous_version_id:
            previous = self.previous_version
            if previous.company_id != self.company_id:
                raise ValidationError({"previous_version": "上一版本必须属于同一公司。"})
            if previous.scheme_key != self.scheme_key:
                raise ValidationError({"previous_version": "上一版本必须使用相同方案稳定键。"})
            if previous.version >= self.version:
                raise ValidationError({"previous_version": "上一版本号必须小于当前版本。"})
        category_modes = {
            self.ResetMode.CATEGORY_YEARLY,
            self.ResetMode.CATEGORY_MONTHLY,
        }
        ordinary_modes = {
            self.ResetMode.NEVER,
            self.ResetMode.YEARLY,
            self.ResetMode.MONTHLY,
        }
        if self.reset_mode in category_modes and self.category_scope_level not in {
            self.CategoryScopeLevel.MAJOR,
            self.CategoryScopeLevel.MINOR,
            self.CategoryScopeLevel.LEAF,
        }:
            raise ValidationError(
                {
                    "category_scope_level": (
                        "按分类重置时必须选择大类、小类或叶级分类。"
                    )
                }
            )
        if self.reset_mode in ordinary_modes and self.category_scope_level is not None:
            raise ValidationError(
                {"category_scope_level": "非分类重置模式不能设置分类作用域层级。"}
            )
        if self.sequence_start is not None and self.sequence_start < 0:
            raise ValidationError({"sequence_start": "首个可签发流水值不得为负数。"})
        if self.effective_to and not self.effective_from:
            raise ValidationError(
                {"effective_from": "填写生效结束日时必须同时填写开始日。"}
            )
        if (
            self.effective_from
            and self.effective_to
            and self.effective_to < self.effective_from
        ):
            raise ValidationError({"effective_to": "生效结束日不得早于开始日。"})

    def __str__(self):
        return f"{self.name} v{self.version}"


class AssetCodingSegment(models.Model):
    class SegmentType(models.TextChoices):
        FIXED_TEXT = "fixed_text", "固定文本"
        COMPANY_CODE = "company_code", "公司编码"
        MAJOR_CATEGORY_CODE = "major_category_code", "大类编码"
        MINOR_CATEGORY_CODE = "minor_category_code", "小类编码"
        CATEGORY_CODE = "category_code", "分类编码"
        DEPARTMENT_CODE = "department_code", "部门编码"
        YEAR = "year", "年"
        YEAR_MONTH = "year_month", "年月"
        FULL_DATE = "full_date", "完整日期"
        SEQUENCE = "sequence", "顺序号"
        CUSTOM_TEXT = "custom_text", "自定义固定文本"
        SEPARATOR = "separator", "分隔符"

    coding_scheme = models.ForeignKey(
        AssetCodingScheme,
        verbose_name="编码方案",
        on_delete=models.CASCADE,
        related_name="segments",
    )
    sequence_order = models.PositiveIntegerField("片段顺序")
    segment_type = models.CharField(
        "片段类型", max_length=32, choices=SegmentType.choices
    )
    fixed_value = models.CharField("固定值", max_length=64, null=True, blank=True)
    format_string = models.CharField(
        "格式字符串", max_length=64, null=True, blank=True, editable=False
    )
    sequence_length = models.PositiveSmallIntegerField(
        "流水位数", null=True, blank=True
    )
    zero_pad = models.BooleanField("左侧补零", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "资产编码片段"
        verbose_name_plural = "资产编码片段"
        ordering = ("coding_scheme_id", "sequence_order")
        constraints = [
            models.UniqueConstraint(
                fields=("coding_scheme", "sequence_order"),
                name="uq_coding_segment_scheme_order",
            ),
            models.UniqueConstraint(
                fields=("coding_scheme",),
                condition=Q(segment_type="sequence"),
                name="uq_coding_segment_one_sequence",
            ),
            models.CheckConstraint(
                condition=Q(sequence_order__gte=1),
                name="ck_coding_segment_order_positive",
            ),
            models.CheckConstraint(
                condition=Q(
                    segment_type__in=(
                        "fixed_text",
                        "company_code",
                        "major_category_code",
                        "minor_category_code",
                        "category_code",
                        "department_code",
                        "year",
                        "year_month",
                        "full_date",
                        "sequence",
                        "custom_text",
                        "separator",
                    )
                ),
                name="ck_coding_segment_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(format_string__isnull=True),
                name="ck_coding_segment_format_null",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        segment_type__in=("fixed_text", "custom_text"),
                        fixed_value__isnull=False,
                        fixed_value__regex=r"^[^\s\x00-\x1F\x7F-\x9F{}](?:[^\x00-\x1F\x7F-\x9F{}]*[^\s\x00-\x1F\x7F-\x9F{}])?$",
                        sequence_length__isnull=True,
                        zero_pad__isnull=True,
                    )
                    | Q(
                        segment_type="separator",
                        fixed_value__in=("-", "_", ".", "/"),
                        sequence_length__isnull=True,
                        zero_pad__isnull=True,
                    )
                    | Q(
                        segment_type="sequence",
                        fixed_value__isnull=True,
                        sequence_length__isnull=False,
                        sequence_length__gte=1,
                        sequence_length__lte=12,
                        zero_pad__isnull=False,
                    )
                    | Q(
                        segment_type__in=(
                            "company_code",
                            "major_category_code",
                            "minor_category_code",
                            "category_code",
                            "department_code",
                            "year",
                            "year_month",
                            "full_date",
                        ),
                        fixed_value__isnull=True,
                        sequence_length__isnull=True,
                        zero_pad__isnull=True,
                    )
                ),
                name="ck_coding_segment_field_matrix",
            ),
        ]

    def clean(self):
        super().clean()
        if self.format_string is not None:
            raise ValidationError({"format_string": "V1 不允许配置格式字符串。"})
        if self.segment_type not in self.SegmentType.values:
            # Field/choice validation owns missing or unsupported tokens.  Do
            # not add a second model-level message to the same form control.
            return
        fixed_types = {self.SegmentType.FIXED_TEXT, self.SegmentType.CUSTOM_TEXT}
        source_types = {
            self.SegmentType.COMPANY_CODE,
            self.SegmentType.MAJOR_CATEGORY_CODE,
            self.SegmentType.MINOR_CATEGORY_CODE,
            self.SegmentType.CATEGORY_CODE,
            self.SegmentType.DEPARTMENT_CODE,
            self.SegmentType.YEAR,
            self.SegmentType.YEAR_MONTH,
            self.SegmentType.FULL_DATE,
        }
        if self.segment_type in fixed_types:
            value = self.fixed_value
            if not value:
                raise ValidationError({"fixed_value": "固定文本不能为空。"})
            if value != value.strip():
                raise ValidationError({"fixed_value": "固定文本不能包含首尾空白。"})
            if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
                raise ValidationError({"fixed_value": "固定文本不能包含控制字符。"})
            if "{" in value or "}" in value:
                raise ValidationError({"fixed_value": "固定文本不能包含花括号。"})
            if self.sequence_length is not None or self.zero_pad is not None:
                raise ValidationError("固定文本片段包含了多余字段。")
        elif self.segment_type == self.SegmentType.SEPARATOR:
            if self.fixed_value not in {"-", "_", ".", "/"}:
                raise ValidationError({"fixed_value": "分隔符只能是 -、_、. 或 /。"})
            if self.sequence_length is not None or self.zero_pad is not None:
                raise ValidationError("分隔符片段包含了多余字段。")
        elif self.segment_type == self.SegmentType.SEQUENCE:
            if self.fixed_value is not None:
                raise ValidationError({"fixed_value": "顺序号片段不能配置固定值。"})
            if self.sequence_length is None or not 1 <= self.sequence_length <= 12:
                raise ValidationError({"sequence_length": "流水位数必须为 1–12。"})
            if self.zero_pad is None:
                raise ValidationError({"zero_pad": "顺序号片段必须明确是否补零。"})
        elif self.segment_type in source_types:
            if (
                self.fixed_value is not None
                or self.sequence_length is not None
                or self.zero_pad is not None
            ):
                raise ValidationError("来源片段包含了多余字段。")
        else:
            raise ValidationError({"segment_type": "不支持的片段类型。"})

    def __str__(self):
        return f"{self.coding_scheme} #{self.sequence_order}"


class SequenceCounter(TimeStampedModel):
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="sequence_counters",
    )
    coding_scheme = models.ForeignKey(
        AssetCodingScheme,
        verbose_name="编码方案",
        on_delete=models.PROTECT,
        related_name="sequence_counters",
    )
    scope_key = models.CharField("作用域键", max_length=512)
    current_value = models.BigIntegerField("当前值")

    class Meta:
        verbose_name = "编码流水计数器"
        verbose_name_plural = "编码流水计数器"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "coding_scheme", "scope_key"),
                name="uq_sequence_counter_scope",
            ),
            models.CheckConstraint(
                condition=Q(current_value__gte=-1), name="ck_sequence_counter_minimum"
            ),
        ]


class IssuedCode(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "当前有效"
        REPLACED = "replaced", "已替换"
        VOIDED = "voided", "已作废"

    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="issued_codes",
    )
    coding_scheme = models.ForeignKey(
        AssetCodingScheme,
        verbose_name="编码方案",
        on_delete=models.PROTECT,
        related_name="issued_codes",
    )
    scope_key = models.CharField("作用域键", max_length=512)
    sequence_value = models.BigIntegerField("流水值")
    display_code = models.CharField("显示编号", max_length=64)
    normalized_code = models.CharField("规范化编号", max_length=64)
    effective_date = models.DateField("编号生效日期")
    effective_date_reason = models.TextField("历史回填原因", blank=True)
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    idempotency_key = models.CharField("幂等键", max_length=255)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="签发人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="issued_asset_codes",
    )
    issued_at = models.DateTimeField("签发时间", default=timezone.now)
    replaced_or_voided_reason = models.TextField("替换或作废原因", blank=True)
    replaced_or_voided_at = models.DateTimeField(
        "替换或作废时间", null=True, blank=True
    )

    class Meta:
        verbose_name = "已发资产编号"
        verbose_name_plural = "已发资产编号"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_issued_code_company_normalized",
            ),
            models.UniqueConstraint(
                fields=("company", "coding_scheme", "scope_key", "sequence_value"),
                name="uq_issued_code_scope_sequence",
            ),
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_issued_code_company_idem",
            ),
            models.CheckConstraint(
                condition=Q(sequence_value__gte=0),
                name="ck_issued_code_sequence_nonnegative",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("active", "replaced", "voided")),
                name="ck_issued_code_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="active",
                        replaced_or_voided_reason="",
                        replaced_or_voided_at__isnull=True,
                    )
                    | Q(
                        status__in=("replaced", "voided"),
                        replaced_or_voided_reason__gt="",
                        replaced_or_voided_at__isnull=False,
                    )
                ),
                name="ck_issued_code_status_fields",
            ),
            models.CheckConstraint(
                condition=~Q(display_code="") & ~Q(normalized_code="") & ~Q(idempotency_key=""),
                name="ck_issued_code_values_nonempty",
            ),
        ]

    def delete(self, *args, **kwargs):
        raise ValidationError("已发编号永久占号，不允许删除。")


class SystemSetting(models.Model):
    class ValueType(models.TextChoices):
        INTEGER = "integer", "整数"
        DECIMAL = "decimal", "小数"
        STRING_LIST = "string_list", "字符串列表"

    REGISTRY_TYPES = {
        "attachment_allowed_extensions": ValueType.STRING_LIST,
        "attachment_max_size_bytes": ValueType.INTEGER,
        "fixed_asset_warning_amount": ValueType.DECIMAL,
    }

    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="system_settings",
    )
    key = models.CharField("设置键", max_length=100)
    value = models.TextField("设置值")
    value_type = models.CharField(
        "值类型", max_length=16, choices=ValueType.choices
    )
    description = models.CharField("说明", max_length=255)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="更新人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_system_settings",
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "系统设置"
        verbose_name_plural = "系统设置"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "key"), name="uq_system_setting_company_key"
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        key="attachment_allowed_extensions",
                        value_type="string_list",
                    )
                    | Q(key="attachment_max_size_bytes", value_type="integer")
                    | Q(key="fixed_asset_warning_amount", value_type="decimal")
                ),
                name="ck_system_setting_registry_type",
            ),
            models.CheckConstraint(
                condition=Q(value_type__in=("integer", "decimal", "string_list")),
                name="ck_system_setting_value_type_valid",
            ),
        ]

    def clean(self):
        super().clean()
        expected_type = self.REGISTRY_TYPES.get(self.key)
        if expected_type is None:
            raise ValidationError({"key": "未知或禁止的系统设置键。"})
        if self.value_type != expected_type:
            raise ValidationError({"value_type": "值类型与固定 registry 不一致。"})
        if self.key == "attachment_max_size_bytes":
            try:
                parsed = int(self.value)
            except (TypeError, ValueError) as exc:
                raise ValidationError({"value": "附件上限必须是整数。"}) from exc
            if str(parsed) != str(self.value).strip() or not 1 <= parsed <= 20 * 1024 * 1024:
                raise ValidationError(
                    {"value": "附件上限必须是 1 至 20971520 的规范整数。"}
                )
        elif self.key == "fixed_asset_warning_amount":
            try:
                parsed = Decimal(self.value)
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise ValidationError({"value": "提示阈值必须是 Decimal。"}) from exc
            if not parsed.is_finite() or parsed < 0:
                raise ValidationError({"value": "提示阈值必须是不小于 0 的数值。"})
        else:
            import json

            try:
                parsed = json.loads(self.value)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValidationError({"value": "扩展名白名单必须是 JSON 数组。"}) from exc
            allowed = {"jpg", "jpeg", "png", "webp", "pdf", "xlsx", "docx"}
            if (
                not isinstance(parsed, list)
                or not parsed
                or any(not isinstance(item, str) for item in parsed)
                or len(set(parsed)) != len(parsed)
                or any(item not in allowed or item != item.lower() for item in parsed)
            ):
                raise ValidationError(
                    {"value": "扩展名须为非空、去重且只含批准小写值的 JSON 数组。"}
                )

    def __str__(self):
        return self.key


class InitializationSetting(models.Model):
    company = models.OneToOneField(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="initialization_setting",
    )
    initialization_completed = models.BooleanField("初始化完成", default=False)
    company_configured = models.BooleanField("公司已配置", default=False)
    departments_configured = models.BooleanField("部门已配置", default=False)
    employees_configured = models.BooleanField("人员已配置", default=False)
    categories_configured = models.BooleanField("实物分类已配置", default=False)
    locations_configured = models.BooleanField("位置已配置", default=False)
    coding_scheme_configured = models.BooleanField("编码规则已配置", default=False)
    finance_rules_configured = models.BooleanField("财务规则已配置", default=False)
    permissions_configured = models.BooleanField("权限已配置", default=False)
    users_configured = models.BooleanField("用户已配置", default=False)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="完成人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_initializations",
    )
    completed_at = models.DateTimeField("完成时间", null=True, blank=True)

    class Meta:
        verbose_name = "初始化设置"
        verbose_name_plural = "初始化设置"
        constraints = [
            models.CheckConstraint(
                condition=(
                    Q(
                        initialization_completed=True,
                        company_configured=True,
                        departments_configured=True,
                        employees_configured=True,
                        categories_configured=True,
                        locations_configured=True,
                        coding_scheme_configured=True,
                        finance_rules_configured=True,
                        permissions_configured=True,
                        users_configured=True,
                        completed_by__isnull=False,
                        completed_at__isnull=False,
                    )
                    | Q(
                        initialization_completed=False,
                        completed_by__isnull=True,
                        completed_at__isnull=True,
                    )
                ),
                name="ck_initialization_completion_state",
            )
        ]

    def __str__(self):
        return f"{self.company} 初始化进度"


class Attachment(models.Model):
    class MalwareScanStatus(models.TextChoices):
        PENDING = "pending", "待校验"
        POLICY_LIMITED = "policy_limited", "策略受限校验通过"
        CLEAN = "clean", "恶意软件扫描通过"
        REJECTED = "rejected", "已拒绝"

    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="attachments",
    )
    storage_key = models.CharField("存储键", max_length=512)
    original_filename = models.CharField("原文件名", max_length=255)
    safe_filename = models.CharField("安全文件名", max_length=255)
    file_size = models.PositiveBigIntegerField(
        "文件大小", validators=[MinValueValidator(1)]
    )
    mime_type = models.CharField("MIME 类型", max_length=127)
    sha256 = models.CharField("SHA-256", max_length=64, validators=[HEX64_VALIDATOR])
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="上传人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_attachments",
    )
    uploaded_at = models.DateTimeField("上传时间", auto_now_add=True)
    orphaned_at = models.DateTimeField(
        "进入孤儿候选时间", null=True, blank=True
    )
    malware_scan_status = models.CharField(
        "安全扫描状态",
        max_length=24,
        choices=MalwareScanStatus.choices,
        default=MalwareScanStatus.PENDING,
    )
    is_available = models.BooleanField("可提供", default=False)

    class Meta:
        verbose_name = "附件"
        verbose_name_plural = "附件"
        constraints = [
            models.UniqueConstraint(
                fields=("company", "storage_key"),
                name="uq_attachment_company_storage",
            ),
            models.CheckConstraint(
                condition=Q(file_size__gt=0), name="ck_attachment_size_positive"
            ),
            models.CheckConstraint(
                condition=Q(
                    malware_scan_status__in=(
                        "pending",
                        "policy_limited",
                        "clean",
                        "rejected",
                    )
                ),
                name="ck_attachment_scan_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(orphaned_at__isnull=True) | Q(is_available=False),
                name="ck_attachment_orphan_unavailable",
            ),
        ]

    def clean(self):
        super().clean()
        self.sha256 = (self.sha256 or "").strip().lower()
        if not self.storage_key or self.storage_key.strip() != self.storage_key:
            raise ValidationError({"storage_key": "存储键不能为空或包含首尾空格。"})
        path_parts = self.storage_key.replace("\\", "/").split("/")
        if self.storage_key.startswith(("/", "\\")) or ".." in path_parts:
            raise ValidationError({"storage_key": "存储键必须是服务端生成的安全相对键。"})
        if self.orphaned_at is not None and self.is_available:
            raise ValidationError({"is_available": "孤儿候选附件不能标记为可用。"})

    def save(self, *args, **kwargs):
        self.sha256 = (self.sha256 or "").strip().lower()
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.original_filename


class ImportBatchQuerySet(models.QuerySet):
    def delete(self):
        raise ValidationError("导入批次只能通过受控清理 Service 删除。")


class ImportBatch(models.Model):
    class ImportType(models.TextChoices):
        DEPARTMENT = "department", "部门"
        EMPLOYEE = "employee", "人员"
        ASSET_INITIALIZATION = "asset_initialization", "资产初始化"
        ITEM_MASTER = "item_master", "低值物品档案"
        OPENING_STOCK = "opening_stock", "低值物品期初库存"
        OPENING_CUSTODY = "opening_custody", "耐用品期初保管"

    class Status(models.TextChoices):
        UPLOADED = "uploaded", "已上传"
        VALIDATED = "validated", "校验通过"
        INVALID = "invalid", "校验不通过"
        CONFIRMED = "confirmed", "已确认"
        FAILED = "failed", "处理失败"
        CANCELLED = "cancelled", "已取消"

    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="import_batches",
    )
    import_type = models.CharField(
        "导入类型", max_length=32, choices=ImportType.choices
    )
    template_version = models.CharField("模板版本", max_length=32)
    file_attachment = models.ForeignKey(
        Attachment,
        verbose_name="源文件附件",
        on_delete=models.PROTECT,
        related_name="import_batches",
    )
    file_sha256 = models.CharField(
        "文件 SHA-256", max_length=64, validators=[HEX64_VALIDATOR]
    )
    status = models.CharField(
        "批次状态", max_length=16, choices=Status.choices, default=Status.UPLOADED
    )
    total_rows = models.PositiveIntegerField("总行数", null=True, blank=True)
    valid_rows = models.PositiveIntegerField("有效行数", null=True, blank=True)
    error_rows = models.PositiveIntegerField("错误行数", null=True, blank=True)
    warning_rows = models.PositiveIntegerField("警告行数", null=True, blank=True)
    request_hash = models.CharField("请求摘要", max_length=64, validators=[HEX64_VALIDATOR])
    idempotency_key = models.CharField("幂等键", max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="上传人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="uploaded_import_batches",
    )
    uploaded_at = models.DateTimeField("上传时间", auto_now_add=True)
    validated_at = models.DateTimeField("校验时间", null=True, blank=True)
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="确认人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_import_batches",
    )
    confirmed_at = models.DateTimeField("确认时间", null=True, blank=True)

    objects = ImportBatchQuerySet.as_manager()

    class Meta:
        verbose_name = "导入批次"
        verbose_name_plural = "导入批次"
        ordering = ("-uploaded_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_import_batch_company_idem",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        total_rows__isnull=True,
                        valid_rows__isnull=True,
                        error_rows__isnull=True,
                        warning_rows__isnull=True,
                    )
                    | Q(
                        total_rows__isnull=False,
                        valid_rows__isnull=False,
                        error_rows__isnull=False,
                        warning_rows__isnull=False,
                        total_rows=models.F("valid_rows") + models.F("error_rows"),
                        warning_rows__lte=models.F("total_rows"),
                    )
                ),
                name="ck_import_batch_counts",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="uploaded",
                        validated_at__isnull=True,
                        confirmed_by__isnull=True,
                        confirmed_at__isnull=True,
                    )
                    | Q(
                        status="validated",
                        validated_at__isnull=False,
                        error_rows=0,
                        confirmed_by__isnull=True,
                        confirmed_at__isnull=True,
                    )
                    | Q(
                        status="invalid",
                        validated_at__isnull=False,
                        error_rows__gt=0,
                        confirmed_by__isnull=True,
                        confirmed_at__isnull=True,
                    )
                    | Q(
                        status="confirmed",
                        validated_at__isnull=False,
                        error_rows=0,
                        confirmed_by__isnull=False,
                        confirmed_at__isnull=False,
                    )
                    | Q(status="failed", confirmed_by__isnull=True, confirmed_at__isnull=True)
                    | Q(status="cancelled", confirmed_by__isnull=True, confirmed_at__isnull=True)
                ),
                name="ck_import_batch_status_fields",
            ),
            models.CheckConstraint(
                condition=Q(
                    import_type__in=(
                        "department",
                        "employee",
                        "asset_initialization",
                        "item_master",
                        "opening_stock",
                        "opening_custody",
                    )
                ),
                name="ck_import_batch_type_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=(
                        "uploaded",
                        "validated",
                        "invalid",
                        "confirmed",
                        "failed",
                        "cancelled",
                    )
                ),
                name="ck_import_batch_status_valid",
            ),
        ]

    def clean(self):
        super().clean()
        self.file_sha256 = (self.file_sha256 or "").strip().lower()
        self.request_hash = (self.request_hash or "").strip().lower()
        if self.file_attachment_id:
            if self.file_attachment.company_id != self.company_id:
                raise ValidationError(
                    {"file_attachment": "源文件附件必须属于同一公司。"}
                )
            if self.file_sha256 != self.file_attachment.sha256:
                raise ValidationError(
                    {"file_sha256": "文件摘要与源文件附件不一致。"}
                )

    def save(self, *args, **kwargs):
        self.file_sha256 = (self.file_sha256 or "").strip().lower()
        self.request_hash = (self.request_hash or "").strip().lower()
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("导入批次只能通过受控清理 Service 删除。")

    def __str__(self):
        return f"{self.get_import_type_display()}导入 {self.pk}"


class ImportRow(models.Model):
    class ValidationStatus(models.TextChoices):
        PENDING = "pending", "待校验"
        VALID = "valid", "有效"
        INVALID = "invalid", "无效"
        CREATED = "created", "已创建"

    batch = models.ForeignKey(
        ImportBatch,
        verbose_name="导入批次",
        on_delete=models.CASCADE,
        related_name="rows",
    )
    row_number = models.PositiveIntegerField("行号")
    raw_data_json = models.JSONField("原始数据", default=dict, blank=True)
    normalized_data_json = models.JSONField("规范化数据", default=dict, blank=True)
    validation_status = models.CharField(
        "校验状态",
        max_length=16,
        choices=ValidationStatus.choices,
        default=ValidationStatus.PENDING,
    )
    errors_json = models.JSONField("错误", default=list, blank=True)
    warnings_json = models.JSONField("警告", default=list, blank=True)
    created_object_type = models.CharField("创建对象类型", max_length=100, blank=True)
    created_object_id = models.CharField("创建对象标识", max_length=255, blank=True)

    class Meta:
        verbose_name = "导入暂存行"
        verbose_name_plural = "导入暂存行"
        ordering = ("batch_id", "row_number")
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "row_number"), name="uq_import_row_batch_number"
            ),
            models.CheckConstraint(
                condition=Q(row_number__gte=1), name="ck_import_row_number_positive"
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        validation_status="created",
                        created_object_type__gt="",
                        created_object_id__gt="",
                    )
                    | Q(
                        validation_status__in=("pending", "valid", "invalid"),
                        created_object_type="",
                        created_object_id="",
                    )
                ),
                name="ck_import_row_created_mapping",
            ),
            models.CheckConstraint(
                condition=Q(
                    validation_status__in=("pending", "valid", "invalid", "created")
                ),
                name="ck_import_row_validation_status_valid",
            ),
        ]

    def clean(self):
        super().clean()
        if not isinstance(self.errors_json, list):
            raise ValidationError({"errors_json": "错误必须是结构化数组。"})
        if not isinstance(self.warnings_json, list):
            raise ValidationError({"warnings_json": "警告必须是结构化数组。"})
        if self.validation_status == self.ValidationStatus.INVALID and not self.errors_json:
            raise ValidationError({"errors_json": "无效行必须包含至少一项错误。"})
        if self.validation_status == self.ValidationStatus.CREATED:
            if self.batch.status != ImportBatch.Status.CONFIRMED:
                raise ValidationError("只有已确认批次的行可以标记为已创建。")
            if not self.created_object_type or not self.created_object_id:
                raise ValidationError("已创建行必须记录对象类型和对象标识。")

    def __str__(self):
        return f"批次 {self.batch_id} 第 {self.row_number} 行"
