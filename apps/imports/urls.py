from django.urls import path

from apps.imports import views


app_name = "imports"

urlpatterns = [
    path("", views.import_home, name="home"),
    path("<str:import_type>/template.xlsx", views.download_template, name="template"),
    path("<str:import_type>/upload/", views.upload_import, name="upload"),
    path("batches/<int:pk>/", views.batch_detail, name="batch_detail"),
    path("batches/<int:pk>/confirm/", views.confirm_batch, name="confirm"),
    path("batches/<int:pk>/source/", views.download_source, name="source"),
]
