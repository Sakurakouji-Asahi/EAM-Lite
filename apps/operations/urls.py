from django.urls import path

from apps.operations import views


app_name = "operations"

urlpatterns = [
    path("backups/", views.backup_list, name="backup-list"),
    path("backups/new/", views.backup_create, name="backup-create"),
    path("backups/<uuid:pk>/", views.backup_detail, name="backup-detail"),
    path(
        "backups/<uuid:pk>/authorize-download/",
        views.backup_authorize_download,
        name="backup-authorize-download",
    ),
    path(
        "backups/download/<uuid:grant_pk>/",
        views.backup_download,
        name="backup-download",
    ),
]
