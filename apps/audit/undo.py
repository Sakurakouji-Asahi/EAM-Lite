"""Reversible, unused asset registrations and initialization imports only.

The plan is derived from locked business records, never from browser payloads.
Audit evidence survives; unused tail numbers may be allocated again.
"""
from collections import defaultdict
import hashlib
import json

from django.apps import apps
from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.models import AuditLog, OperationUndo
from apps.audit.permissions import require_view_audit_logs, scoped_audit_logs
from apps.audit.services import request_audit_context, write_business_audit_log
from apps.assets.models import Asset, AssetCodeHistory, AssetIdentity, AssetQrIdentity, AssetRegistration
from apps.assets.permissions import can_create_asset_draft, scoped_assets_p1
from apps.assets.services import _controlled_update, _controlled_delete_asset_draft
from apps.masterdata.models import Company, ImportBatch, IssuedCode, SequenceCounter
from apps.masterdata.permissions import current_company, role_names_for

SUPPORTED = {("Asset", "asset_register"), ("ImportBatch", "import_confirm")}
SALT = "audit.operation-undo.v1"


def _digest(value):
    return hashlib.sha256(json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True).encode()).hexdigest()


def _require_access(actor, log):
    require_view_audit_logs(actor)
    company = current_company()
    if company is None or log.company_id != company.pk:
        raise PermissionDenied("该操作不属于当前公司。")
    if not scoped_audit_logs(actor, company).filter(pk=log.pk).exists():
        raise PermissionDenied("您没有查看该操作的权限。")
    if not role_names_for(actor).intersection({"finance", "equipment", "warehouse"}):
        raise PermissionDenied("撤销需要相应的资产业务办理权限。")
    if (log.object_type, log.action) not in SUPPORTED:
        raise ValidationError("此操作暂不支持从日志撤销。")
    return company


def _related_rows(model, asset_ids, *, lock):
    qs = model.objects.filter(asset_id__in=asset_ids).order_by("pk")
    if lock:
        qs = qs.select_for_update()
    return list(qs)


