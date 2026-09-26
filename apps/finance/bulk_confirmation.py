"""Preview saved finance values, then confirm independently with durable replay."""
import hashlib
import json
import uuid
from decimal import Decimal

from django.core import signing
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone

from apps.assets.bulk_support import MAX_BULK_ASSETS, normalize_asset_selection
from apps.assets.models import Asset
from apps.finance.confirmation_initial import finance_confirmation_initial
from apps.finance.domain import depreciation_position
from apps.finance.forms import FinanceDraftForm
from apps.finance.models import AssetFinance, AssetDepreciationProfile, FinanceFormalizationRequest, DepreciationMethod
from apps.finance.permissions import require_manage_finance, scoped_finance_assets
from apps.finance.readiness import finance_confirmation_pending
from apps.finance.services import (
    _get_warning_amount, _profile_spec, _require_current_company, _validate_asset_physical,
    confirm_asset_finance, resolve_depreciation_policy,
)
from apps.masterdata.models import Company, FixedAssetCategory

SALT = "finance.bulk-confirmation.v1"
REASON = "批量核对会计认定、期初余额与折旧参数"


def _values(instance):
    return {f.attname: getattr(instance, f.attname) for f in instance._meta.concrete_fields} if instance else None


def _snapshot(value):
    return hashlib.sha256(json.dumps(value,cls=DjangoJSONEncoder,sort_keys=True).encode()).hexdigest()


def _prepare(*, actor, asset, lock=False):
    if not asset.current_issued_code_id:
        raise ValidationError("请先完成实物建档，再办理批量财务确认。")
    _validate_asset_physical(asset)
    finance_qs = AssetFinance.objects.filter(company=asset.company, asset=asset)
    profiles_qs = AssetDepreciationProfile.objects.filter(company=asset.company,asset=asset,status="draft").order_by("version")
    if lock:
        finance_qs, profiles_qs = finance_qs.select_for_update(), profiles_qs.select_for_update()
    finance, profiles = finance_qs.first(), list(profiles_qs)
    if finance is None:
        raise ValidationError("尚未保存财务资料，请先补充会计认定和原值。")
    if len(profiles)>1:
        raise ValidationError("存在多份未确认折旧草稿，请先核对。")
    initial = finance_confirmation_initial(asset)
    form = FinanceDraftForm(initial,actor=actor,company=asset.company,asset=asset)
    if not form.is_valid():
        raise ValidationError([f"{form.fields[key].label if key in form.fields else '财务资料'}：{message}"
                               for key,errors in form.errors.items() for message in errors])
    finance_data, profile_data = form.finance_data(), form.profile_data()
    category = finance_data.get("fixed_asset_category")
    if category and lock:
        category = FixedAssetCategory.objects.select_for_update().get(pk=category.pk,company=asset.company)
        if not category.is_active:
            raise ValidationError("固定资产会计类别已停用，请重新核对。")
        finance_data["fixed_asset_category"] = category
    threshold = _get_warning_amount(asset.company)
    policy = None
    details = {"cost": finance_data["original_cost"], "accumulated": Decimal("0.00"),
               "impairment": Decimal("0.00"), "book": finance_data["original_cost"],
               "salvage": None, "progress": "不计提折旧", "policy": None}
    if finance_data["accounting_treatment"] == "fixed_asset":
        if profiles:
            profile_data["opening_book_value"] = profiles[0].opening_book_value
            profile_data["change_reason"] = profiles[0].change_reason
            if profiles[0].actual_continuation_review_required:
                raise ValidationError("实际接续日尚待复核，请先核对单项折旧资料。")
        policy = resolve_depreciation_policy(asset=asset,requested_policy=profile_data.get("depreciation_policy"),lock=lock)
        # Pin the version actually reviewed, even when it came from a default.
        # A concurrent change of the category default must not change this row.
        profile_data["depreciation_policy"] = policy
        _, result, resolved = _profile_spec(asset=asset,finance_data=finance_data,profile_data=profile_data,policy=policy)
        salvage = result["salvage_value"] if isinstance(result,dict) else result.salvage_value
        position = depreciation_position(original_cost=finance_data["original_cost"],
            accumulated_depreciation=resolved["opening_actual_accumulated_depreciation"],
            impairment_balance=resolved["opening_impairment"],salvage_value=salvage)
        details.update({"accumulated":resolved["opening_actual_accumulated_depreciation"],
            "impairment":resolved["opening_impairment"],"book":resolved["opening_book_value"],
            "salvage":salvage,"life":resolved["useful_life_months"],
            "start":resolved["start_date"],"continuation":resolved["actual_continuation_date"],
            "method":dict(DepreciationMethod.choices)[resolved["method"]],"policy":policy,
            "progress":("不计提折旧" if resolved["method"]=="no_depreciation" else
                {"fully_depreciated":"已提足折旧","not_fully_depreciated":"未提足折旧",
                 "no_depreciable_balance":"无剩余可折旧金额"}[position.status])})
    else:
        if profiles:
            raise ValidationError("已有固定资产折旧草稿，请先核对会计认定。")
        if finance_data["original_cost"]>=threshold and not finance_data["accounting_treatment_reason"].strip():
            raise ValidationError("原值达到认定提示金额，请补充受控非固定资产的认定说明。")
    snapshot = _snapshot({"date":timezone.localdate(),"asset":_values(asset),"finance":_values(finance),
        "profiles":[_values(p) for p in profiles],"policy":_values(policy),"category":_values(category),
        "threshold":threshold,"details":{k:v for k,v in details.items() if k!="policy"}})
    return {"finance_data":finance_data,"profile_data":profile_data,"snapshot":snapshot,
            "details":details,"treatment":finance.get_accounting_treatment_display(),"category":category}


