import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinLengthValidator
from django.db import models
from django.db.models import Q
from django.utils import timezone

from apps.masterdata.models import (
    AssetCategory,
    AssetCodingScheme,
    Attachment,
    Company,
    Department,
    Employee,
    IssuedCode,
    Location,
)
from apps.masterdata.normalization import (
    clean_display_identifier,
    normalize_identifier,
)


class AssetQuerySet(models.QuerySet):
    def update(self, **kwargs):
        protected = {
            "asset_status",
            "record_status",
            "asset_code",
            "current_issued_code",
            "current_issued_code_id",
            "requested_coding_scheme",
            "requested_coding_scheme_id",
            "submitted_at",
            "department",
            "department_id",
            "responsible_employee",
            "responsible_employee_id",
            "location",
            "location_id",
        }.intersection(kwargs)
        submitted_actor_keys = {"submitted_by", "submitted_by_id"}.intersection(kwargs)
        if submitted_actor_keys and any(kwargs[key] is not None for key in submitted_actor_keys):
            protected.update(submitted_actor_keys)
        if protected:
            raise ValidationError("资产状态、编号和提交元数据只能通过受控 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("资产只能通过受控 Service 物理删除未提交草稿。")


class Asset(models.Model):
    class AssetStatus(models.TextChoices):
        DRAFT = "draft", "草稿"
        PENDING_FINANCE = "pending_finance", "待财务确认"
        PENDING_LABEL = "pending_label", "待贴标"
        IN_USE = "in_use", "在用"
        IDLE = "idle", "闲置"
        LOANED = "loaned", "借出"
        UNDER_REPAIR = "under_repair", "维修中"
        PENDING_DISPOSAL = "pending_disposal", "处置处理中"
        DISPOSED = "disposed", "已报废"
        SOLD = "sold", "已出售"
        OTHER_DISPOSED = "other_disposed", "已其他处置"

    class RecordStatus(models.TextChoices):
        ACTIVE = "active", "有效"
        ARCHIVED = "archived", "已归档"

    class TrackingMode(models.TextChoices):
        SINGLE_ITEM = "single_item", "单件追踪"

    class InitializationSource(models.TextChoices):
        MANUAL = "manual", "手工录入"
        EXCEL_IMPORT = "excel_import", "受控 Excel 导入"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="assets",
    )
    asset_code = models.CharField(
        "正式资产编号", max_length=64, null=True, blank=True, editable=False
    )
    current_issued_code = models.OneToOneField(
        IssuedCode,
        verbose_name="当前正式编号登记",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="current_asset",
    )
    requested_coding_scheme = models.ForeignKey(
        AssetCodingScheme,
        verbose_name="指定编码方案版本",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="requested_by_assets",
    )
    asset_status = models.CharField(
        "资产状态",
        max_length=32,
        choices=AssetStatus.choices,
        default=AssetStatus.DRAFT,
    )
    record_status = models.CharField(
        "记录状态",
        max_length=16,
        choices=RecordStatus.choices,
        default=RecordStatus.ACTIVE,
    )
    asset_name = models.CharField("资产名称", max_length=200)
    category = models.ForeignKey(
        AssetCategory,
        verbose_name="实物分类",
        on_delete=models.PROTECT,
        related_name="assets",
    )
    brand = models.CharField("品牌", max_length=100, blank=True)
    model = models.CharField("型号", max_length=100, blank=True)
    manufacturer = models.CharField("厂家", max_length=200, blank=True)
    serial_number = models.CharField("序列号", max_length=200, blank=True)
    factory_number = models.CharField("出厂编号", max_length=200, blank=True)
    historical_code = models.CharField("历史参考编号", max_length=200, blank=True)
    tracking_mode = models.CharField(
        "追踪方式",
        max_length=16,
        choices=TrackingMode.choices,
        default=TrackingMode.SINGLE_ITEM,
        editable=False,
    )
    quantity = models.PositiveSmallIntegerField("数量", default=1, editable=False)
    unit = models.CharField("单位", max_length=32, blank=True)
    description = models.TextField("说明", blank=True)
    department = models.ForeignKey(
        Department,
        verbose_name="当前部门",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    responsible_employee = models.ForeignKey(
        Employee,
        verbose_name="当前责任人",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="responsible_assets",
    )
    location = models.ForeignKey(
        Location,
        verbose_name="当前位置",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="assets",
    )
    acquisition_date = models.DateField("购置日期", null=True, blank=True)
    commissioning_date = models.DateField(
        "达到可使用状态日期", null=True, blank=True
    )
    is_maintenance_required = models.BooleanField("需要保养", default=False)
    initialization_source = models.CharField(
        "初始化来源",
        max_length=32,
        choices=InitializationSource.choices,
        default=InitializationSource.MANUAL,
    )
    initialization_date = models.DateField(
        "初始化日期", default=timezone.localdate
    )
    initialized_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="初始化人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="initialized_assets",
    )
    notes = models.TextField("备注", blank=True)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="提交人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="submitted_assets",
    )
    submitted_at = models.DateTimeField("提交时间", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_assets",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="最后修改人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="updated_assets",
    )
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    objects = AssetQuerySet.as_manager()

    class Meta:
        verbose_name = "资产"
        verbose_name_plural = "资产"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "asset_code"),
                condition=Q(asset_code__isnull=False),
                name="uq_asset_company_code",
            ),
            models.CheckConstraint(
                condition=Q(quantity=1), name="ck_asset_quantity_one"
            ),
            models.CheckConstraint(
                condition=Q(tracking_mode="single_item"),
                name="ck_asset_tracking_single",
            ),
            models.CheckConstraint(
                condition=Q(initialization_source__in=("manual", "excel_import")),
                name="ck_asset_initialization_source",
            ),
            models.CheckConstraint(
                condition=Q(
                    asset_status__in=(
                        "draft",
                        "pending_finance",
                        "pending_label",
                        "in_use",
                        "idle",
                        "loaned",
                        "under_repair",
                        "pending_disposal",
                        "disposed",
                        "sold",
                        "other_disposed",
                    )
                ),
                name="ck_asset_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(record_status__in=("active", "archived")),
                name="ck_asset_record_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        asset_status__in=("draft", "pending_finance"),
                        asset_code__isnull=True,
                        current_issued_code__isnull=True,
                    )
                    | Q(
                        asset_status__in=(
                            "pending_label",
                            "in_use",
                            "idle",
                            "loaned",
                            "under_repair",
                            "pending_disposal",
                            "disposed",
                            "sold",
                            "other_disposed",
                        ),
                        asset_code__isnull=False,
                        current_issued_code__isnull=False,
                    )
                ),
                name="ck_asset_code_status",
            ),
            models.CheckConstraint(
                condition=Q(asset_code__isnull=True) | ~Q(asset_code=""),
                name="ck_asset_code_not_empty",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        asset_status="draft",
                        submitted_by__isnull=True,
                        submitted_at__isnull=True,
                    )
                    | Q(
                        asset_status__in=(
                            "pending_finance",
                            "pending_label",
                            "in_use",
                            "idle",
                            "loaned",
                            "under_repair",
                            "pending_disposal",
                            "disposed",
                            "sold",
                            "other_disposed",
                        ),
                        submitted_at__isnull=False,
                    )
                ),
                name="ck_asset_submission_metadata",
            ),
            models.CheckConstraint(
                condition=(
                    Q(record_status="active")
                    | Q(
                        asset_status__in=("disposed", "sold", "other_disposed"),
                        record_status="archived",
                    )
                ),
                name="ck_asset_archive_terminal_only",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(
                        asset_status__in=(
                            "in_use",
                            "idle",
                            "loaned",
                            "under_repair",
                            "pending_disposal",
                        )
                    )
                    | Q(
                        department__isnull=False,
                        responsible_employee__isnull=False,
                        location__isnull=False,
                    )
                ),
                name="ck_asset_operational_responsibility",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_code == "":
            errors["asset_code"] = "未发号资产必须保存 NULL，不能使用空字符串。"
        if self.category_id:
            if self.category.company_id != self.company_id:
                errors["category"] = "实物分类必须属于同一公司。"
            elif not self.category.is_active:
                errors["category"] = "实物分类必须处于启用状态。"
        if self.department_id:
            if self.department.company_id != self.company_id:
                errors["department"] = "部门必须属于同一公司。"
            elif not self.department.is_active:
                errors["department"] = "部门必须处于启用状态。"
        if self.responsible_employee_id:
            employee = self.responsible_employee
            if employee.company_id != self.company_id:
                errors["responsible_employee"] = "责任人必须属于同一公司。"
            elif self.department_id and employee.department_id != self.department_id:
                errors["responsible_employee"] = "责任人必须属于资产当前部门。"
            elif (
                employee.employment_status != Employee.EmploymentStatus.ACTIVE
                or not employee.is_active
            ):
                errors["responsible_employee"] = "责任人必须是在职且启用的员工。"
        if self.location_id:
            if self.location.company_id != self.company_id:
                errors["location"] = "位置必须属于同一公司。"
            elif not self.location.is_active:
                errors["location"] = "位置必须处于启用状态。"
        if self.requested_coding_scheme_id:
            scheme = self.requested_coding_scheme
            today = timezone.localdate()
            if scheme.company_id != self.company_id:
                errors["requested_coding_scheme"] = "编码方案必须属于同一公司。"
            elif (
                scheme.status != AssetCodingScheme.Status.ACTIVE
                or scheme.effective_from is None
                or scheme.effective_from > today
                or (scheme.effective_to is not None and scheme.effective_to < today)
            ):
                errors["requested_coding_scheme"] = "编码方案必须是当前生效版本。"
        if self.current_issued_code_id:
            issued = self.current_issued_code
            if issued.company_id != self.company_id:
                errors["current_issued_code"] = "正式编号登记必须属于同一公司。"
            elif self.asset_code != issued.display_code:
                errors["asset_code"] = "正式编号必须与当前编号登记一致。"
        if self.asset_status in {
            self.AssetStatus.DRAFT,
            self.AssetStatus.PENDING_FINANCE,
        } and (self.asset_code is not None or self.current_issued_code_id is not None):
            errors["asset_code"] = "草稿和待财务确认资产不得包含正式编号。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "asset_status",
                "record_status",
                "asset_code",
                "current_issued_code_id",
                "requested_coding_scheme_id",
                "submitted_by_id",
                "submitted_at",
                "department_id",
                "responsible_employee_id",
                "location_id",
            ).first()
            if previous is not None:
                current = {
                    "asset_status": self.asset_status,
                    "record_status": self.record_status,
                    "asset_code": self.asset_code,
                    "current_issued_code_id": self.current_issued_code_id,
                    "requested_coding_scheme_id": self.requested_coding_scheme_id,
                    "submitted_by_id": self.submitted_by_id,
                    "submitted_at": self.submitted_at,
                    "department_id": self.department_id,
                    "responsible_employee_id": self.responsible_employee_id,
                    "location_id": self.location_id,
                }
                if previous != current:
                    raise ValidationError(
                        "资产状态、编号和提交元数据只能通过受控 Service 修改。"
                    )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("资产只能通过受控 Service 物理删除未提交草稿。")

    @property
    def draft_number(self):
        return f"D-{str(self.pk).split('-')[0].upper()}"

    @property
    def cover_attachment_link(self):
        return self.attachment_links.filter(
            role=AttachmentLink.Role.COVER,
            status=AttachmentLink.Status.ACTIVE,
            attachment__is_available=True,
            attachment__malware_scan_status__in=(
                Attachment.MalwareScanStatus.POLICY_LIMITED,
                Attachment.MalwareScanStatus.CLEAN,
            ),
        ).select_related("attachment").first()

    def __str__(self):
        return self.asset_code or self.draft_number