def _plan(actor, log, *, lock=False):
    company = _require_access(actor, log)
    is_import = log.object_type == "ImportBatch"
    batch = None
    if is_import:
        batches = ImportBatch.objects.filter(company=company)
        if lock:
            batches = batches.select_for_update()
        batch = batches.filter(pk=log.object_id).first()
        if batch is None or batch.import_type != "asset_initialization":
            raise ValidationError("目前仅支持撤销资产初始化导入。")
        if batch.status != "confirmed":
            raise ValidationError("该批次不是已确认导入，或已经撤销。")
        rows = list(batch.rows.order_by("row_number"))
        if not rows or len(rows) != batch.total_rows or any(
            r.validation_status != "created" or r.created_object_type != "Asset" for r in rows
        ):
            raise ValidationError("导入结果不完整，不能自动撤销。")
        ids = [r.created_object_id for r in rows]
    else:
        ids = [log.object_id]
    qs = scoped_assets_p1(actor, company).filter(pk__in=ids).order_by("pk")
    if lock:
        qs = qs.select_for_update(of=("self",))
    assets = list(qs.select_related("department", "company"))
    if len(assets) != len(ids) or len(set(ids)) != len(ids):
        raise PermissionDenied("部分资产已不存在或超出当前账号的数据范围。")
    for asset in assets:
        if not can_create_asset_draft(actor, company, asset.department):
            raise PermissionDenied("您没有撤销部分资产的权限。")
        if asset.record_status != "active" or asset.asset_status not in {"draft", "pending_label"}:
            raise ValidationError(f"{asset.asset_name} 已有后续状态变化，不能自动撤销。")
        if is_import and asset.initialization_source != "excel_import":
            raise ValidationError("资产来源与导入批次不一致。")
    ids = [a.pk for a in assets]
    # A new business relation is blocked by default, even when another app is added.
    allowed = {
        "assets.AssetRegistration", "assets.AssetCodeHistory", "assets.AssetQrIdentity",
        "assets.AssetIdentity", "finance.AssetFinance", "finance.AssetDepreciationProfile",
        "assets.AssetCustomValue",
    }
    if not is_import:
        allowed.add("assets.AttachmentLink")  # Existing physical data stays with the draft.
    for model in apps.get_models():
        for field in model._meta.fields:
            if field.is_relation and field.related_model is Asset:
                if model._meta.label in allowed and field.name == "asset":
                    continue
                if model.objects.filter(**{field.attname + "__in": ids}).exists():
                    raise ValidationError(f"存在{model._meta.verbose_name}引用，需先处理后续业务。")
    finances = _related_rows(apps.get_model("finance.AssetFinance"), ids, lock=lock)
    profiles = _related_rows(apps.get_model("finance.AssetDepreciationProfile"), ids, lock=lock)
    if any(f.finance_confirmed_at is not None for f in finances) or any(p.status != "draft" for p in profiles):
        raise ValidationError("已有财务确认或生效折旧配置，不能自动撤销。")
    if is_import and (finances or profiles) and "finance" not in role_names_for(actor):
        raise PermissionDenied("含财务草稿的导入需由有财务权限的人员撤销。")
    registrations = _related_rows(AssetRegistration, ids, lock=lock)
    histories = _related_rows(AssetCodeHistory, ids, lock=lock)
    identities = _related_rows(AssetIdentity, ids, lock=lock)
    qrs = _related_rows(AssetQrIdentity, ids, lock=lock)
    registered = [a for a in assets if a.current_issued_code_id]
    code_ids = [a.current_issued_code_id for a in registered]
    codes_qs = IssuedCode.objects.filter(pk__in=code_ids).order_by("pk")
    if lock:
        codes_qs = codes_qs.select_for_update()
    codes = list(codes_qs)
    if not is_import and (not registered or assets[0].asset_code != log.new_data_json.get("asset_code")):
        raise ValidationError("这条日志已不对应当前建档，请查看最新操作。")
    if len(registrations) != len(registered) or any(
        r.source != "physical" or r.result_issued_code_id not in code_ids for r in registrations
    ):
        raise ValidationError("建档来源或编号已发生变化，不能自动撤销。")
    if len(histories) != len(registered) or any(h.event_type != "issued" for h in histories):
        raise ValidationError("已有编号更正或其他编号历史，不能释放编号。")
    if len(qrs) != len(registered) or any(q.status != "active" or q.label_status != "ready_to_print" for q in qrs):
        raise ValidationError("二维码已打印、贴标或换标，不能释放编号。")
    if any(c.status != "active" or c.company_id != company.pk for c in codes):
        raise ValidationError("编号已作废、更正或不属于当前公司。")
    # Also catch references through QR/profile/identity rather than Asset itself.
    for objects in (qrs, identities, registrations, profiles, finances, codes):
        if not objects:
            continue
        model = type(objects[0])
        pks = [o.pk for o in objects]
        for rel in model._meta.related_objects:
            if rel.related_model in {Asset, AssetRegistration, AssetCodeHistory, AssetIdentity}:
                continue
            if rel.related_model.objects.filter(**{rel.field.attname + "__in": pks}).exists():
                raise ValidationError(f"存在{rel.related_model._meta.verbose_name}引用，不能自动撤销。")

    register_logs = []
    for reg in registrations:
        original = AuditLog.objects.filter(
            company=company, object_type="Asset", object_id=str(reg.asset_id),
            action="asset_register", created_at__gte=reg.registered_at,
        ).order_by("created_at", "pk").first()
        if original is None or OperationUndo.objects.filter(original_log=original).exists():
            raise ValidationError("缺少有效建档操作记录，不能自动撤销。")
        if not is_import and original.pk != log.pk:
            raise ValidationError("请从最新的建档日志办理撤销。")
        register_logs.append(original.pk)

    counters = []
    by_scope = defaultdict(list)
    for code in codes:
        by_scope[(code.scope_key, None if code.scope_key.startswith('{"standard":"asset_identity_v1",') else code.coding_scheme_id)].append(code)
    for (scope, scheme_id), group in sorted(by_scope.items()):
        counter_qs = SequenceCounter.objects.filter(company=company, scope_key=scope)
        if scheme_id is not None:
            counter_qs = counter_qs.filter(coding_scheme_id=scheme_id)
        if lock:
            counter_qs = counter_qs.select_for_update()
        counter = counter_qs.first()
        # Components allocate their subitem from immutable identity rows, not counters.
        if counter is None and all(i.parent_identity_id for i in identities if i.issued_code_id in {c.pk for c in group}):
            components = [i for i in identities if i.issued_code_id in {c.pk for c in group}]
            if components:
                for component in components:
                    if AssetIdentity.objects.filter(parent_identity_id=component.parent_identity_id,
                        subitem_number__gt=component.subitem_number).exclude(pk__in=[i.pk for i in components]).exists():
                        raise ValidationError("同一主资产已有后续组件编号，请先撤销后建档组件。")
                continue
        values = sorted(c.sequence_value for c in group)
        if counter is None or values[-1] != counter.current_value or values != list(range(values[0], values[-1] + 1)):
            raise ValidationError("同一编号范围已有后续建档；请先撤销后建档资产，或一起撤销所属导入批次。")
        if IssuedCode.objects.filter(company=company, scope_key=scope, sequence_value__gte=values[0]).exclude(pk__in=code_ids).exists():
            raise ValidationError("待释放编号范围内还有其他编号，不能回退流水。")
        counters.append({"id": str(counter.pk), "old": counter.current_value, "new": values[0] - 1})

    deletes = {}
    revisions = []
    custom = _related_rows(apps.get_model("assets.AssetCustomValue"), ids, lock=lock)
    delete_objects = [identities, registrations, histories, qrs, codes]
    if is_import:
        delete_objects.extend([profiles, finances, custom, assets])
    for objects in delete_objects:
        if objects:
            deletes[objects[0]._meta.db_table] = [str(o.pk) for o in objects]
    for objects in [assets, finances, profiles, custom, qrs]:
        revisions.extend([{f.attname: getattr(o, f.attname) for f in o._meta.concrete_fields} for o in objects])
    plan = {
        "delete": deletes, "draft_assets": [str(a.pk) for a in registered],
        "counters": counters, "batch_id": str(batch.pk) if batch else "",
        "original_logs": sorted(set(register_logs + [log.pk])),
        "revision": _digest(revisions),
        "registration_keys": [r.idempotency_key for r in registrations],
        "assets": [{"id": str(a.pk), "name": a.asset_name, "code": a.asset_code or a.draft_number} for a in assets],
        "asset_count": len(assets), "released_codes": len(codes),
    }
    return plan


