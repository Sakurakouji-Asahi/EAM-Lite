from django.urls import path

from apps.reports import views


app_name = "reports"

urlpatterns = [
    path("", views.report_center, name="report-center"),
    path("export/", views.report_export, name="report-export"),
    path("exports/<uuid:pk>/", views.export_detail, name="export-detail"),
    path("exports/<uuid:pk>/download/", views.export_download, name="export-download"),
    path("tplus/", views.tplus_export, name="tplus-export"),
    path("external-references/", views.external_reference_list, name="external-reference-list"),
    path("external-references/<uuid:asset_pk>/", views.external_reference_edit, name="external-reference-edit"),
]
