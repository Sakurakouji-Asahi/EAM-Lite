from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from apps.coding.presets import install_standard_coding_rule
from apps.coding.standard import CATEGORY_DEFAULTS, MANAGEMENT_CHOICES
from apps.masterdata.permissions import current_company, require_manage_masterdata


class StandardCodingSetupForm(forms.Form):
    include_categories = forms.BooleanField(
        label="同时建立文档中的 10 个实物大类", required=False, initial=True,
        help_text="只补充缺少的分类；现有分类有冲突时会提示核对。",
    )


@login_required
@require_http_methods(["GET", "POST"])
def standard_coding_setup(request):
    require_manage_masterdata(request.user, "coding_scheme")
    company = current_company()
    if company is None:
        raise Http404("请先配置公司。")
    form = StandardCodingSetupForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            scheme = install_standard_coding_rule(actor=request.user, company=company,
                include_categories=form.cleaned_data["include_categories"], request=request)
        except ValidationError as exc:
            form.add_error(None, "；".join(exc.messages))
        else:
            messages.success(request, "统一编码规则已采用。新资产按管理属性、实物大类和取得年份分别编号。")
            return redirect("masterdata:coding-scheme-detail", pk=scheme.pk)
    return render(request, "masterdata/standard_coding_setup.html", {
        "form": form, "attributes": MANAGEMENT_CHOICES, "categories": CATEGORY_DEFAULTS,
    })
