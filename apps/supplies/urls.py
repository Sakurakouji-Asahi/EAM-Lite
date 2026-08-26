from django.urls import path

from apps.supplies import views


app_name = "supplies"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path("categories/", views.category_list, name="category-list"),
    path("categories/new/", views.category_create, name="category-create"),
    path("categories/<uuid:pk>/edit/", views.category_edit, name="category-edit"),
    path(
        "categories/<uuid:pk>/deactivate/",
        views.category_deactivate,
        name="category-deactivate",
    ),
    path("warehouses/", views.warehouse_list, name="warehouse-list"),
    path("warehouses/new/", views.warehouse_create, name="warehouse-create"),
    path("warehouses/<uuid:pk>/edit/", views.warehouse_edit, name="warehouse-edit"),
    path(
        "warehouses/<uuid:pk>/deactivate/",
        views.warehouse_deactivate,
        name="warehouse-deactivate",
    ),
    path("items/", views.item_list, name="item-list"),
    path("items/new/", views.item_create, name="item-create"),
    path("items/<uuid:pk>/edit/", views.item_edit, name="item-edit"),
    path(
        "items/<uuid:pk>/deactivate/",
        views.item_deactivate,
        name="item-deactivate",
    ),
    path("imports/items/", views.item_import, name="item-import"),
]
