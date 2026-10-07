"""Render permission configuration errors without discarding safe form input."""
from urllib.parse import urlencode, urlsplit

from django.shortcuts import render
from django.urls import reverse

from apps.accounts.roles import ROLE_LABELS
from apps.masterdata.forms import ScopeAssignForm, ScopeRevokeForm, UserRoleForm
from apps.masterdata.models import UserDepartmentScope
from apps.masterdata.permissions import assigned_role_names_for


def permission_list_return_url(request):
    fallback = reverse("masterdata:user-permissions-list")
    value = request.POST.get("return_to", request.GET.get("return_to", ""))
    if not value or len(value) > 3000 or "\\" in value or any(ord(char) < 32 for char in value):
        return fallback
    try:
        parts = urlsplit(value)
    except ValueError:
        return fallback
    if parts.scheme or parts.netloc or parts.path != fallback:
        return fallback
    return value


def permission_detail_url(request, target):
    return reverse("masterdata:user-permissions-detail", args=[target.pk]) + "?" + urlencode(
        {"return_to": permission_list_return_url(request)})


def _input_ids(form, prefix):
    # These pages contain several forms with a reason field. Names stay unchanged.
    for name, field in form.fields.items():
        field.widget.attrs["id"] = f"{prefix}-{name}"
    return form


def render_permissions_detail(request, company, target, *, role_form=None,
                              scope_form=None, revoke_form=None, revoke_scope_id=None,
                              failed_form=None, failed_action=""):
    names = sorted(assigned_role_names_for(target))
    if role_form is None:
        role_form = UserRoleForm(actor=request.user, initial={"roles": names})
    if scope_form is None:
        scope_form = ScopeAssignForm(actor=request.user, company=company)
    _input_ids(role_form, "permission-roles")
    _input_ids(scope_form, "permission-scope")
    scopes = list(UserDepartmentScope.objects.filter(
        company=company, user=target, is_active=True,
    ).select_related("department").order_by("department__normalized_code", "pk"))
    scope_rows = []
    for scope in scopes:
        form = revoke_form if scope.pk == revoke_scope_id else ScopeRevokeForm(actor=request.user)
        _input_ids(form, f"permission-revoke-{scope.pk}")
        scope_rows.append({"scope": scope, "form": form})
    return render(request, "masterdata/user_permissions_detail.html", {
        "target_user": target, "role_form": role_form, "scope_form": scope_form,
        "scopes": scopes, "scope_rows": scope_rows,
        "assigned_roles": [ROLE_LABELS[name] for name in names],
        "finance_fields_visible": "finance" in names,
        "return_url": permission_list_return_url(request),
        "return_query": urlencode({"return_to": permission_list_return_url(request)}),
        "failed_form": failed_form, "failed_action": failed_action,
    })
