from django.urls import path

from apps.maintenance import views


app_name = "maintenance"

urlpatterns = [
    path("", views.plan_list, name="plan-list"),
    path("due/", views.due_list, name="due-list"),
    path("records/", views.record_list, name="record-list"),
    path("problems/", views.problem_list, name="problem-list"),
    path("plans/new/", views.plan_create, name="plan-create"),
    path("plans/<uuid:pk>/", views.plan_detail, name="plan-detail"),
    path("plans/<uuid:pk>/edit/", views.plan_edit, name="plan-edit"),
    path("plans/<uuid:pk>/status/", views.plan_status, name="plan-status"),
    path("plans/<uuid:pk>/complete/", views.plan_complete, name="plan-complete"),
    path("records/<uuid:pk>/", views.record_detail, name="record-detail"),
    path("records/<uuid:pk>/void/", views.record_void, name="record-void"),
    path("records/<uuid:pk>/redo/", views.record_redo, name="record-redo"),
    path("problems/<uuid:pk>/close/", views.problem_close, name="problem-close"),
    path("<str:target_type>/<uuid:target_pk>/attachments/upload/", views.attachment_upload, name="attachment-upload"),
    path("attachments/<uuid:pk>/download/", views.attachment_download, name="attachment-download"),
    path("attachments/<uuid:pk>/void/", views.attachment_void, name="attachment-void"),
]
