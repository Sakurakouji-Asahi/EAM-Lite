from django.urls import path

from apps.assets import lifecycle_views


urlpatterns = [
    path("<uuid:pk>/lifecycle/transfer/", lifecycle_views.asset_transfer, name="lifecycle-transfer"),
    path("<uuid:pk>/lifecycle/idle/", lifecycle_views.asset_idle, name="lifecycle-idle"),
    path("<uuid:pk>/lifecycle/activate/", lifecycle_views.asset_activate, name="lifecycle-activate"),
    path("<uuid:pk>/lifecycle/repair-start/", lifecycle_views.asset_repair_start, name="lifecycle-repair-start"),
    path("<uuid:pk>/lifecycle/repair-complete/", lifecycle_views.asset_repair_complete, name="lifecycle-repair-complete"),
    path("<uuid:pk>/lifecycle/loan/", lifecycle_views.asset_loan, name="lifecycle-loan"),
    path("<uuid:pk>/lifecycle/loan-return/", lifecycle_views.asset_loan_return, name="lifecycle-loan-return"),
    path("<uuid:pk>/lifecycle/code-correct/", lifecycle_views.asset_code_correct, name="lifecycle-code-correct"),
    path("<uuid:pk>/lifecycle/archive/", lifecycle_views.asset_archive, name="lifecycle-archive"),
    path("<uuid:pk>/lifecycle/restore/", lifecycle_views.asset_restore, name="lifecycle-restore"),
    path("<uuid:pk>/disposals/start/", lifecycle_views.disposal_start, name="disposal-start"),
    path("disposals/<uuid:pk>/", lifecycle_views.disposal_detail, name="disposal-detail"),
    path("disposals/<uuid:pk>/actual/", lifecycle_views.disposal_actual, name="disposal-actual"),
    path("disposals/<uuid:pk>/finance-lock/", lifecycle_views.disposal_finance_lock, name="disposal-finance-lock"),
    path("disposals/<uuid:pk>/cancel/", lifecycle_views.disposal_cancel, name="disposal-cancel"),
    path("disposals/<uuid:pk>/complete/", lifecycle_views.disposal_complete, name="disposal-complete"),
    path("disposals/<uuid:pk>/reverse/", lifecycle_views.disposal_reverse, name="disposal-reverse"),
    path("disposals/<uuid:pk>/attachments/upload/", lifecycle_views.disposal_attachment_upload, name="disposal-attachment-upload"),
    path("disposals/<uuid:disposal_pk>/attachments/<uuid:pk>/download/", lifecycle_views.disposal_attachment_download, name="disposal-attachment-download"),
    path("disposals/<uuid:disposal_pk>/attachments/<uuid:pk>/void/", lifecycle_views.disposal_attachment_void, name="disposal-attachment-void"),
]
