from django.urls import path

from apps.assets import views
from apps.assets.lifecycle_urls import urlpatterns as lifecycle_urlpatterns
from apps.assets.qr_urls import urlpatterns as qr_urlpatterns


app_name = "assets"

urlpatterns = [
    path("", views.asset_list, name="asset-list"),
    path("new/", views.asset_create, name="asset-create"),
    path("<uuid:pk>/", views.asset_detail, name="asset-detail"),
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
