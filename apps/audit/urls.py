from django.urls import path

from apps.audit import views


app_name = "audit"

urlpatterns = [
    path("", views.audit_log_list, name="log-list"),
    path("<int:pk>/undo/", views.operation_undo, name="operation-undo"),
]
