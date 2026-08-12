from django.urls import path

from apps.masterdata import views


app_name = "masterdata"

urlpatterns = [
    path("setup/", views.setup_overview, name="setup"),
    path("setup/<int:step>/", views.setup_step, name="setup-step"),
    path("companies/", views.company_list, name="company-list"),
    path("companies/new/", views.company_create_view, name="company-create"),
    path("companies/<int:pk>/", views.company_detail, name="company-detail"),
    path("companies/<int:pk>/edit/", views.company_edit, name="company-edit"),
    path(
        "companies/<int:pk>/status/",
        views.status_change,
        {"resource": "company"},
        name="company-status",
    ),
    path("departments/", views.department_list, name="department-list"),
    path("departments/new/", views.department_create_view, name="department-create"),
    path("departments/<int:pk>/", views.department_detail, name="department-detail"),
    path("departments/<int:pk>/edit/", views.department_edit, name="department-edit"),
    path(
        "departments/<int:pk>/status/",
        views.status_change,
        {"resource": "department"},
        name="department-status",
    ),
    path("employees/", views.employee_list, name="employee-list"),
    path("employees/new/", views.employee_create_view, name="employee-create"),
    path("employees/<int:pk>/", views.employee_detail, name="employee-detail"),
    path("employees/<int:pk>/edit/", views.employee_edit, name="employee-edit"),
    path(
        "employees/<int:pk>/user/",
        views.employee_user_link,
        name="employee-user-link",
    ),
    path(
        "employees/<int:pk>/status/",
        views.status_change,
        {"resource": "employee"},
        name="employee-status",
    ),
    path("locations/", views.location_list, name="location-list"),
    path("locations/new/", views.location_create_view, name="location-create"),
    path("locations/<int:pk>/", views.location_detail, name="location-detail"),
    path("locations/<int:pk>/edit/", views.location_edit, name="location-edit"),
    path(
        "locations/<int:pk>/status/",
        views.status_change,
        {"resource": "location"},
        name="location-status",
    ),
    path("categories/", views.category_list, name="category-list"),
    path("coding-schemes/", views.coding_scheme_list, name="coding-scheme-list"),
    path("coding-schemes/new/", views.coding_scheme_create, name="coding-scheme-create"),
    path("coding-schemes/<int:pk>/", views.coding_scheme_detail, name="coding-scheme-detail"),
    path("coding-schemes/<int:pk>/edit/", views.coding_scheme_edit, name="coding-scheme-edit"),
    path(
        "coding-schemes/<int:pk>/activate/",
        views.coding_scheme_action,
        {"action": "activate"},
        name="coding-scheme-activate",
    ),
    path(
        "coding-schemes/<int:pk>/retire/",
        views.coding_scheme_action,
        {"action": "retire"},
        name="coding-scheme-retire",
    ),
    path(
        "coding-schemes/<int:pk>/set-default/",
        views.coding_scheme_action,
        {"action": "default"},
        name="coding-scheme-set-default",
    ),
    path(
        "coding-schemes/<int:pk>/clone/",
        views.coding_scheme_action,
        {"action": "clone"},
        name="coding-scheme-clone",
    ),
    path("categories/new/", views.category_create_view, name="category-create"),
    path("categories/<int:pk>/", views.category_detail, name="category-detail"),
    path("categories/<int:pk>/edit/", views.category_edit, name="category-edit"),
    path(
        "categories/<int:pk>/status/",
        views.status_change,
        {"resource": "asset_category"},
        name="category-status",
    ),
    path("system-settings/", views.system_settings, name="system-settings"),
    path("user-permissions/", views.user_permissions_list, name="user-permissions-list"),
    path(
        "user-permissions/<int:user_id>/",
        views.user_permissions_detail,
        name="user-permissions-detail",
    ),
    path(
        "user-permissions/<int:user_id>/roles/",
        views.user_roles_update,
        name="user-roles-update",
    ),
    path(
        "user-permissions/<int:user_id>/scopes/",
        views.user_scope_assign,
        name="user-scope-assign",
    ),
    path(
        "user-permissions/<int:user_id>/scopes/<int:scope_id>/revoke/",
        views.user_scope_revoke,
        name="user-scope-revoke",
    ),
]
