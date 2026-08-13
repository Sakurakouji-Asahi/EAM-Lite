from django.urls import path

from apps.offboarding import views


app_name = "offboarding"

urlpatterns = [
    path("", views.clearance_list, name="clearance-list"),
    path("initiate/", views.clearance_initiate, name="clearance-initiate"),
    path("<uuid:pk>/", views.clearance_detail, name="clearance-detail"),
    path("<uuid:pk>/refresh/", views.clearance_refresh, name="clearance-refresh"),
    path(
        "<uuid:pk>/supplement/",
        views.clearance_supplement,
        name="clearance-supplement",
    ),
    path("<uuid:pk>/complete/", views.clearance_complete, name="clearance-complete"),
    path(
        "<uuid:clearance_pk>/items/<uuid:pk>/return/",
        views.clearance_item_return,
        name="item-return",
    ),
    path(
        "<uuid:clearance_pk>/items/<uuid:pk>/transfer/",
        views.clearance_item_transfer,
        name="item-transfer",
    ),
    path(
        "<uuid:clearance_pk>/<str:target_type>/<uuid:target_pk>/attachments/upload/",
        views.clearance_attachment_upload,
        name="attachment-upload",
    ),
    path(
        "<uuid:clearance_pk>/attachments/<uuid:pk>/download/",
        views.clearance_attachment_download,
        name="attachment-download",
    ),
    path(
        "<uuid:clearance_pk>/attachments/<uuid:pk>/void/",
        views.clearance_attachment_void,
        name="attachment-void",
    ),
]
