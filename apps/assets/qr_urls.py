"""Sprint 6 QR label URL patterns, appended under the assets namespace."""

from django.urls import path

from apps.assets import qr_views


urlpatterns = [
    path("labels/", qr_views.label_queue, name="label-queue"),
    path("labels/batches/", qr_views.label_batch_list, name="label-batch-list"),
    path(
        "labels/batches/<uuid:pk>/",
        qr_views.label_batch_detail,
        name="label-batch-detail",
    ),
    path(
        "labels/batches/<uuid:pk>/print/",
        qr_views.label_batch_print,
        name="label-batch-print",
    ),
    path(
        "labels/batches/<uuid:pk>/confirm/",
        qr_views.label_batch_confirm,
        name="label-batch-confirm",
    ),
    path(
        "labels/batches/<uuid:pk>/cancel/",
        qr_views.label_batch_cancel,
        name="label-batch-cancel",
    ),
    path(
        "labels/items/<uuid:pk>/qr.svg",
        qr_views.label_item_qr_svg,
        name="label-item-qr",
    ),
    path("scan/<str:token>/", qr_views.qr_scan, name="qr-scan"),
    path("scan/<str:token>/confirm/", qr_views.qr_attach, name="qr-attach"),
    path(
        "scan-cover/<uuid:pk>/",
        qr_views.qr_scan_cover,
        name="qr-scan-cover",
    ),
    path("<uuid:pk>/labels/rotate/", qr_views.qr_rotate, name="qr-rotate"),
]
