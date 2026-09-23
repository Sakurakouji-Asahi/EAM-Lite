from django.urls import path

from apps.assets import views
from apps.assets.identity_views import asset_code_preview, identification_confirm
from apps.assets.trace_views import asset_trace_edit
from apps.assets.custody_views import custody_return
from apps.assets.bulk_views import bulk_registration
from apps.assets.correction_views import reverse_trace
from apps.assets.lifecycle_urls import urlpatterns as lifecycle_urlpatterns
from apps.assets.qr_urls import urlpatterns as qr_urlpatterns


app_name = "assets"

urlpatterns = [
    path("", views.asset_list, name="asset-list"),
    path("new/", views.asset_create, name="asset-create"),
    path("bulk-register/", bulk_registration, name="bulk-registration"),
    path("code-preview/", asset_code_preview, name="asset-code-preview"),
    path("export/", views.asset_list_export, name="asset-list-export"),
    path("<uuid:pk>/", views.asset_detail, name="asset-detail"),
    path("<uuid:pk>/identification/", identification_confirm, name="identification-confirm"),
    path("<uuid:pk>/origin/", asset_trace_edit, {"kind": "origin"}, name="asset-origin-edit"),
    path("<uuid:pk>/composition/", asset_trace_edit, {"kind": "composition"}, name="asset-composition-edit"),
    path("<uuid:pk>/custody-return/", custody_return, name="asset-custody-return"),
    path("<uuid:pk>/custody-returns/<uuid:record_pk>/reverse/", reverse_trace, {"kind": "custody"}, name="custody-return-reverse"),
    path("<uuid:pk>/origins/<uuid:record_pk>/reverse/", reverse_trace, {"kind": "origin"}, name="origin-reverse"),
    path("<uuid:pk>/edit/", views.asset_edit, name="asset-edit"),
    path("<uuid:pk>/submit/", views.asset_submit, name="asset-submit"),
    path("<uuid:pk>/withdraw/", views.asset_withdraw, name="asset-withdraw"),
    path("<uuid:pk>/delete/", views.asset_delete, name="asset-delete"),
    path(
        "<uuid:pk>/requested-scheme/",
        views.requested_scheme,
        name="asset-requested-scheme",
    ),
    path(
        "<uuid:pk>/attachments/upload/",
        views.attachment_upload,
        name="attachment-upload",
    ),
    path(
        "<uuid:asset_pk>/attachments/<uuid:pk>/download/",
        views.attachment_download,
        name="attachment-download",
    ),
    path(
        "<uuid:asset_pk>/attachments/<uuid:pk>/void/",
        views.attachment_void,
        name="attachment-void",
    ),
]

urlpatterns += qr_urlpatterns
urlpatterns += lifecycle_urlpatterns