class AssetCustomField(models.Model):
    class FieldType(models.TextChoices):
        TEXT = "text", "文本"
        DECIMAL = "decimal", "小数"
        DATE = "date", "日期"
        BOOLEAN = "boolean", "布尔"
        SELECT = "select", "单选"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_custom_fields",
    )
    category = models.ForeignKey(
        AssetCategory,
        verbose_name="适用实物分类",
        on_delete=models.PROTECT,
        related_name="custom_fields",
    )
    name = models.CharField("字段名称", max_length=100)
    code = models.CharField("字段编码", max_length=100)
    normalized_code = models.CharField(
        "规范化字段编码", max_length=100, editable=False
    )
    field_type = models.CharField(
        "字段类型", max_length=16, choices=FieldType.choices
    )
    required = models.BooleanField("提交时必填", default=False)
    options_json = models.JSONField("选项", null=True, blank=True)
    display_order = models.PositiveIntegerField("显示顺序", default=1)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "资产动态字段"
        verbose_name_plural = "资产动态字段"
        ordering = ("company_id", "display_order", "normalized_code")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "normalized_code"),
                name="uq_asset_custom_field_company_code",
            ),
            models.CheckConstraint(
                condition=~Q(normalized_code=""),
                name="ck_asset_custom_field_code_nonempty",
            ),
            models.CheckConstraint(
                condition=Q(
                    field_type__in=("text", "decimal", "date", "boolean", "select")
                ),
                name="ck_asset_custom_field_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(field_type="select", options_json__isnull=False)
                    | Q(
                        field_type__in=("text", "decimal", "date", "boolean"),
                        options_json__isnull=True,
                    )
                ),
                name="ck_asset_custom_field_options_type",
            ),
            models.CheckConstraint(
                condition=Q(display_order__gte=1),
                name="ck_asset_custom_field_order_positive",
            ),
        ]

    def _normalize_code(self):
        self.code = clean_display_identifier(self.code)
        self.normalized_code = normalize_identifier(self.code)

    def clean(self):
        super().clean()
        self._normalize_code()
        errors = {}
        if not self.normalized_code:
            errors["code"] = "字段编码不能为空。"
        if self.category_id and self.category.company_id != self.company_id:
            errors["category"] = "适用分类必须属于同一公司。"
        if self.field_type == self.FieldType.SELECT:
            options = self.options_json
            if not isinstance(options, list) or not options:
                errors["options_json"] = "单选字段必须配置非空字符串选项数组。"
            elif any(
                not isinstance(option, str)
                or not option
                or option != option.strip()
                for option in options
            ):
                errors["options_json"] = "单选选项必须是无首尾空白的非空字符串。"
            elif len(options) != len(set(options)):
                errors["options_json"] = "单选选项不能重复。"
        elif self.options_json is not None:
            errors["options_json"] = "只有单选字段可以配置选项。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        self._normalize_code()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None and "code" in update_fields:
            kwargs["update_fields"] = {*update_fields, "normalized_code"}
        return super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class AssetCustomValue(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_custom_values",
    )
    asset = models.ForeignKey(
        Asset,
        verbose_name="资产",
        on_delete=models.CASCADE,
        related_name="custom_values",
    )
    custom_field = models.ForeignKey(
        AssetCustomField,
        verbose_name="动态字段",
        on_delete=models.PROTECT,
        related_name="values",
    )
    value_text = models.TextField("文本值", null=True, blank=True)
    value_decimal = models.DecimalField(
        "小数值", max_digits=30, decimal_places=8, null=True, blank=True
    )
    value_date = models.DateField("日期值", null=True, blank=True)
    value_boolean = models.BooleanField("布尔值", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        verbose_name = "资产动态字段值"
        verbose_name_plural = "资产动态字段值"
        constraints = [
            models.UniqueConstraint(
                fields=("asset", "custom_field"),
                name="uq_asset_custom_value_asset_field",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        value_text__isnull=False,
                        value_decimal__isnull=True,
                        value_date__isnull=True,
                        value_boolean__isnull=True,
                    )
                    | Q(
                        value_text__isnull=True,
                        value_decimal__isnull=False,
                        value_date__isnull=True,
                        value_boolean__isnull=True,
                    )
                    | Q(
                        value_text__isnull=True,
                        value_decimal__isnull=True,
                        value_date__isnull=False,
                        value_boolean__isnull=True,
                    )
                    | Q(
                        value_text__isnull=True,
                        value_decimal__isnull=True,
                        value_date__isnull=True,
                        value_boolean__isnull=False,
                    )
                ),
                name="ck_asset_custom_value_one_column",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        if self.custom_field_id:
            field = self.custom_field
            if field.company_id != self.company_id:
                errors["custom_field"] = "动态字段必须属于同一公司。"
            elif self.asset_id and field.category_id != self.asset.category_id:
                errors["custom_field"] = "动态字段不适用于该资产实物分类。"
            values = {
                AssetCustomField.FieldType.TEXT: self.value_text,
                AssetCustomField.FieldType.SELECT: self.value_text,
                AssetCustomField.FieldType.DECIMAL: self.value_decimal,
                AssetCustomField.FieldType.DATE: self.value_date,
                AssetCustomField.FieldType.BOOLEAN: self.value_boolean,
            }
            expected_value = values.get(field.field_type)
            if field.field_type not in values:
                errors["custom_field"] = "不支持的动态字段类型。"
            elif expected_value is None:
                errors["custom_field"] = "动态字段值保存到了错误的值列。"
            if (
                field.field_type == AssetCustomField.FieldType.SELECT
                and self.value_text not in (field.options_json or [])
            ):
                errors["value_text"] = "单选值不属于已批准选项。"
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.asset} / {self.custom_field}"


