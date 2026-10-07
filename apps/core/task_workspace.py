"""Role-safe presentation of the existing task aggregates; no new queries."""

from django.urls import reverse


def build_task_workspace(*, navigation, dashboard, lifecycle, supply_dashboard=None):
    tasks = navigation.get("tasks", {})
    supplies = navigation.get("supplies", {})
    pending = dashboard.get("pending", {})
    loans = lifecycle.get("loans", {})
    disposals = lifecycle.get("disposals", {})
    supply = supply_dashboard or {}
    cards = []
    priorities = []

    def add_card(key, title, count, description, route, *, query="", urgent=False):
        cards.append({
            "key": key, "title": title, "count": count, "description": description,
            "url": reverse(route) + query,
            "state": "urgent" if urgent else ("pending" if count else "clear"),
        })

    def add_priority(key, title, count, route, query, description):
        if count:
            priorities.append({
                "key": key, "title": title, "count": count,
                "url": reverse(route) + query, "description": description,
            })

    if lifecycle.get("can_view_loans"):
        overdue = loans.get("overdue", 0)
        add_card("loans", "借用归还", loans.get("active", 0),
                 f"逾期 {overdue} 笔，未来 7 天内到期 {loans.get('due', 0)} 笔",
                 "assets:loan-workbench", urgent=bool(overdue))
        add_priority("overdue_loans", "逾期借用", overdue, "assets:loan-workbench",
                     "?state=overdue", "核对借用期限并办理归还")
    if lifecycle.get("can_view_disposals"):
        actual, finance, complete = (disposals.get(key, 0) for key in ("actual", "finance", "complete"))
        add_card("disposals", "资产处置", actual + finance + complete,
                 f"补结果 {actual} · 财务核对 {finance} · 待完成 {complete}",
                 "assets:disposal-workbench")
    if navigation.get("finance_reports", {}).get("can_manage_finance"):
        add_card("pending_finance", "财务确认", pending.get("pending_finance", 0),
                 "资料齐备后确认金额和折旧", "finance:pending-list")
    if tasks.get("can_manage_labels"):
        add_card("pending_labels", "待贴标签", pending.get("pending_label", 0),
                 "打印二维码并逐项确认贴标", "assets:label-queue")
    if tasks.get("can_view_asset_inventory"):
        exceptions = pending.get("inventory_exceptions", 0)
        add_card("asset_inventory", "待资产盘点", pending.get("inventory_pending", 0),
                 "尚未扫描的应盘明细，进入任务查看进度", "inventory:task-list", query="?work=unscanned")
        add_card("asset_exceptions", "资产盘点异常", exceptions,
                 "待核对的异常、盘亏和盘盈明细", "inventory:task-list", query="?work=unresolved", urgent=bool(exceptions))
        add_priority("asset_exceptions", "资产盘点异常", exceptions, "inventory:task-list",
                     "?work=unresolved", "进入待处理差异的盘点任务")
    if tasks.get("can_view_supply_inventory") and supply_dashboard is not None:
        add_card("supply_inventory", "未关闭物品盘点", supply.get("open_count_task_count", 0),
                 "尚未关闭的仓库库存和保管盘点任务", "supplies:count-task-list", query="?status=open")
    if tasks.get("can_view_maintenance"):
        upcoming = pending.get("maintenance_upcoming", 0)
        overdue = pending.get("maintenance_overdue", 0)
        add_card("maintenance", "待保养", upcoming + overdue,
                 f"今日及即将到期 {upcoming} 项 · 逾期 {overdue} 项",
                 "maintenance:due-list", urgent=bool(overdue))
        add_priority("overdue_maintenance", "逾期保养", overdue, "maintenance:due-list",
                     "?due_scope=overdue", "查看已超过保养日期的计划")
    if tasks.get("can_view_offboarding"):
        add_card("offboarding", "离职资产未清", pending.get("offboarding_unresolved", 0),
                 "逐件资产的未解决清退明细", "offboarding:clearance-list", query="?status=unfinished")
        if supply_dashboard is not None:
            add_card("supply_clearance", "离职物品未清", supply.get("pending_clearance_count", 0),
                     "数量耐用品的未解决清退明细", "offboarding:clearance-list", query="?status=unfinished")
    if supply_dashboard is not None and supplies.get("can_view_stock"):
        count = supply.get("low_stock_count", 0)
        url = reverse("reports:supply-report-detail", args=["supply_low_stock"])
        cards.append({"key": "low_stock", "title": "低库存预警", "count": count,
                      "description": "低于最低库存的余额记录，查看数量缺口",
                      "url": url, "state": "urgent" if count else "clear"})
        if count:
            priorities.append({"key": "low_stock", "title": "低库存预警", "count": count,
                               "description": "核对最低库存缺口", "url": url})
    if supply_dashboard is not None and supplies.get("can_manage_documents"):
        add_card("supply_drafts", "待处理库存单据", supply.get("draft_document_count", 0),
                 "核对草稿后过账或取消", "supplies:document-list", query="?status=draft")

    # Date-based overdue work leads; counts retain their own business units.
    priorities.sort(key=lambda item: {
        "overdue_loans": 0, "overdue_maintenance": 1,
        "asset_exceptions": 2, "low_stock": 3,
    }[item["key"]])
    return {"cards": cards, "priorities": priorities,
            "active_categories": sum(bool(card["count"]) for card in cards)}
