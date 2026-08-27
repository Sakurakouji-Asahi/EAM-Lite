from django.urls import path

from apps.supplies import views


app_name = "supplies"

urlpatterns = [
    path("", views.dashboard, name="dashboard"),
    path(
        "reconciliation/",
        views.reconciliation_help,
        name="reconciliation-help",
    ),
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
    path("documents/", views.document_list, name="document-list"),
    path(
        "documents/new/<str:document_type>/",
        views.document_create,
        name="document-create",
    ),
    path("documents/<uuid:pk>/", views.document_detail, name="document-detail"),
    path(
        "documents/<uuid:pk>/edit/", views.document_edit, name="document-edit"
    ),
    path(
        "documents/<uuid:pk>/cancel/",
        views.document_cancel,
        name="document-cancel",
    ),
    path(
        "documents/<uuid:pk>/post/", views.document_post, name="document-post"
    ),
    path(
        "documents/<uuid:pk>/reverse/",
        views.document_reverse,
        name="document-reverse",
    ),
    path(
        "documents/returns/from/<uuid:line_pk>/",
        views.consumable_return_create,
        name="consumable-return-create",
    ),
    path("stock/", views.stock_balance_list, name="stock-balance-list"),
    path("stock/ledger/", views.stock_ledger_list, name="stock-ledger-list"),
    path("custodies/", views.custody_list, name="custody-list"),
    path("custodies/mine/", views.my_custodies, name="my-custodies"),
    path("custodies/<uuid:pk>/", views.custody_detail, name="custody-detail"),
    path(
        "custodies/<uuid:pk>/return/",
        views.durable_return_create,
        name="durable-return-create",
    ),
    path(
        "custodies/<uuid:pk>/transfer/",
        views.custody_transfer,
        name="custody-transfer",
    ),
    path(
        "custodies/<uuid:pk>/write-off/<str:action>/",
        views.custody_write_off,
        name="custody-write-off",
    ),
    path(
        "imports/opening-stock/",
        views.opening_stock_import,
        name="opening-stock-import",
    ),
    path(
        "imports/opening-custody/",
        views.opening_custody_import,
        name="opening-custody-import",
    ),
    path("counts/", views.count_task_list, name="count-task-list"),
    path("counts/new/", views.count_task_create, name="count-task-create"),
    path("counts/<uuid:pk>/", views.count_task_detail, name="count-task-detail"),
    path(
        "counts/<uuid:pk>/publish/",
        views.count_task_publish,
        name="count-task-publish",
    ),
    path(
        "counts/<uuid:pk>/add-item/",
        views.count_task_add_item,
        name="count-task-add-item",
    ),
    path(
        "counts/<uuid:pk>/lines/<uuid:line_pk>/record/",
        views.count_line_record,
        name="count-line-record",
    ),
    path(
        "counts/<uuid:pk>/lines/<uuid:line_pk>/cost/",
        views.count_line_adjustment_cost,
        name="count-line-adjustment-cost",
    ),
    path(
        "counts/<uuid:pk>/lines/<uuid:line_pk>/resolve/",
        views.count_line_resolve,
        name="count-line-resolve",
    ),
    path(
        "counts/<uuid:pk>/stop/",
        views.count_task_stop,
        name="count-task-stop",
    ),
    path(
        "counts/<uuid:pk>/close/",
        views.count_task_close,
        name="count-task-close",
    ),
    path(
        "counts/<uuid:pk>/cancel/",
        views.count_task_cancel,
        name="count-task-cancel",
    ),
    path(
        "individual-durables/new/",
        views.individual_durable_create,
        name="individual-durable-create",
    ),
    path(
        "individual-durables/",
        views.individual_durable_list,
        name="individual-durable-list",
    ),
]