class AssetCodeHistory(models.Model):
    class EventType(models.TextChoices):
        ISSUED = "issued", "首次发号"
        CORRECTED = "corrected", "编号更正"
        VOIDED = "voided", "编号作废"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_code_histories",
    )
    asset = models.ForeignKey(
        Asset,
        verbose_name="资产",
        on_delete=models.PROTECT,
        related_name="code_history",
    )
    event_type = models.CharField(
        "事件类型", max_length=16, choices=EventType.choices
    )
    old_issued_code = models.ForeignKey(
        IssuedCode,
        verbose_name="原编号登记",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="old_code_history",
    )
    new_issued_code = models.ForeignKey(
        IssuedCode,
        verbose_name="新编号登记",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="new_code_history",
    )
    reason = models.TextField("原因", blank=True)
    effective_at = models.DateTimeField("业务生效时间")
    operated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="operated_asset_code_histories",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        verbose_name = "资产编号历史"
        verbose_name_plural = "资产编号历史"
        ordering = ("asset_id", "created_at")
        constraints = [
            models.CheckConstraint(
                condition=Q(event_type__in=("issued", "corrected", "voided")),
                name="ck_asset_code_history_type",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        event_type="issued",
                        old_issued_code__isnull=True,
                        new_issued_code__isnull=False,
                    )
                    | Q(
                        event_type="corrected",
                        old_issued_code__isnull=False,
                        new_issued_code__isnull=False,
                        reason__gt="",
                    )
                    | Q(
                        event_type="voided",
                        old_issued_code__isnull=False,
                        new_issued_code__isnull=True,
                        reason__gt="",
                    )
                ),
                name="ck_asset_code_history_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(old_issued_code__isnull=True)
                    | Q(new_issued_code__isnull=True)
                    | ~Q(old_issued_code=models.F("new_issued_code"))
                ),
                name="ck_asset_code_history_distinct_codes",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        for field_name in ("old_issued_code", "new_issued_code"):
            issued = getattr(self, field_name)
            if issued is not None and issued.company_id != self.company_id:
                errors[field_name] = "编号登记必须属于同一公司。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("资产编号历史只允许追加。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("资产编号历史不可删除。")

    def __str__(self):
        return f"{self.asset} - {self.get_event_type_display()}"


class AssetQrIdentityQuerySet(models.QuerySet):
    def update(self, **kwargs):
        protected = {
            "company",
            "company_id",
            "asset",
            "asset_id",
            "public_token",
            "status",
            "label_status",
            "issued_at",
            "version",
            "revoked_at",
            "revoke_reason",
            "attached_at",
        }.intersection(kwargs)
        actor_fields = {
            "issued_by",
            "issued_by_id",
            "revoked_by",
            "revoked_by_id",
            "attached_by",
            "attached_by_id",
        }.intersection(kwargs)
        if actor_fields and any(kwargs[field] is not None for field in actor_fields):
            protected.update(actor_fields)
        if protected:
            raise ValidationError("二维码身份只能通过受控打印、贴标或换标 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("二维码身份历史不可删除。")


class AssetQrIdentity(models.Model):
    class Status(models.TextChoices):
        ACTIVE = "active", "有效"
        REVOKED = "revoked", "已撤销"

    class LabelStatus(models.TextChoices):
        NOT_GENERATED = "not_generated", "未生成"
        READY_TO_PRINT = "ready_to_print", "待打印"
        PRINTED = "printed", "已打印"
        ATTACHED = "attached", "已贴标"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_qr_identities",
    )
    asset = models.ForeignKey(
        Asset,
        verbose_name="资产",
        on_delete=models.PROTECT,
        related_name="qr_identities",
    )
    public_token = models.CharField(
        "公开随机标识",
        max_length=128,
        unique=True,
        validators=[MinLengthValidator(22)],
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    label_status = models.CharField(
        "标签状态",
        max_length=16,
        choices=LabelStatus.choices,
        default=LabelStatus.READY_TO_PRINT,
    )
    issued_at = models.DateTimeField("签发时间", default=timezone.now)
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="签发人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="issued_asset_qr_identities",
    )
    revoked_at = models.DateTimeField("撤销时间", null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="撤销人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="revoked_asset_qr_identities",
    )
    revoke_reason = models.TextField("撤销原因", blank=True)
    attached_at = models.DateTimeField("贴标时间", null=True, blank=True)
    attached_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="贴标确认人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="attached_asset_qr_identities",
    )
    version = models.PositiveIntegerField("版本", default=1)

    objects = AssetQrIdentityQuerySet.as_manager()

    class Meta:
        verbose_name = "资产二维码身份"
        verbose_name_plural = "资产二维码身份"
        ordering = ("asset_id", "version")
        constraints = [
            models.UniqueConstraint(
                fields=("asset", "version"), name="uq_asset_qr_identity_version"
            ),
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(status="active"),
                name="uq_asset_qr_identity_active",
            ),
            models.CheckConstraint(
                condition=Q(version__gte=1), name="ck_asset_qr_version_positive"
            ),
            models.CheckConstraint(
                condition=Q(public_token__regex=r"^.{22,128}$"),
                name="ck_asset_qr_token_length",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("active", "revoked")),
                name="ck_asset_qr_status_valid",
            ),
            models.CheckConstraint(
                condition=Q(
                    label_status__in=(
                        "not_generated",
                        "ready_to_print",
                        "printed",
                        "attached",
                    )
                ),
                name="ck_asset_qr_label_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="active",
                        revoked_at__isnull=True,
                        revoked_by__isnull=True,
                        revoke_reason="",
                    )
                    | Q(
                        status="revoked",
                        revoked_at__isnull=False,
                    )
                    & ~Q(revoke_reason="")
                ),
                name="ck_asset_qr_status_fields",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        label_status__in=("not_generated", "ready_to_print", "printed"),
                        attached_at__isnull=True,
                        attached_by__isnull=True,
                    )
                    | Q(label_status="attached", attached_at__isnull=False)
                ),
                name="ck_asset_qr_label_fields",
            ),
        ]

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "company_id", "asset_id", "public_token", "status",
                "label_status", "issued_at", "issued_by_id", "revoked_at",
                "revoked_by_id", "revoke_reason", "attached_at",
                "attached_by_id", "version",
            ).first()
            current = {
                "company_id": self.company_id,
                "asset_id": self.asset_id,
                "public_token": self.public_token,
                "status": self.status,
                "label_status": self.label_status,
                "issued_at": self.issued_at,
                "issued_by_id": self.issued_by_id,
                "revoked_at": self.revoked_at,
                "revoked_by_id": self.revoked_by_id,
                "revoke_reason": self.revoke_reason,
                "attached_at": self.attached_at,
                "attached_by_id": self.attached_by_id,
                "version": self.version,
            }
            if previous is not None and previous != current:
                raise ValidationError(
                    "二维码身份只能由后续受控打印、贴标或换标 Service 修改。"
                )
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("二维码身份历史不可删除。")

    def clean(self):
        super().clean()
        if self.asset_id and self.asset.company_id != self.company_id:
            raise ValidationError({"asset": "二维码身份与资产必须属于同一公司。"})

    def __str__(self):
        return f"{self.asset} / QR v{self.version}"


class AssetLabelAttachmentRequestQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if kwargs and set(kwargs).issubset({"completed_by", "completed_by_id"}) and all(
            value is None for value in kwargs.values()
        ):
            return super().update(**kwargs)
        raise ValidationError("贴标幂等结果是不可变业务证据。")

    def delete(self):
        raise ValidationError("贴标幂等结果是不可变业务证据。")


class AssetLabelAttachmentRequest(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="asset_label_attachment_requests",
    )
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="label_attachment_requests",
    )
    qr_identity = models.ForeignKey(
        AssetQrIdentity,
        on_delete=models.PROTECT,
        related_name="attachment_requests",
    )
    idempotency_key = models.CharField(max_length=128)
    request_hash = models.CharField(max_length=64)
    target_status = models.CharField(max_length=32, blank=True)
    completed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="completed_asset_label_attachment_requests",
    )
    completed_at = models.DateTimeField(auto_now_add=True)

    objects = AssetLabelAttachmentRequestQuerySet.as_manager()

    class Meta:
        ordering = ("completed_at", "pk")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_label_attach_request_company_idem",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key="") & ~Q(request_hash=""),
                name="ck_label_attach_request_values",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "贴标幂等结果必须与资产属于同一公司。"
        if self.qr_identity_id:
            if self.qr_identity.company_id != self.company_id:
                errors["qr_identity"] = "贴标幂等结果必须与二维码属于同一公司。"
            elif self.asset_id and self.qr_identity.asset_id != self.asset_id:
                errors["qr_identity"] = "二维码必须属于该资产。"
        if not str(self.idempotency_key or "").strip():
            errors["idempotency_key"] = "幂等键不能为空。"
        if len(str(self.request_hash or "")) != 64:
            errors["request_hash"] = "请求摘要必须是 64 位 SHA-256。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "company_id",
                "asset_id",
                "qr_identity_id",
                "idempotency_key",
                "request_hash",
                "target_status",
                "completed_by_id",
                "completed_at",
            ).first()
            current = {
                "company_id": self.company_id,
                "asset_id": self.asset_id,
                "qr_identity_id": self.qr_identity_id,
                "idempotency_key": self.idempotency_key,
                "request_hash": self.request_hash,
                "target_status": self.target_status,
                "completed_by_id": self.completed_by_id,
                "completed_at": self.completed_at,
            }
            actor_cleared = (
                previous is not None
                and previous["completed_by_id"] is not None
                and current["completed_by_id"] is None
                and all(
                    previous[field] == current[field]
                    for field in current
                    if field != "completed_by_id"
                )
            )
            if previous != current and not actor_cleared:
                raise ValidationError("贴标幂等结果是不可变业务证据。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("贴标幂等结果是不可变业务证据。")


class AssetLabelPrintBatchQuerySet(models.QuerySet):
    def update(self, **kwargs):
        protected = {
            "company",
            "company_id",
            "batch_code",
            "template_version",
            "status",
            "include_responsible_employee",
            "include_location",
            "include_model",
            "created_at",
            "printed_at",
            "idempotency_key",
        }.intersection(kwargs)
        actor_fields = {
            "created_by",
            "created_by_id",
            "printed_by",
            "printed_by_id",
        }.intersection(kwargs)
        if actor_fields and any(kwargs[field] is not None for field in actor_fields):
            protected.update(actor_fields)
        if protected:
            raise ValidationError("打印批次只能通过受控标签 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("打印批次是审计历史，不可删除。")


class AssetLabelPrintBatch(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "草稿"
        GENERATED = "generated", "已生成"
        PRINTED = "printed", "已打印"
        CANCELLED = "cancelled", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_label_print_batches",
    )
    batch_code = models.CharField("批次编号", max_length=64)
    template_version = models.CharField("模板版本", max_length=32)
    status = models.CharField(
        "批次状态",
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    include_responsible_employee = models.BooleanField("显示责任人", default=False)
    include_location = models.BooleanField("显示位置", default=False)
    include_model = models.BooleanField("显示型号", default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="创建人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_asset_label_print_batches",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    printed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="打印确认人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="printed_asset_label_print_batches",
    )
    printed_at = models.DateTimeField("打印确认时间", null=True, blank=True)
    idempotency_key = models.CharField("幂等键", max_length=128)

    objects = AssetLabelPrintBatchQuerySet.as_manager()

    class Meta:
        verbose_name = "资产标签打印批次"
        verbose_name_plural = "资产标签打印批次"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "batch_code"),
                name="uq_label_batch_company_code",
            ),
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_label_batch_company_idem",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("draft", "generated", "printed", "cancelled")),
                name="ck_label_batch_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="printed",
                        printed_at__isnull=False,
                    )
                    | Q(
                        status__in=("draft", "generated", "cancelled"),
                        printed_by__isnull=True,
                        printed_at__isnull=True,
                    )
                ),
                name="ck_label_batch_print_fields",
            ),
            models.CheckConstraint(
                condition=~Q(batch_code=""), name="ck_label_batch_code_nonempty"
            ),
            models.CheckConstraint(
                condition=~Q(template_version=""),
                name="ck_label_batch_template_nonempty",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""),
                name="ck_label_batch_idem_nonempty",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if not str(self.batch_code or "").strip():
            errors["batch_code"] = "批次编号不能为空。"
        if not str(self.template_version or "").strip():
            errors["template_version"] = "模板版本不能为空。"
        if not str(self.idempotency_key or "").strip():
            errors["idempotency_key"] = "幂等键不能为空。"
        if self.status == self.Status.PRINTED:
            if self.printed_at is None:
                errors["printed_at"] = "已打印批次必须记录打印确认时间。"
        elif self.printed_by_id is not None or self.printed_at is not None:
            errors["status"] = "未打印批次不得记录打印确认人或时间。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "company_id",
                "batch_code",
                "template_version",
                "status",
                "include_responsible_employee",
                "include_location",
                "include_model",
                "created_by_id",
                "created_at",
                "printed_by_id",
                "printed_at",
                "idempotency_key",
            ).first()
            current = {
                "company_id": self.company_id,
                "batch_code": self.batch_code,
                "template_version": self.template_version,
                "status": self.status,
                "include_responsible_employee": self.include_responsible_employee,
                "include_location": self.include_location,
                "include_model": self.include_model,
                "created_by_id": self.created_by_id,
                "created_at": self.created_at,
                "printed_by_id": self.printed_by_id,
                "printed_at": self.printed_at,
                "idempotency_key": self.idempotency_key,
            }
            actor_cleared = previous is not None and all(
                current[field] == previous[field]
                for field in current
                if field not in {"created_by_id", "printed_by_id"}
            ) and all(
                current[field] == previous[field]
                or (previous[field] is not None and current[field] is None)
                for field in ("created_by_id", "printed_by_id")
            )
            if previous is not None and previous != current and not actor_cleared:
                raise ValidationError("打印批次只能通过受控标签 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("打印批次是审计历史，不可删除。")

    def __str__(self):
        return self.batch_code


class AssetLabelPrintItemQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if kwargs:
            raise ValidationError("打印明细只能通过受控标签 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("打印明细是审计历史，不可删除。")


class AssetLabelPrintItem(models.Model):
    class PrintStatus(models.TextChoices):
        GENERATED = "generated", "已生成"
        PRINTED = "printed", "已打印"
        CANCELLED = "cancelled", "已取消"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(
        AssetLabelPrintBatch,
        verbose_name="打印批次",
        on_delete=models.CASCADE,
        related_name="items",
    )
    qr_identity = models.ForeignKey(
        AssetQrIdentity,
        verbose_name="二维码身份",
        on_delete=models.PROTECT,
        related_name="print_items",
    )
    page_no = models.PositiveIntegerField("页码")
    position_no = models.PositiveIntegerField("页内位置")
    label_snapshot_json = models.JSONField("标签文字快照", default=dict)
    print_status = models.CharField(
        "打印状态",
        max_length=16,
        choices=PrintStatus.choices,
        default=PrintStatus.GENERATED,
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = AssetLabelPrintItemQuerySet.as_manager()

    class Meta:
        verbose_name = "资产标签打印明细"
        verbose_name_plural = "资产标签打印明细"
        ordering = ("batch_id", "page_no", "position_no")
        constraints = [
            models.UniqueConstraint(
                fields=("batch", "qr_identity"),
                name="uq_label_item_batch_qr",
            ),
            models.UniqueConstraint(
                fields=("batch", "page_no", "position_no"),
                name="uq_label_item_batch_position",
            ),
            models.CheckConstraint(
                condition=Q(page_no__gte=1), name="ck_label_item_page_positive"
            ),
            models.CheckConstraint(
                condition=Q(position_no__gte=1),
                name="ck_label_item_position_positive",
            ),
            models.CheckConstraint(
                condition=Q(print_status__in=("generated", "printed", "cancelled")),
                name="ck_label_item_status",
            ),
        ]

    def clean(self):
        super().clean()
        if self.batch_id and self.qr_identity_id:
            if self.batch.company_id != self.qr_identity.company_id:
                raise ValidationError(
                    {"qr_identity": "打印批次与二维码身份必须属于同一公司。"}
                )
        if not isinstance(self.label_snapshot_json, dict) or not self.label_snapshot_json:
            raise ValidationError({"label_snapshot_json": "标签文字快照必须是非空对象。"})

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("打印明细只能通过受控标签 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("打印明细是审计历史，不可删除。")

    def __str__(self):
        return f"{self.batch} / {self.page_no}-{self.position_no}"


class AssetMovementQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs).issubset({"operated_by", "operated_by_id"}) and all(
            value is None for value in kwargs.values()
        ):
            if self.filter(operated_by__isnull=True).exists():
                raise ValidationError("资产变动历史只允许将原操作人清空。")
            return super().update(**kwargs)
        raise ValidationError("资产变动历史只允许追加。")

    def delete(self):
        raise ValidationError("资产变动历史不可删除。")


class AssetMovement(models.Model):
    class MovementType(models.TextChoices):
        ASSIGNMENT = "assignment", "领用"
        ASSIGNMENT_RETURN = "assignment_return", "领用归还"
        TRANSFER = "transfer", "调拨/责任位置变更"
        LOAN = "loan", "借出"
        LOAN_RETURN = "loan_return", "借出归还"
        IDLE = "idle", "闲置"
        ACTIVATE = "activate", "启用"
        REPAIR_START = "repair_start", "送修"
        REPAIR_COMPLETE = "repair_complete", "维修完成"
        LABEL_ACTIVATION = "label_activation", "首次贴标启用"
        DISPOSAL_START = "disposal_start", "发起处置"
        DISPOSAL_CANCEL = "disposal_cancel", "取消处置"
        DISPOSAL_COMPLETE = "disposal_complete", "完成处置"
        DISPOSAL_REVERSAL = "disposal_reversal", "处置冲销"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="asset_movements",
    )
    asset = models.ForeignKey(
        Asset,
        verbose_name="资产",
        on_delete=models.PROTECT,
        related_name="movements",
    )
    movement_type = models.CharField(
        "变动类型", max_length=32, choices=MovementType.choices
    )
    effective_at = models.DateTimeField("生效时间")
    from_department = models.ForeignKey(
        Department,
        verbose_name="原部门",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_movements_from_department",
    )
    to_department = models.ForeignKey(
        Department,
        verbose_name="新部门",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_movements_to_department",
    )
    from_employee = models.ForeignKey(
        Employee,
        verbose_name="原责任人",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_movements_from_employee",
    )
    to_employee = models.ForeignKey(
        Employee,
        verbose_name="新责任人",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_movements_to_employee",
    )
    from_location = models.ForeignKey(
        Location,
        verbose_name="原位置",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_movements_from_location",
    )
    to_location = models.ForeignKey(
        Location,
        verbose_name="新位置",
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_movements_to_location",
    )
    from_status = models.CharField("原状态", max_length=32)
    to_status = models.CharField("新状态", max_length=32)
    reason = models.TextField("原因")
    remark = models.TextField("备注", blank=True)
    idempotency_key = models.CharField("幂等键", max_length=128)
    operated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="操作人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="operated_asset_movements",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = AssetMovementQuerySet.as_manager()

    class Meta:
        verbose_name = "资产变动历史"
        verbose_name_plural = "资产变动历史"
        ordering = ("asset_id", "effective_at", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_movement_company_idem",
            ),
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(movement_type="label_activation"),
                name="uq_movement_asset_label_activation",
            ),
            models.CheckConstraint(
                condition=Q(
                    movement_type__in=(
                        "assignment",
                        "assignment_return",
                        "transfer",
                        "loan",
                        "loan_return",
                        "idle",
                        "activate",
                        "repair_start",
                        "repair_complete",
                        "label_activation",
                        "disposal_start",
                        "disposal_cancel",
                        "disposal_complete",
                        "disposal_reversal",
                    )
                ),
                name="ck_movement_type_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(movement_type="label_activation")
                    | Q(
                        movement_type="label_activation",
                        from_status="pending_label",
                        to_status__in=("in_use", "idle"),
                        from_department__isnull=False,
                        to_department__isnull=False,
                        from_employee__isnull=False,
                        to_employee__isnull=False,
                        from_location__isnull=False,
                        to_location__isnull=False,
                    )
                ),
                name="ck_movement_label_activation_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~Q(from_department=models.F("to_department"))
                    | ~Q(from_employee=models.F("to_employee"))
                    | ~Q(from_location=models.F("to_location"))
                    | ~Q(from_status=models.F("to_status"))
                ),
                name="ck_movement_has_change",
            ),
            models.CheckConstraint(
                condition=~Q(idempotency_key=""),
                name="ck_movement_idem_nonempty",
            ),
            models.CheckConstraint(
                condition=~Q(reason=""),
                name="ck_movement_reason_nonempty",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.movement_type not in self.MovementType.values:
            errors["movement_type"] = "资产变动类型无效。"
        valid_statuses = set(Asset.AssetStatus.values)
        if self.from_status not in valid_statuses:
            errors["from_status"] = "原资产状态无效。"
        if self.to_status not in valid_statuses:
            errors["to_status"] = "新资产状态无效。"
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        pairs = (
            ("from_department", "to_department"),
            ("from_employee", "to_employee"),
            ("from_location", "to_location"),
        )
        for from_name, to_name in pairs:
            from_obj = getattr(self, from_name)
            to_obj = getattr(self, to_name)
            for field_name, obj in ((from_name, from_obj), (to_name, to_obj)):
                if obj is not None and obj.company_id != self.company_id:
                    errors[field_name] = "变动快照必须属于同一公司。"
        if self.movement_type == self.MovementType.LABEL_ACTIVATION:
            if self.from_status != Asset.AssetStatus.PENDING_LABEL:
                errors["from_status"] = "首次贴标必须从待贴标状态开始。"
            if self.to_status not in {Asset.AssetStatus.IN_USE, Asset.AssetStatus.IDLE}:
                errors["to_status"] = "首次贴标只能进入在用或闲置。"
            for from_name, to_name in pairs:
                from_obj = getattr(self, from_name)
                to_obj = getattr(self, to_name)
                if from_obj is None or to_obj is None:
                    errors[from_name] = "首次贴标必须保存完整责任与位置快照。"
                elif from_obj.pk != to_obj.pk:
                    errors[to_name] = "首次贴标不改变部门、责任人或位置。"
        has_change = any(
            getattr(self, from_name + "_id") != getattr(self, to_name + "_id")
            for from_name, to_name in (
                ("from_department", "to_department"),
                ("from_employee", "to_employee"),
                ("from_location", "to_location"),
            )
        ) or self.from_status != self.to_status
        if not has_change:
            errors["movement_type"] = "资产变动必须至少改变一个当前维度。"
        if self.effective_at and self.effective_at > timezone.now():
            errors["effective_at"] = "变动生效时间不得晚于当前时间。"
        if not str(self.idempotency_key or "").strip():
            errors["idempotency_key"] = "幂等键不能为空。"
        if not str(self.reason or "").strip():
            errors["reason"] = "资产变动必须记录原因。"
        if self._state.adding and self.operated_by_id is None:
            errors["operated_by"] = "资产变动必须记录操作人。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            concrete_fields = list(self._meta.concrete_fields)
            immutable_fields = [field.attname for field in concrete_fields]
            previous = type(self).objects.filter(pk=self.pk).values(
                *immutable_fields
            ).first()
            current = {
                field.attname: getattr(self, field.attname)
                for field in concrete_fields
            }
            actor_cleared = previous is not None and all(
                current[field] == previous[field]
                for field in immutable_fields
                if field != "operated_by_id"
            ) and previous["operated_by_id"] is not None and self.operated_by_id is None
            if not actor_cleared:
                raise ValidationError("资产变动历史只允许追加。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("资产变动历史不可删除。")

    def __str__(self):
        return f"{self.asset} / {self.get_movement_type_display()}"


class AssetLoanQuerySet(models.QuerySet):
    def update(self, **kwargs):
        actor_fields = {
            "handled_by",
            "handled_by_id",
            "created_by",
            "created_by_id",
        }
        if set(kwargs).issubset(actor_fields) and all(
            value is None for value in kwargs.values()
        ):
            return super().update(**kwargs)
        raise ValidationError("借用记录只能通过受控借还 Service 修改。")

    def delete(self):
        raise ValidationError("借用记录是业务历史，不可删除。")


class AssetLoan(models.Model):
    class BorrowerType(models.TextChoices):
        INTERNAL_EMPLOYEE = "internal_employee", "内部员工"
        EXTERNAL = "external", "外部借用方"

    class Status(models.TextChoices):
        ACTIVE = "active", "借出中"
        RETURNED = "returned", "已归还"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="asset_loans", verbose_name="公司"
    )
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="loans", verbose_name="资产"
    )
    borrower_type = models.CharField(
        "借用方类型", max_length=32, choices=BorrowerType.choices
    )
    borrower_employee = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="borrowed_asset_loans",
        verbose_name="内部借用员工",
    )
    borrower_name_snapshot = models.CharField("内部借用人姓名快照", max_length=200, blank=True)
    borrower_name = models.CharField("外部借用人", max_length=200, blank=True)
    borrower_organization = models.CharField("外部借用单位", max_length=200, blank=True)
    loan_date = models.DateField("借出日期")
    expected_return_date = models.DateField("预计归还日期")
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="handled_asset_loans",
        verbose_name="借出经办人",
    )
    previous_asset_status = models.CharField("借出前状态", max_length=32)
    reason = models.TextField("借出原因")
    status = models.CharField(
        "借用状态", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    returned_at = models.DateTimeField("实际归还时间", null=True, blank=True)
    received_by_employee = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="received_asset_loan_returns",
        verbose_name="归还接收人",
    )
    return_department = models.ForeignKey(
        Department,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_loan_returns",
        verbose_name="归还部门",
    )
    return_responsible_employee = models.ForeignKey(
        Employee,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="responsible_asset_loan_returns",
        verbose_name="归还后责任人",
    )
    return_location = models.ForeignKey(
        Location,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="asset_loan_returns",
        verbose_name="归还位置",
    )
    return_asset_status = models.CharField("归还后状态", max_length=32, blank=True)
    return_remark = models.TextField("归还备注", blank=True)
    loan_movement = models.OneToOneField(
        AssetMovement,
        on_delete=models.PROTECT,
        related_name="loan_record",
        verbose_name="借出变动",
    )
    return_movement = models.OneToOneField(
        AssetMovement,
        null=True,
        blank=True,
        on_delete=models.PROTECT,
        related_name="loan_return_record",
        verbose_name="归还变动",
    )
    loan_idempotency_key = models.CharField("借出幂等键", max_length=128)
    return_idempotency_key = models.CharField(
        "归还幂等键", max_length=128, null=True, blank=True
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_asset_loans",
        verbose_name="创建人",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    objects = AssetLoanQuerySet.as_manager()

    class Meta:
        verbose_name = "资产借用"
        verbose_name_plural = "资产借用"
        ordering = ("-loan_date", "-created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(status="active"),
                name="uq_asset_loan_active_asset",
            ),
            models.UniqueConstraint(
                fields=("company", "loan_idempotency_key"),
                name="uq_asset_loan_company_loan_idem",
            ),
            models.UniqueConstraint(
                fields=("company", "return_idempotency_key"),
                condition=Q(return_idempotency_key__isnull=False),
                name="uq_asset_loan_company_return_idem",
            ),
            models.CheckConstraint(
                condition=Q(borrower_type__in=("internal_employee", "external")),
                name="ck_asset_loan_borrower_type",
            ),
            models.CheckConstraint(
                condition=Q(status__in=("active", "returned")),
                name="ck_asset_loan_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        borrower_type="internal_employee",
                        borrower_employee__isnull=False,
                        borrower_name_snapshot__gt="",
                        borrower_name="",
                        borrower_organization="",
                    )
                    | Q(
                        borrower_type="external",
                        borrower_employee__isnull=True,
                        borrower_name_snapshot="",
                        borrower_name__gt="",
                    )
                ),
                name="ck_asset_loan_borrower_fields",
            ),
            models.CheckConstraint(
                condition=Q(expected_return_date__gte=models.F("loan_date")),
                name="ck_asset_loan_expected_date",
            ),
            models.CheckConstraint(
                condition=Q(previous_asset_status__in=("in_use", "idle")),
                name="ck_asset_loan_previous_status",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="active",
                        returned_at__isnull=True,
                        received_by_employee__isnull=True,
                        return_department__isnull=True,
                        return_responsible_employee__isnull=True,
                        return_location__isnull=True,
                        return_asset_status="",
                        return_movement__isnull=True,
                        return_idempotency_key__isnull=True,
                    )
                    | Q(
                        status="returned",
                        returned_at__isnull=False,
                        received_by_employee__isnull=False,
                        return_department__isnull=False,
                        return_responsible_employee__isnull=False,
                        return_location__isnull=False,
                        return_asset_status__in=("in_use", "idle"),
                        return_movement__isnull=False,
                        return_idempotency_key__isnull=False,
                    )
                ),
                name="ck_asset_loan_return_fields",
            ),
            models.CheckConstraint(
                condition=Q(return_movement__isnull=True)
                | ~Q(loan_movement=models.F("return_movement")),
                name="ck_asset_loan_movements_different",
            ),
            models.CheckConstraint(
                condition=~Q(loan_idempotency_key="") & ~Q(reason=""),
                name="ck_asset_loan_required_text",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "借用资产必须属于同一公司。"
        if self.borrower_type == self.BorrowerType.INTERNAL_EMPLOYEE:
            employee = self.borrower_employee
            if employee is None:
                errors["borrower_employee"] = "内部借用必须选择员工。"
            elif employee.company_id != self.company_id:
                errors["borrower_employee"] = "内部借用员工必须属于同一公司。"
            elif employee.employment_status != "active" or not employee.is_active:
                errors["borrower_employee"] = "内部借用员工必须在职且启用。"
        for field_name in (
            "received_by_employee",
            "return_department",
            "return_responsible_employee",
            "return_location",
        ):
            obj = getattr(self, field_name)
            if obj is not None and obj.company_id != self.company_id:
                errors[field_name] = "归还目标必须属于同一公司。"
        if self.handled_by_id is None and self._state.adding:
            errors["handled_by"] = "借出经办人必填。"
        if self.created_by_id is None and self._state.adding:
            errors["created_by"] = "借出创建人必填。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("借用记录只能通过受控借还 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("借用记录是业务历史，不可删除。")

    def __str__(self):
        return f"{self.asset} / {self.get_borrower_type_display()}"


class AssetDisposalQuerySet(models.QuerySet):
    def update(self, **kwargs):
        raise ValidationError("处置记录只能通过受控处置 Service 修改。")

    def delete(self):
        raise ValidationError("处置记录和财务快照不可删除。")


class AssetDisposal(models.Model):
    class DisposalType(models.TextChoices):
        SCRAP = "scrap", "报废"
        SALE = "sale", "出售"
        OTHER = "other", "其他处置"

    class Status(models.TextChoices):
        DRAFT = "draft", "处理中"
        FINANCE_LOCKED = "finance_locked", "财务已锁定"
        CONFIRMED = "confirmed", "已完成"
        CANCELLED = "cancelled", "已取消"
        REVERSED = "reversed", "已冲销"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company, on_delete=models.PROTECT, related_name="asset_disposals", verbose_name="公司"
    )
    asset = models.ForeignKey(
        Asset, on_delete=models.PROTECT, related_name="disposals", verbose_name="资产"
    )
    disposal_type = models.CharField(
        "处置类型", max_length=16, choices=DisposalType.choices
    )
    application_date = models.DateField("申请日期")
    planned_disposal_date = models.DateField("拟处置日期")
    actual_disposal_date = models.DateField("实际处置日期", null=True, blank=True)
    reason = models.TextField("处置原因")
    description = models.TextField("处置说明", blank=True)
    recipient_name = models.CharField("接收方/去向", max_length=200, blank=True)
    disposal_income = models.DecimalField(
        "处置收入", max_digits=18, decimal_places=2, null=True, blank=True
    )
    original_cost_snapshot = models.DecimalField(
        "原值快照", max_digits=18, decimal_places=2, null=True, blank=True
    )
    actual_accumulated_depreciation_snapshot = models.DecimalField(
        "实际累计折旧快照", max_digits=18, decimal_places=2, null=True, blank=True
    )
    impairment_snapshot = models.DecimalField(
        "减值快照", max_digits=18, decimal_places=2, null=True, blank=True
    )
    book_value_snapshot = models.DecimalField(
        "账面净值快照", max_digits=18, decimal_places=2, null=True, blank=True
    )
    previous_asset_status = models.CharField("处置前资产状态", max_length=32)
    status = models.CharField(
        "处置状态", max_length=16, choices=Status.choices, default=Status.DRAFT
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="initiated_asset_disposals",
        verbose_name="发起人",
    )
    finance_locked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="finance_locked_asset_disposals",
        verbose_name="财务锁定人",
    )
    finance_locked_at = models.DateTimeField("财务锁定时间", null=True, blank=True)
    handled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="handled_asset_disposals",
        verbose_name="处置经办人",
    )
    confirmed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="confirmed_asset_disposals",
        verbose_name="完成人",
    )
    confirmed_at = models.DateTimeField("完成时间", null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cancelled_asset_disposals",
        verbose_name="取消人",
    )
    cancelled_at = models.DateTimeField("取消时间", null=True, blank=True)
    cancellation_reason = models.TextField("取消原因", blank=True)
    idempotency_key = models.CharField("发起幂等键", max_length=128)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = AssetDisposalQuerySet.as_manager()

    class Meta:
        verbose_name = "资产处置"
        verbose_name_plural = "资产处置"
        ordering = ("-created_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_disposal_company_idem",
            ),
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(status__in=("draft", "finance_locked")),
                name="uq_disposal_asset_open",
            ),
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(status="confirmed"),
                name="uq_disposal_asset_confirmed",
            ),
            models.CheckConstraint(
                condition=Q(disposal_type__in=("scrap", "sale", "other")),
                name="ck_disposal_type",
            ),
            models.CheckConstraint(
                condition=Q(
                    status__in=(
                        "draft",
                        "finance_locked",
                        "confirmed",
                        "cancelled",
                        "reversed",
                    )
                ),
                name="ck_disposal_status",
            ),
            models.CheckConstraint(
                condition=Q(planned_disposal_date__gte=models.F("application_date")),
                name="ck_disposal_planned_date",
            ),
            models.CheckConstraint(
                condition=Q(actual_disposal_date__isnull=True)
                | Q(actual_disposal_date__gte=models.F("application_date")),
                name="ck_disposal_actual_date",
            ),
            models.CheckConstraint(
                condition=(
                    (Q(disposal_income__isnull=True) | Q(disposal_income__gte=0))
                    & (
                        Q(original_cost_snapshot__isnull=True)
                        | Q(original_cost_snapshot__gte=0)
                    )
                    & (
                        Q(actual_accumulated_depreciation_snapshot__isnull=True)
                        | Q(actual_accumulated_depreciation_snapshot__gte=0)
                    )
                    & (
                        Q(impairment_snapshot__isnull=True)
                        | Q(impairment_snapshot__gte=0)
                    )
                    & (
                        Q(book_value_snapshot__isnull=True)
                        | Q(book_value_snapshot__gte=0)
                    )
                ),
                name="ck_disposal_amounts_nonnegative",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        original_cost_snapshot__isnull=True,
                        actual_accumulated_depreciation_snapshot__isnull=True,
                        impairment_snapshot__isnull=True,
                        book_value_snapshot__isnull=True,
                    )
                    | Q(
                        original_cost_snapshot=models.F(
                            "actual_accumulated_depreciation_snapshot"
                        )
                        + models.F("impairment_snapshot")
                        + models.F("book_value_snapshot")
                    )
                ),
                name="ck_disposal_snapshot_reconciles",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="draft",
                        finance_locked_at__isnull=True,
                        finance_locked_by__isnull=True,
                        original_cost_snapshot__isnull=True,
                        actual_accumulated_depreciation_snapshot__isnull=True,
                        impairment_snapshot__isnull=True,
                        book_value_snapshot__isnull=True,
                        confirmed_at__isnull=True,
                        confirmed_by__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                    )
                    | Q(
                        status="finance_locked",
                        actual_disposal_date__isnull=False,
                        finance_locked_at__isnull=False,
                        original_cost_snapshot__isnull=False,
                        actual_accumulated_depreciation_snapshot__isnull=False,
                        impairment_snapshot__isnull=False,
                        book_value_snapshot__isnull=False,
                        disposal_income__isnull=False,
                        confirmed_at__isnull=True,
                        confirmed_by__isnull=True,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                    )
                    | Q(
                        status__in=("confirmed", "reversed"),
                        actual_disposal_date__isnull=False,
                        finance_locked_at__isnull=False,
                        original_cost_snapshot__isnull=False,
                        actual_accumulated_depreciation_snapshot__isnull=False,
                        impairment_snapshot__isnull=False,
                        book_value_snapshot__isnull=False,
                        disposal_income__isnull=False,
                        confirmed_at__isnull=False,
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                    )
                    | (
                        Q(
                            status="cancelled",
                            confirmed_at__isnull=True,
                            confirmed_by__isnull=True,
                            cancelled_at__isnull=False,
                        )
                        & ~Q(cancellation_reason="")
                    )
                ),
                name="ck_disposal_status_fields",
            ),
            models.CheckConstraint(
                condition=Q(previous_asset_status__in=("in_use", "idle", "under_repair")),
                name="ck_disposal_previous_status",
            ),
            models.CheckConstraint(
                condition=~Q(reason="") & ~Q(idempotency_key=""),
                name="ck_disposal_required_text",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "处置资产必须属于同一公司。"
        if self._state.adding and self.actual_disposal_date is not None:
            errors["actual_disposal_date"] = "发起处置时实际处置日期必须为空。"
        if self.actual_disposal_date and self.actual_disposal_date > timezone.localdate():
            errors["actual_disposal_date"] = "实际处置日期不得晚于当前上海业务日。"
        if self._state.adding and self.initiated_by_id is None:
            errors["initiated_by"] = "处置发起人必填。"
        if self._state.adding and self.handled_by_id is None:
            errors["handled_by"] = "处置经办人必填。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("处置记录只能通过受控处置 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("处置记录和财务快照不可删除。")

    def __str__(self):
        return f"{self.asset} / {self.get_disposal_type_display()}"


class AssetDisposalReversalQuerySet(models.QuerySet):
    def update(self, **kwargs):
        if set(kwargs).issubset({"reversed_by", "reversed_by_id"}) and all(
            value is None for value in kwargs.values()
        ):
            return super().update(**kwargs)
        raise ValidationError("处置冲销记录只允许追加。")

    def delete(self):
        raise ValidationError("处置冲销记录不可删除。")


class AssetDisposalReversal(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        on_delete=models.PROTECT,
        related_name="asset_disposal_reversals",
        verbose_name="公司",
    )
    asset_disposal = models.OneToOneField(
        AssetDisposal,
        on_delete=models.PROTECT,
        related_name="reversal",
        verbose_name="原处置",
    )
    reason = models.TextField("冲销原因")
    restored_asset_status = models.CharField("恢复资产状态", max_length=32)
    idempotency_key = models.CharField("冲销幂等键", max_length=128)
    reversed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reversed_asset_disposals",
        verbose_name="冲销人",
    )
    reversed_at = models.DateTimeField("冲销时间")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    objects = AssetDisposalReversalQuerySet.as_manager()

    class Meta:
        verbose_name = "资产处置冲销"
        verbose_name_plural = "资产处置冲销"
        ordering = ("-reversed_at",)
        constraints = [
            models.UniqueConstraint(
                fields=("company", "idempotency_key"),
                name="uq_disposal_reversal_company_idem",
            ),
            models.CheckConstraint(
                condition=Q(restored_asset_status__in=("in_use", "idle", "under_repair")),
                name="ck_disposal_reversal_status",
            ),
            models.CheckConstraint(
                condition=~Q(reason="") & ~Q(idempotency_key=""),
                name="ck_disposal_reversal_required_text",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_disposal_id and self.asset_disposal.company_id != self.company_id:
            errors["asset_disposal"] = "冲销的处置必须属于同一公司。"
        if self._state.adding and self.reversed_by_id is None:
            errors["reversed_by"] = "处置冲销必须记录操作人。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            raise ValidationError("处置冲销记录只允许追加。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("处置冲销记录不可删除。")

    def __str__(self):
        return f"{self.asset_disposal} / 冲销"


class AttachmentLinkQuerySet(models.QuerySet):
    def update(self, **kwargs):
        immutable = {
            "company",
            "company_id",
            "asset",
            "asset_id",
            "asset_disposal",
            "asset_disposal_id",
            "attachment",
            "attachment_id",
            "role",
            "security_class",
        }.intersection(kwargs)
        if immutable:
            raise ValidationError("附件关联身份、用途和安全分类不可修改；请作废后新建。")
        protected = {
            "status",
            "void_reason",
            "voided_at",
        }.intersection(kwargs)
        void_actor_keys = {"voided_by", "voided_by_id"}.intersection(kwargs)
        if void_actor_keys and any(kwargs[key] is not None for key in void_actor_keys):
            protected.update(void_actor_keys)
        if protected:
            raise ValidationError("附件业务状态只能通过受控 Service 修改。")
        return super().update(**kwargs)

    def delete(self):
        raise ValidationError("附件业务关联不得物理删除，只能通过受控 Service 作废。")


class AttachmentLink(models.Model):
    class Role(models.TextChoices):
        COVER = "cover", "封面照片"
        PHOTO = "photo", "资产照片"
        INVOICE = "invoice", "发票"
        CONTRACT = "contract", "合同"
        ACCEPTANCE = "acceptance", "验收单"
        CERTIFICATE = "certificate", "合格证"
        MANUAL = "manual", "说明书"
        DISPOSAL = "disposal", "处置证据"
        OTHER = "other", "其他"

    class SecurityClass(models.TextChoices):
        A0 = "A0", "普通附件"
        A1 = "A1", "财务附件"

    class Status(models.TextChoices):
        ACTIVE = "active", "有效"
        VOIDED = "voided", "已作废"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    company = models.ForeignKey(
        Company,
        verbose_name="公司",
        on_delete=models.PROTECT,
        related_name="attachment_links",
    )
    attachment = models.OneToOneField(
        Attachment,
        verbose_name="附件",
        on_delete=models.PROTECT,
        related_name="business_link",
    )
    asset = models.ForeignKey(
        Asset,
        verbose_name="资产",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachment_links",
    )
    asset_disposal = models.ForeignKey(
        AssetDisposal,
        verbose_name="资产处置",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="attachment_links",
    )
    role = models.CharField("附件用途", max_length=32, choices=Role.choices)
    security_class = models.CharField(
        "安全分类", max_length=2, choices=SecurityClass.choices
    )
    status = models.CharField(
        "状态", max_length=16, choices=Status.choices, default=Status.ACTIVE
    )
    void_reason = models.TextField("作废原因", blank=True)
    voided_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="作废人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="voided_attachment_links",
    )
    voided_at = models.DateTimeField("作废时间", null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="关联人",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="created_attachment_links",
    )
    created_at = models.DateTimeField("关联时间", auto_now_add=True)

    objects = AttachmentLinkQuerySet.as_manager()

    class Meta:
        verbose_name = "附件业务关联"
        verbose_name_plural = "附件业务关联"
        ordering = ("asset_id", "created_at")
        constraints = [
            models.UniqueConstraint(
                fields=("asset",),
                condition=Q(role="cover", status="active"),
                name="uq_attachment_link_active_cover",
            ),
            models.CheckConstraint(
                condition=(
                    Q(asset__isnull=False, asset_disposal__isnull=True)
                    | Q(asset__isnull=True, asset_disposal__isnull=False)
                ),
                name="ck_attachment_link_one_target",
            ),
            models.CheckConstraint(
                condition=Q(
                    role__in=(
                        "cover",
                        "photo",
                        "invoice",
                        "contract",
                        "acceptance",
                        "certificate",
                        "manual",
                        "disposal",
                        "other",
                    )
                ),
                name="ck_attachment_link_role",
            ),
            models.CheckConstraint(
                condition=Q(security_class__in=("A0", "A1")),
                name="ck_attachment_link_security",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        role__in=("cover", "photo", "certificate", "manual"),
                        security_class="A0",
                    )
                    | Q(
                        role__in=("invoice", "contract", "acceptance"),
                        security_class="A1",
                    )
                    | Q(role="disposal", security_class__in=("A0", "A1"))
                    | Q(role="other", security_class__in=("A0", "A1"))
                ),
                name="ck_attachment_link_role_security",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="active",
                        void_reason="",
                        voided_by__isnull=True,
                        voided_at__isnull=True,
                    )
                    | Q(
                        status="voided",
                        void_reason__gt="",
                        voided_at__isnull=False,
                    )
                ),
                name="ck_attachment_link_status_fields",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id and self.asset.company_id != self.company_id:
            errors["asset"] = "资产必须属于同一公司。"
        if self.asset_disposal_id and self.asset_disposal.company_id != self.company_id:
            errors["asset_disposal"] = "资产处置必须属于同一公司。"
        if bool(self.asset_id) == bool(self.asset_disposal_id):
            errors["asset"] = "附件必须且只能关联一个业务目标。"
        if self.attachment_id and self.attachment.company_id != self.company_id:
            errors["attachment"] = "附件必须属于同一公司。"
        a0_roles = {
            self.Role.COVER,
            self.Role.PHOTO,
            self.Role.CERTIFICATE,
            self.Role.MANUAL,
        }
        a1_roles = {self.Role.INVOICE, self.Role.CONTRACT, self.Role.ACCEPTANCE}
        if self.role in a0_roles and self.security_class != self.SecurityClass.A0:
            errors["security_class"] = "该附件用途必须使用 A0 普通分类。"
        if self.role in a1_roles and self.security_class != self.SecurityClass.A1:
            errors["security_class"] = "该附件用途必须使用 A1 财务分类。"
        if errors:
            raise ValidationError(errors)

    def save(self, *args, **kwargs):
        if not self._state.adding:
            previous = type(self).objects.filter(pk=self.pk).values(
                "company_id",
                "asset_id",
                "asset_disposal_id",
                "attachment_id",
                "role",
                "security_class",
                "status",
                "void_reason",
                "voided_by_id",
                "voided_at",
            ).first()
            current = {
                "company_id": self.company_id,
                "asset_id": self.asset_id,
                "asset_disposal_id": self.asset_disposal_id,
                "attachment_id": self.attachment_id,
                "role": self.role,
                "security_class": self.security_class,
                "status": self.status,
                "void_reason": self.void_reason,
                "voided_by_id": self.voided_by_id,
                "voided_at": self.voided_at,
            }
            if previous is not None and previous != current:
                identity_fields = (
                    "company_id",
                    "asset_id",
                    "asset_disposal_id",
                    "attachment_id",
                    "role",
                    "security_class",
                )
                if any(previous[field] != current[field] for field in identity_fields):
                    raise ValidationError(
                        "附件关联身份、用途和安全分类不可修改；请作废后新建。"
                    )
                raise ValidationError("附件业务状态只能通过受控 Service 修改。")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("附件业务关联不得物理删除，只能通过受控 Service 作废。")

    def __str__(self):
        return f"{self.asset or self.asset_disposal} - {self.get_role_display()}"