def preview_undo(*, actor, log):
    _require_access(actor, log)
    existing = OperationUndo.objects.filter(original_log=log).first()
    if existing:
        return {"already_undone": True, "undo": existing}
    plan = _plan(actor, log)
    token = signing.dumps({"actor": actor.pk, "log": log.pk, "digest": _digest(plan)}, salt=SALT)
    return {"plan": plan, "confirmation": token, "is_import": bool(plan["batch_id"])}


def _raw_delete(model, ids):
    if not ids:
        return
    # Exactly scoped deletes; database FK constraints still run at commit.
    table = connection.ops.quote_name(model._meta.db_table)
    column = connection.ops.quote_name(model._meta.pk.column)
    values = [model._meta.pk.get_db_prep_value(pk, connection) for pk in ids]
    with connection.cursor() as cursor:
        cursor.execute(f"DELETE FROM {table} WHERE {column} IN ({','.join(['%s'] * len(values))})", values)  # nosec B608
        if cursor.rowcount != len(ids):
            raise ValidationError("撤销对象已变化，操作已回滚。")


@transaction.atomic
def undo_operation(*, actor, log, reason, confirmation, request=None):
    company = _require_access(actor, log)
    explanation = str(reason or "").strip()
    if not explanation or len(explanation) > 1000:
        raise ValidationError("请填写撤销原因（不超过 1000 字）。")
    try:
        signed = signing.loads(confirmation, salt=SALT, max_age=1800)
        if signed["actor"] != actor.pk or signed["log"] != log.pk:
            raise signing.BadSignature
    except (signing.BadSignature, KeyError, TypeError) as exc:
        raise ValidationError("撤销预览已失效，请重新核对。") from exc
    if log.object_type == "ImportBatch":
        from apps.imports.services import _lock_import_namespace
        _lock_import_namespace(company.pk)
    Company.objects.select_for_update().get(pk=company.pk)
    existing = OperationUndo.objects.filter(original_log=log).first()
    if existing:
        return existing
    plan = _plan(actor, log, lock=True)
    if _digest(plan) != signed["digest"]:
        raise ValidationError("预览后资料或编号已有变化，请重新核对；本次未撤销任何记录。")
    reversal = write_business_audit_log(
        company=company, user=actor, action="import_reverse" if plan["batch_id"] else "asset_registration_reverse",
        object_type=log.object_type, object_id=log.object_id,
        old_data={"assets": plan["assets"]},
        new_data={"original_log_id": log.pk, "reason": explanation, "asset_count": plan["asset_count"],
                  "released_codes": plan["released_codes"], "result": "撤销导入" if plan["batch_id"] else "退回草稿"},
        **request_audit_context(request),
    )
    undo = OperationUndo.objects.create(original_log=log, reversal_log=reversal, plan_json=plan)
    for original_id in plan["original_logs"]:
        if original_id != log.pk:
            OperationUndo.objects.create(original_log_id=original_id, reversal_log=reversal)
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SELECT set_config('eam_lite.operation_undo', %s, true)", [str(undo.pk)])
    now = timezone.now()
    for asset_id in plan["draft_assets"]:
        _controlled_update(Asset, pk=asset_id, values={"asset_status": "draft", "asset_code": None,
            "current_issued_code_id": None, "submitted_by_id": None, "submitted_at": None,
            "updated_by_id": actor.pk, "updated_at": now})
    for label in ("assets.AssetIdentity", "assets.AssetRegistration", "assets.AssetCodeHistory", "assets.AssetQrIdentity",
                  "masterdata.IssuedCode", "finance.AssetDepreciationProfile", "finance.AssetFinance", "assets.AssetCustomValue"):
        model = apps.get_model(label)
        _raw_delete(model, plan["delete"].get(model._meta.db_table, []))
    for counter in plan["counters"]:
        SequenceCounter.objects.filter(pk=counter["id"]).update(current_value=counter["new"], updated_at=now)
    if plan["batch_id"]:
        for asset in plan["assets"]:
            _controlled_delete_asset_draft(pk=asset["id"])
        ImportBatch.objects.filter(pk=plan["batch_id"]).update(status="reversed")
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
            cursor.execute("SELECT set_config('eam_lite.operation_undo', '', true)")
            cursor.execute("SET CONSTRAINTS ALL DEFERRED")
    return undo


def registration_key_was_undone(company, key):
    return any(key in plan.get("registration_keys", []) for plan in
               OperationUndo.objects.filter(original_log__company=company).exclude(plan_json={}).values_list("plan_json", flat=True))
