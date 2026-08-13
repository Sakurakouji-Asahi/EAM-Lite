from django.urls import path

from apps.inventory import views


app_name = "inventory"

urlpatterns = [
    path("", views.task_list, name="task-list"),
    path("new/", views.task_create, name="task-create"),
    path("tasks/<uuid:pk>/", views.task_detail, name="task-detail"),
    path("tasks/<uuid:pk>/edit/", views.task_edit, name="task-edit"),
    path("tasks/<uuid:pk>/publish/", views.task_publish, name="task-publish"),
    path("tasks/<uuid:pk>/scan/", views.task_scan_entry, name="task-scan"),
    path(
        "tasks/<uuid:pk>/scan/context/<str:context_key>/",
        views.task_scan_context,
        name="task-scan-context",
    ),
    path("tasks/<uuid:pk>/stop/", views.task_stop, name="task-stop"),
    path("tasks/<uuid:pk>/close/", views.task_close, name="task-close"),
    path("tasks/<uuid:pk>/cancel/", views.task_cancel, name="task-cancel"),
    path(
        "tasks/<uuid:task_pk>/rows/<uuid:pk>/supplement/",
        views.task_supplement,
        name="task-supplement",
    ),
    path(
        "tasks/<uuid:task_pk>/rows/<uuid:pk>/resolve/",
        views.task_resolve,
        name="task-resolve",
    ),
    path(
        "tasks/<uuid:task_pk>/resolutions/<uuid:pk>/correct/",
        views.resolution_correct,
        name="resolution-correct",
    ),
    path(
        "tasks/<uuid:pk>/surpluses/new/",
        views.surplus_create,
        name="surplus-create",
    ),
    path(
        "tasks/<uuid:task_pk>/surpluses/<uuid:pk>/",
        views.surplus_detail,
        name="surplus-detail",
    ),
    path(
        "tasks/<uuid:task_pk>/surpluses/<uuid:pk>/resolve/",
        views.surplus_resolve,
        name="surplus-resolve",
    ),
    path(
        "tasks/<uuid:task_pk>/surpluses/<uuid:pk>/convert/",
        views.surplus_convert,
        name="surplus-convert",
    ),
    path(
        "tasks/<uuid:task_pk>/<str:target_type>/<uuid:target_pk>/attachments/upload/",
        views.attachment_upload,
        name="attachment-upload",
    ),
    path(
        "tasks/<uuid:task_pk>/attachments/<uuid:pk>/download/",
        views.attachment_download,
        name="attachment-download",
    ),
    path(
        "tasks/<uuid:task_pk>/attachments/<uuid:pk>/void/",
        views.attachment_void,
        name="attachment-void",
    ),
]
