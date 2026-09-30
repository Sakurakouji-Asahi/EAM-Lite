"""Stable source identities shared by import execution and follow-up views."""


def opening_stock_document_key(batch_id, warehouse_id):
    return f"opening-stock-import:{batch_id}:{warehouse_id}"
