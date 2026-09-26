from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.shortcuts import redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

from apps.assets.bulk_support import MAX_BULK_ASSETS
from apps.finance.bulk_confirmation import preview_bulk_finance, confirm_bulk_finance
from apps.finance.forms import PendingFinanceFilterForm
from apps.finance.permissions import require_manage_finance, scoped_finance_assets
from apps.finance.readiness import filter_pending_finance_assets
from apps.finance.views import _company


@never_cache
@login_required
@require_http_methods(["GET","POST"])
def bulk_finance_confirmation(request):
    require_manage_finance(request.user)
    company = _company()
    if request.method == "GET":
        return redirect("finance:pending-list")
    context = {"preview":None,"result":None,"error":"","cleared_asset_ids":[],
        "selection_key":f"eam-bulk-finance:{company.pk}:{request.user.pk}:{request.POST.get('import_batch','')}"}
    try:
        if request.POST.get("action") == "confirm":
            result = confirm_bulk_finance(actor=request.user,company=company,token=request.POST.get("token"),
                acknowledged=request.POST.get("acknowledge")=="on",request=request)
            context["result"] = result
            context["cleared_asset_ids"] = [str(row["asset"].pk) for row in result["rows"] if not row["error"]]
        elif request.POST.get("action") == "preview":
            ids = request.POST.getlist("assets")
            if request.POST.get("selection_scope") == "filtered":
                form = PendingFinanceFilterForm(request.POST,company=company)
                if not form.is_valid():
                    raise ValidationError("筛选条件无效，请返回财务待办重新选择。")
                queryset = filter_pending_finance_assets(scoped_finance_assets(request.user,company),form.cleaned_data)
                ids = list(queryset.order_by("submitted_at","created_at","pk").values_list("pk",flat=True)[:MAX_BULK_ASSETS+1])
            context["preview"] = preview_bulk_finance(actor=request.user,company=company,asset_ids=ids)
            context["import_batch"] = request.POST.get("import_batch","")
        else:
            raise ValidationError("未知的批量财务操作。")
    except (ValidationError,ValueError) as exc:
        context["error"] = "；".join(exc.messages) if isinstance(exc,ValidationError) else str(exc)
    response = render(request,"finance/bulk_confirmation.html",context,status=400 if context["error"] else 200)
    response["Cache-Control"] = "private, no-store"
    return response