def preview_bulk_finance(*,actor,company,asset_ids):
    require_manage_finance(actor)
    company = _require_current_company(company)
    ids = normalize_asset_selection(asset_ids)
    assets = {str(a.pk):a for a in scoped_finance_assets(actor,company).filter(pk__in=ids).select_related(
        "company","category","department","location","responsible_employee")}
    if len(assets)!=len(ids):
        raise PermissionDenied("所选资产包含其他公司或无权办理的记录。")
    rows, ready = [], []
    totals = {key:Decimal("0.00") for key in ("cost","accumulated","impairment","book")}
    for pk in ids:
        asset = assets[pk]
        row = {"asset":asset,"error":"","can_edit":finance_confirmation_pending(asset)}
        try:
            prepared = _prepare(actor=actor,asset=asset)
            row.update(prepared)
            ready.append({"id":pk,"snapshot":prepared["snapshot"]})
            for key in totals:
                totals[key] += prepared["details"][key]
        except (ValidationError,ValueError) as exc:
            row["error"] = "；".join(exc.messages) if isinstance(exc,ValidationError) else str(exc)
        rows.append(row)
    payload = {"version":1,"actor":str(actor.pk),"company":str(company.pk),"batch":str(uuid.uuid4()),"rows":ready}
    return {"rows":rows,"ready_count":len(ready),"error_count":len(rows)-len(ready),"totals":totals,
            "token":signing.dumps(payload,salt=SALT,compress=True) if ready else ""}


def confirm_bulk_finance(*,actor,company,token,acknowledged,request=None):
    require_manage_finance(actor)
    company = _require_current_company(company)
    if acknowledged is not True:
        raise ValidationError("请先勾选已核对本批资产的会计认定、金额和折旧设置。")
    try:
        payload = signing.loads(token,salt=SALT,max_age=3600)
        if (not isinstance(payload,dict) or payload.get("version")!=1 or payload.get("actor")!=str(actor.pk)
            or payload.get("company")!=str(company.pk)):
            raise signing.BadSignature
        batch = str(uuid.UUID(payload["batch"]))
        rows = payload["rows"]
        if not isinstance(rows,list) or not 1<=len(rows)<=MAX_BULK_ASSETS:
            raise signing.BadSignature
        ids = [str(uuid.UUID(row["id"])) for row in rows]
        if len(ids)!=len(set(ids)) or any(not isinstance(row.get("snapshot"),str) for row in rows):
            raise signing.BadSignature
    except (signing.BadSignature,ValueError,KeyError,TypeError,AttributeError) as exc:
        raise ValidationError("批量确认预览已失效或不属于当前账号，请重新核对。") from exc
    results = []
    for row in rows:
        asset, replay = None, False
        try:
            with transaction.atomic():
                locked_company = Company.objects.select_for_update().get(pk=company.pk)
                _require_current_company(locked_company)
                asset = scoped_finance_assets(actor,locked_company,Asset.objects.select_for_update(of=("self",))).select_related(
                    "company","category","department","location","responsible_employee").get(pk=row["id"])
                key = f"bulk-finance:{batch}:{asset.pk}"
                replay = FinanceFormalizationRequest.objects.filter(company=locked_company,asset=asset,
                    idempotency_key=key,status="completed",created_by=actor).exists()
                if not replay:
                    prepared = _prepare(actor=actor,asset=asset,lock=True)
                    if prepared["snapshot"]!=row["snapshot"]:
                        raise ValidationError("预览后资料或折旧政策已有变化，本项未确认，请重新核对。")
                    asset = confirm_asset_finance(actor=actor,asset=asset,finance_data=prepared["finance_data"],
                        profile_data=prepared["profile_data"],idempotency_key=key,reason=REASON,request=request)
            results.append({"asset":asset,"error":"","replayed":replay})
        except (PermissionDenied,Asset.DoesNotExist):
            results.append({"asset":None,"error":"资产已不存在或当前账号已无办理权限。","replayed":False})
        except (ValidationError,ValueError) as exc:
            results.append({"asset":asset,"error":"；".join(exc.messages) if isinstance(exc,ValidationError) else str(exc),"replayed":False})
    return {"rows":results,"success_count":sum(not r["error"] for r in results),
            "error_count":sum(bool(r["error"]) for r in results)}
