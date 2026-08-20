from django.urls import path

from apps.finance import views


app_name = "finance"

urlpatterns = [
    path("pending/", views.pending_finance_list, name="pending-list"),
    path("pending/<uuid:pk>/", views.finance_confirm, name="finance-confirm"),
    path("pending/<uuid:pk>/preview/", views.finance_preview, name="finance-preview"),
    path("assets/<uuid:pk>/", views.asset_finance_detail, name="asset-finance-detail"),
    path("assets/<uuid:pk>/usage/", views.work_usage, name="work-usage"),
    path("assets/<uuid:pk>/event/", views.profile_event, name="profile-event"),
    path("assets/<uuid:pk>/adjustment/", views.value_adjustment, name="value-adjustment"),
    path("assets/<uuid:pk>/adjustment/<uuid:adjustment_pk>/reverse/", views.value_adjustment_reverse, name="value-adjustment-reverse"),
    path("assets/<uuid:pk>/profile-version/", views.profile_version, name="profile-version"),
    path(
        "profiles/<uuid:profile_pk>/continuation-review/",
        views.profile_continuation_review,
        name="profile-continuation-review",
    ),
    path("assets/<uuid:pk>/theoretical/", views.theoretical_run, name="theoretical-run"),
    path("assets/<uuid:pk>/theoretical/<uuid:run_pk>/", views.theoretical_detail, name="theoretical-detail"),
    path("policies/", views.policy_list, name="policy-list"),
    path("policies/new/", views.policy_form, name="policy-create"),
    path("policies/<uuid:pk>/", views.policy_detail, name="policy-detail"),
    path("policies/<uuid:pk>/edit/", views.policy_form, name="policy-edit"),
    path("policies/<uuid:pk>/<str:action>/", views.policy_action, name="policy-action"),
    path("category-policy/", views.category_policy_form, name="category-policy"),
    path("fixed-categories/", views.fixed_category_list, name="fixed-category-list"),
    path("fixed-categories/new/", views.fixed_category_form, name="fixed-category-create"),
    path("fixed-categories/<int:pk>/edit/", views.fixed_category_form, name="fixed-category-edit"),
    path("fixed-categories/<int:pk>/deactivate/", views.fixed_category_deactivate, name="fixed-category-deactivate"),
    path("settings/", views.finance_settings, name="settings"),
    path("batches/", views.batch_list, name="batch-list"),
    path("batches/new/", views.batch_generate, name="batch-generate"),
    path("batches/<uuid:pk>/", views.batch_detail, name="batch-detail"),
    path("batches/<uuid:pk>/confirm/", views.batch_confirm, name="batch-confirm"),
    path("batches/<uuid:pk>/reverse/", views.batch_reverse, name="batch-reverse"),
]
