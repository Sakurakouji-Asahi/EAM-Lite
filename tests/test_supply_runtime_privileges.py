from apps.operations.management.commands.grant_runtime_database_privileges import (
    _NO_DELETE_MODELS,
)


def test_runtime_role_cannot_delete_or_truncate_supply_accounting_history():
    assert {
        "supplies.SupplyStockBalance",
        "supplies.SupplyStockLedger",
        "supplies.SupplyCustody",
        "supplies.SupplyCustodyMovement",
        "supplies.SupplyCountTask",
        "supplies.SupplyCountLine",
        "supplies.EmployeeSupplyClearanceItem",
    }.issubset(_NO_DELETE_MODELS)
