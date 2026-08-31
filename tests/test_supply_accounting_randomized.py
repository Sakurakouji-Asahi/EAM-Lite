from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from random import Random

import pytest

from apps.supplies.domain import (
    allocate_custody_amount,
    calculate_issue,
    calculate_receipt,
    calculate_receipt_from_amount,
)
from apps.supplies.models import (
    SupplyDocument,
    SupplyDocumentLine,
    SupplyDocumentStatus,
    SupplyDocumentType,
    SupplyStockBalance,
    SupplyStockLedger,
)
from apps.supplies.services import (
    create_supply_document,
    post_supply_document,
    reverse_supply_document,
)
from tests.test_sprint15_support import (
    make_company,
    make_department,
    make_supply_category,
    make_supply_item,
    make_supply_warehouse,
    make_user,
)


# These constants and formulas intentionally live in the test.  They are an
# independent accounting oracle, not aliases of apps.supplies.domain.
_QTY_QUANT = Decimal("0.0001")
_COST_QUANT = Decimal("0.000001")
_MONEY_QUANT = Decimal("0.01")
_ZERO_QTY = Decimal("0.0000")
_ZERO_COST = Decimal("0.000000")
_ZERO_MONEY = Decimal("0.00")
_RANDOM_CASES = 600
_BASE_SEED = 0xEA_2026_0831


def _qty(value) -> Decimal:
    return Decimal(value).quantize(_QTY_QUANT, rounding=ROUND_HALF_UP)


def _cost(value) -> Decimal:
    return Decimal(value).quantize(_COST_QUANT, rounding=ROUND_HALF_UP)


def _money(value) -> Decimal:
    return Decimal(value).quantize(_MONEY_QUANT, rounding=ROUND_HALF_UP)


def _average(quantity: Decimal, amount: Decimal) -> Decimal:
    quantity = _qty(quantity)
    amount = _money(amount)
    if quantity == _ZERO_QTY:
        assert amount == _ZERO_MONEY
        return _ZERO_COST
    return _cost(amount / quantity)


@dataclass(frozen=True)
class _OraclePosting:
    quantity: Decimal
    unit_cost: Decimal
    amount: Decimal
    quantity_before: Decimal
    amount_before: Decimal
    quantity_after: Decimal
    amount_after: Decimal
    average_after: Decimal


@dataclass
class _OracleStock:
    quantity: Decimal = _ZERO_QTY
    amount: Decimal = _ZERO_MONEY

    @property
    def average(self) -> Decimal:
        return _average(self.quantity, self.amount)

    def snapshot(self) -> tuple[Decimal, Decimal]:
        return self.quantity, self.amount

    def restore(self, snapshot: tuple[Decimal, Decimal]) -> None:
        self.quantity, self.amount = snapshot

    def receive_at_unit_cost(self, raw_quantity, raw_unit_cost) -> _OraclePosting:
        quantity = _qty(raw_quantity)
        unit_cost = _cost(raw_unit_cost)
        assert quantity > _ZERO_QTY
        assert unit_cost >= _ZERO_COST
        amount = _money(quantity * unit_cost)
        return self.receive_authoritative_amount(
            quantity,
            amount,
            reported_unit_cost=unit_cost,
        )

    def receive_authoritative_amount(
        self,
        raw_quantity,
        raw_amount,
        *,
        reported_unit_cost: Decimal | None = None,
    ) -> _OraclePosting:
        quantity = _qty(raw_quantity)
        amount = _money(raw_amount)
        assert quantity > _ZERO_QTY
        assert amount >= _ZERO_MONEY
        unit_cost = (
            _cost(reported_unit_cost)
            if reported_unit_cost is not None
            else _cost(amount / quantity)
        )
        quantity_before, amount_before = self.snapshot()
        quantity_after = _qty(quantity_before + quantity)
        amount_after = _money(amount_before + amount)
        average_after = _average(quantity_after, amount_after)
        self.quantity = quantity_after
        self.amount = amount_after
        return _OraclePosting(
            quantity=quantity,
            unit_cost=unit_cost,
            amount=amount,
            quantity_before=quantity_before,
            amount_before=amount_before,
            quantity_after=quantity_after,
            amount_after=amount_after,
            average_after=average_after,
        )

    def issue(self, raw_quantity) -> _OraclePosting:
        quantity = _qty(raw_quantity)
        assert _ZERO_QTY < quantity <= self.quantity
        quantity_before, amount_before = self.snapshot()
        unit_cost = self.average
        if quantity == quantity_before:
            amount = amount_before
            quantity_after = _ZERO_QTY
            amount_after = _ZERO_MONEY
            average_after = _ZERO_COST
        else:
            amount = _money(quantity * unit_cost)
            assert amount <= amount_before
            quantity_after = _qty(quantity_before - quantity)
            amount_after = _money(amount_before - amount)
            average_after = _average(quantity_after, amount_after)
        self.quantity = quantity_after
        self.amount = amount_after
        return _OraclePosting(
            quantity=quantity,
            unit_cost=unit_cost,
            amount=amount,
            quantity_before=quantity_before,
            amount_before=amount_before,
            quantity_after=quantity_after,
            amount_after=amount_after,
            average_after=average_after,
        )


@dataclass
class _ReturnSourceOracle:
    line: SupplyDocumentLine | None
    total_quantity: Decimal
    total_amount: Decimal
    unit_cost: Decimal
    returned_quantity: Decimal = _ZERO_QTY
    returned_amount: Decimal = _ZERO_MONEY
    active: bool = True

    @property
    def remaining_quantity(self) -> Decimal:
        return _qty(self.total_quantity - self.returned_quantity)

    @property
    def remaining_amount(self) -> Decimal:
        return _money(self.total_amount - self.returned_amount)

    def allocate(self, raw_quantity) -> tuple[Decimal, Decimal]:
        quantity = _qty(raw_quantity)
        assert self.active
        assert _ZERO_QTY < quantity <= self.remaining_quantity
        cumulative_quantity = _qty(self.returned_quantity + quantity)
        if cumulative_quantity == self.total_quantity:
            cumulative_amount = self.total_amount
        else:
            cumulative_amount = min(
                _money(cumulative_quantity * self.unit_cost),
                self.total_amount,
            )
        amount = _money(cumulative_amount - self.returned_amount)
        assert _ZERO_MONEY <= amount <= self.remaining_amount
        self.returned_quantity = cumulative_quantity
        self.returned_amount = cumulative_amount
        return quantity, amount

    def undo(self, quantity: Decimal, amount: Decimal) -> None:
        self.returned_quantity = _qty(self.returned_quantity - quantity)
        self.returned_amount = _money(self.returned_amount - amount)
        assert self.returned_quantity >= _ZERO_QTY
        assert self.returned_amount >= _ZERO_MONEY


def _random_raw_quantity(rng: Random) -> Decimal:
    # Five decimals deliberately exercise quantity rounding to four places.
    numerator = rng.randint(10_000, 2_500_000)
    if rng.randrange(3) == 0:
        numerator = (numerator // 10) * 10 + 5
    return Decimal(numerator).scaleb(-5)


def _random_raw_cost(rng: Random, *, allow_zero: bool = True) -> Decimal:
    # Seven decimals deliberately exercise unit-cost rounding to six places.
    if allow_zero and rng.randrange(20) == 0:
        return Decimal("0")
    numerator = rng.randint(100_000, 1_000_000_000)
    if rng.randrange(3) == 0:
        numerator = (numerator // 10) * 10 + 5
    return Decimal(numerator).scaleb(-7)


def _quantity_from_ticks(ticks: int) -> Decimal:
    return _qty(Decimal(ticks) * _QTY_QUANT)


def _random_available_quantity(
    rng: Random,
    available: Decimal,
    *,
    full_probability: int = 5,
) -> Decimal:
    ticks = int(_qty(available) / _QTY_QUANT)
    assert ticks >= 1
    if ticks == 1 or rng.randrange(full_probability) == 0:
        return _qty(available)
    return _quantity_from_ticks(rng.randint(1, ticks - 1))


def _random_partition(rng: Random, total: Decimal) -> list[Decimal]:
    ticks = int(_qty(total) / _QTY_QUANT)
    if ticks == 1:
        return [_qty(total)]
    part_count = min(rng.randint(2, 6), ticks)
    cuts = sorted(rng.sample(range(1, ticks), part_count - 1))
    boundaries = [0, *cuts, ticks]
    return [
        _quantity_from_ticks(boundaries[index + 1] - boundaries[index])
        for index in range(part_count)
    ]


def _assert_receipt_result(result, expected: _OraclePosting) -> None:
    assert result.receipt_quantity == expected.quantity
    assert result.receipt_unit_cost == expected.unit_cost
    assert result.receipt_amount == expected.amount
    assert result.quantity_after == expected.quantity_after
    assert result.amount_after == expected.amount_after
    assert result.average_unit_cost_after == expected.average_after


def _assert_issue_result(result, expected: _OraclePosting) -> None:
    assert result.issue_quantity == expected.quantity
    assert result.issue_unit_cost == expected.unit_cost
    assert result.issue_amount == expected.amount
    assert result.quantity_after == expected.quantity_after
    assert result.amount_after == expected.amount_after
    assert result.average_unit_cost_after == expected.average_after


@pytest.mark.parametrize(
    "case_seed",
    range(_RANDOM_CASES),
    ids=lambda value: f"seed-{value:03d}",
)
def test_600_randomized_decimal_sequences_match_independent_oracle(case_seed):
    rng = Random(_BASE_SEED + case_seed)

    # Multiple receipts followed by a partial and a final issue validate the
    # moving average and the zero-quantity/zero-amount final-period correction.
    oracle = _OracleStock()
    actual_quantity = _ZERO_QTY
    actual_amount = _ZERO_MONEY
    for _ in range(rng.randint(2, 5)):
        quantity = _random_raw_quantity(rng)
        unit_cost = _random_raw_cost(rng)
        expected = oracle.receive_at_unit_cost(quantity, unit_cost)
        actual = calculate_receipt(
            actual_quantity,
            actual_amount,
            quantity,
            unit_cost,
        )
        _assert_receipt_result(actual, expected)
        actual_quantity = actual.quantity_after
        actual_amount = actual.amount_after

    partial_quantity = _random_available_quantity(
        rng,
        oracle.quantity,
        full_probability=10_000,
    )
    if partial_quantity == oracle.quantity:
        partial_quantity = _quantity_from_ticks(
            max(1, int(oracle.quantity / _QTY_QUANT) // 2)
        )
    expected_issue = oracle.issue(partial_quantity)
    actual_issue = calculate_issue(actual_quantity, actual_amount, partial_quantity)
    _assert_issue_result(actual_issue, expected_issue)
    actual_quantity = actual_issue.quantity_after
    actual_amount = actual_issue.amount_after

    expected_final_issue = oracle.issue(oracle.quantity)
    actual_final_issue = calculate_issue(
        actual_quantity,
        actual_amount,
        actual_quantity,
    )
    _assert_issue_result(actual_final_issue, expected_final_issue)
    assert actual_final_issue.quantity_after == _ZERO_QTY
    assert actual_final_issue.amount_after == _ZERO_MONEY
    assert actual_final_issue.average_unit_cost_after == _ZERO_COST

    # Returns carry the authoritative original issue amount.  The oracle uses
    # cumulative allocation, so splitting a source into many returns neither
    # creates nor loses cents.
    source_oracle = _OracleStock()
    source_actual_quantity = _ZERO_QTY
    source_actual_amount = _ZERO_MONEY
    for _ in range(rng.randint(2, 4)):
        quantity = _random_raw_quantity(rng)
        unit_cost = _random_raw_cost(rng, allow_zero=False)
        expected = source_oracle.receive_at_unit_cost(quantity, unit_cost)
        actual = calculate_receipt(
            source_actual_quantity,
            source_actual_amount,
            quantity,
            unit_cost,
        )
        _assert_receipt_result(actual, expected)
        source_actual_quantity = actual.quantity_after
        source_actual_amount = actual.amount_after
    source_quantity = _random_available_quantity(rng, source_oracle.quantity)
    expected_source_issue = source_oracle.issue(source_quantity)
    actual_source_issue = calculate_issue(
        source_actual_quantity,
        source_actual_amount,
        source_quantity,
    )
    _assert_issue_result(actual_source_issue, expected_source_issue)
    return_source = _ReturnSourceOracle(
        line=None,
        total_quantity=expected_source_issue.quantity,
        total_amount=expected_source_issue.amount,
        unit_cost=expected_source_issue.unit_cost,
    )

    return_oracle = _OracleStock()
    return_actual_quantity = _ZERO_QTY
    return_actual_amount = _ZERO_MONEY
    seed_quantity = _random_raw_quantity(rng)
    seed_cost = _random_raw_cost(rng)
    expected_seed = return_oracle.receive_at_unit_cost(seed_quantity, seed_cost)
    actual_seed = calculate_receipt(
        return_actual_quantity,
        return_actual_amount,
        seed_quantity,
        seed_cost,
    )
    _assert_receipt_result(actual_seed, expected_seed)
    return_actual_quantity = actual_seed.quantity_after
    return_actual_amount = actual_seed.amount_after
    allocated_amount = _ZERO_MONEY
    for quantity in _random_partition(rng, return_source.total_quantity):
        return_quantity, return_amount = return_source.allocate(quantity)
        expected_return = return_oracle.receive_authoritative_amount(
            return_quantity,
            return_amount,
        )
        actual_return = calculate_receipt_from_amount(
            return_actual_quantity,
            return_actual_amount,
            return_quantity,
            return_amount,
        )
        _assert_receipt_result(actual_return, expected_return)
        return_actual_quantity = actual_return.quantity_after
        return_actual_amount = actual_return.amount_after
        allocated_amount = _money(allocated_amount + return_amount)
    assert return_source.returned_quantity == return_source.total_quantity
    assert allocated_amount == return_source.total_amount
    assert return_source.returned_amount == return_source.total_amount

    # A transfer is an issue from the source plus an authoritative-amount
    # receipt at the destination.  The source is drained in two stages to
    # exercise both partial and full-transfer tail handling.
    transfer_source = _OracleStock()
    transfer_source_actual_quantity = _ZERO_QTY
    transfer_source_actual_amount = _ZERO_MONEY
    for _ in range(rng.randint(2, 4)):
        quantity = _random_raw_quantity(rng)
        unit_cost = _random_raw_cost(rng)
        expected = transfer_source.receive_at_unit_cost(quantity, unit_cost)
        actual = calculate_receipt(
            transfer_source_actual_quantity,
            transfer_source_actual_amount,
            quantity,
            unit_cost,
        )
        _assert_receipt_result(actual, expected)
        transfer_source_actual_quantity = actual.quantity_after
        transfer_source_actual_amount = actual.amount_after

    transfer_target = _OracleStock()
    target_seed_quantity = _random_raw_quantity(rng)
    target_seed_cost = _random_raw_cost(rng)
    target_seed = transfer_target.receive_at_unit_cost(
        target_seed_quantity,
        target_seed_cost,
    )
    target_actual = calculate_receipt(
        _ZERO_QTY,
        _ZERO_MONEY,
        target_seed_quantity,
        target_seed_cost,
    )
    _assert_receipt_result(target_actual, target_seed)
    target_actual_quantity = target_actual.quantity_after
    target_actual_amount = target_actual.amount_after

    first_transfer_quantity = _random_available_quantity(
        rng,
        transfer_source.quantity,
        full_probability=10_000,
    )
    if first_transfer_quantity == transfer_source.quantity:
        first_transfer_quantity = _quantity_from_ticks(
            max(1, int(transfer_source.quantity / _QTY_QUANT) // 2)
        )
    for quantity in (first_transfer_quantity, None):
        transfer_quantity = transfer_source.quantity if quantity is None else quantity
        expected_out = transfer_source.issue(transfer_quantity)
        actual_out = calculate_issue(
            transfer_source_actual_quantity,
            transfer_source_actual_amount,
            transfer_quantity,
        )
        _assert_issue_result(actual_out, expected_out)
        transfer_source_actual_quantity = actual_out.quantity_after
        transfer_source_actual_amount = actual_out.amount_after

        expected_in = transfer_target.receive_authoritative_amount(
            expected_out.quantity,
            expected_out.amount,
        )
        actual_in = calculate_receipt_from_amount(
            target_actual_quantity,
            target_actual_amount,
            actual_out.issue_quantity,
            actual_out.issue_amount,
        )
        _assert_receipt_result(actual_in, expected_in)
        assert actual_in.receipt_amount == actual_out.issue_amount
        target_actual_quantity = actual_in.quantity_after
        target_actual_amount = actual_in.amount_after

    assert transfer_source.quantity == _ZERO_QTY
    assert transfer_source.amount == _ZERO_MONEY
    assert transfer_source_actual_quantity == _ZERO_QTY
    assert transfer_source_actual_amount == _ZERO_MONEY

    # Quantity-managed durable custody uses the same immutable snapshot rule.
    # Every partial allocation must be capped by the remaining authoritative
    # amount, and the final action consumes the exact tail.
    custody_quantity = expected_source_issue.quantity
    custody_amount = expected_source_issue.amount
    custody_cost = expected_source_issue.unit_cost
    allocated_custody_amount = _ZERO_MONEY
    for action_quantity in _random_partition(rng, custody_quantity):
        amount_before = custody_amount
        result = allocate_custody_amount(
            current_quantity=custody_quantity,
            current_amount=custody_amount,
            unit_cost_snapshot=custody_cost,
            action_quantity=action_quantity,
        )
        if action_quantity == custody_quantity:
            expected_action_amount = custody_amount
        else:
            expected_action_amount = min(
                _money(action_quantity * custody_cost),
                custody_amount,
            )
        assert result.action_amount == expected_action_amount
        assert _ZERO_MONEY <= result.action_amount <= amount_before
        custody_quantity = result.quantity_after
        custody_amount = result.amount_after
        allocated_custody_amount = _money(
            allocated_custody_amount + result.action_amount
        )
    assert custody_quantity == _ZERO_QTY
    assert custody_amount == _ZERO_MONEY
    assert allocated_custody_amount == expected_source_issue.amount


@dataclass
class _PostedOperation:
    document: SupplyDocument
    snapshots: dict[object, tuple[Decimal, Decimal]]
    issued_source: _ReturnSourceOracle | None = None
    returned_source: _ReturnSourceOracle | None = None
    returned_quantity: Decimal = _ZERO_QTY
    returned_amount: Decimal = _ZERO_MONEY


def _post_once_and_retry(document: SupplyDocument, actor, key: str) -> SupplyDocument:
    posted = post_supply_document(
        document=document,
        actor=actor,
        idempotency_key=key,
    )
    ledger_ids = set(posted.stock_ledgers.values_list("pk", flat=True))
    retried = post_supply_document(
        document=document,
        actor=actor,
        idempotency_key=key,
    )
    assert retried.pk == posted.pk
    assert set(retried.stock_ledgers.values_list("pk", flat=True)) == ledger_ids
    return posted


def _assert_database_accounting_invariants(
    *,
    company,
    item,
    warehouses,
    oracle_by_warehouse,
) -> None:
    for warehouse in warehouses:
        expected = oracle_by_warehouse[warehouse.pk]
        ledgers = list(
            SupplyStockLedger.objects.filter(
                company=company,
                warehouse=warehouse,
                item=item,
            ).order_by(
                "occurred_at",
                "document__posted_at",
                "document__document_no",
                "document_line__line_no",
                "movement_type",
            )
        )
        balance = SupplyStockBalance.objects.filter(
            company=company,
            warehouse=warehouse,
            item=item,
        ).first()
        if not ledgers:
            assert balance is None
            assert expected.snapshot() == (_ZERO_QTY, _ZERO_MONEY)
            continue
        assert balance is not None
        quantity_delta = sum(
            (ledger.quantity_delta for ledger in ledgers),
            _ZERO_QTY,
        )
        amount_delta = sum(
            (ledger.amount_delta for ledger in ledgers),
            _ZERO_MONEY,
        )
        assert _qty(quantity_delta) == balance.quantity_on_hand == expected.quantity
        assert _money(amount_delta) == balance.amount_on_hand == expected.amount
        assert balance.average_unit_cost == expected.average
        assert balance.quantity_on_hand >= _ZERO_QTY
        assert balance.amount_on_hand >= _ZERO_MONEY

        previous = None
        for ledger in ledgers:
            assert ledger.quantity_after == _qty(
                ledger.quantity_before + ledger.quantity_delta
            )
            assert ledger.amount_after == _money(
                ledger.amount_before + ledger.amount_delta
            )
            assert ledger.quantity_before >= _ZERO_QTY
            assert ledger.quantity_after >= _ZERO_QTY
            assert ledger.amount_before >= _ZERO_MONEY
            assert ledger.amount_after >= _ZERO_MONEY
            assert ledger.average_unit_cost_before == _average(
                ledger.quantity_before,
                ledger.amount_before,
            )
            assert ledger.average_unit_cost_after == _average(
                ledger.quantity_after,
                ledger.amount_after,
            )
            assert ledger.document_line.document_id == ledger.document_id
            assert ledger.document_line.item_id == ledger.item_id == item.pk
            if previous is not None:
                assert ledger.quantity_before == previous.quantity_after
                assert ledger.amount_before == previous.amount_after
                assert (
                    ledger.average_unit_cost_before
                    == previous.average_unit_cost_after
                )
            previous = ledger
        assert previous.quantity_after == balance.quantity_on_hand
        assert previous.amount_after == balance.amount_on_hand
        assert previous.average_unit_cost_after == balance.average_unit_cost

    posted_lines = SupplyDocumentLine.objects.filter(
        company=company,
        item=item,
        document__status__in=(
            SupplyDocumentStatus.POSTED,
            SupplyDocumentStatus.REVERSED,
        ),
    ).select_related("document", "document__reversal_of")
    for line in posted_lines:
        ledgers = list(line.stock_ledgers.all())
        assert line.posted_unit_cost is not None
        assert line.posted_amount is not None
        assert ledgers
        is_transfer = line.document.document_type == SupplyDocumentType.TRANSFER
        reverses_transfer = (
            line.document.document_type == SupplyDocumentType.REVERSAL
            and line.document.reversal_of.document_type
            == SupplyDocumentType.TRANSFER
        )
        assert len(ledgers) == (2 if is_transfer or reverses_transfer else 1)
        for ledger in ledgers:
            assert abs(ledger.quantity_delta) == line.quantity
            assert abs(ledger.amount_delta) == line.posted_amount
            assert ledger.unit_cost == line.posted_unit_cost
        if len(ledgers) == 2:
            assert sum(
                (ledger.quantity_delta for ledger in ledgers),
                _ZERO_QTY,
            ) == _ZERO_QTY
            assert sum(
                (ledger.amount_delta for ledger in ledgers),
                _ZERO_MONEY,
            ) == _ZERO_MONEY

    issue_lines = SupplyDocumentLine.objects.filter(
        company=company,
        item=item,
        document__document_type=SupplyDocumentType.ISSUE,
        document__status=SupplyDocumentStatus.POSTED,
    )
    for issue_line in issue_lines:
        active_returns = issue_line.return_lines.filter(
            document__document_type=SupplyDocumentType.RETURN,
            document__status=SupplyDocumentStatus.POSTED,
        )
        returned_quantity = sum(
            (line.quantity for line in active_returns),
            _ZERO_QTY,
        )
        returned_amount = sum(
            (line.posted_amount for line in active_returns),
            _ZERO_MONEY,
        )
        assert returned_quantity <= issue_line.quantity
        assert returned_amount <= issue_line.posted_amount
        if returned_quantity == issue_line.quantity:
            assert returned_amount == issue_line.posted_amount

    reversal_ledgers = SupplyStockLedger.objects.filter(
        company=company,
        item=item,
        movement_type="reversal",
    ).select_related("reverses_ledger", "document__reversal_of")
    for reversal in reversal_ledgers:
        original = reversal.reverses_ledger
        assert original is not None
        assert reversal.document.reversal_of_id == original.document_id
        assert reversal.quantity_delta == -original.quantity_delta
        assert reversal.amount_delta == -original.amount_delta
        assert reversal.quantity_before == original.quantity_after
        assert reversal.quantity_after == original.quantity_before
        assert reversal.amount_before == original.amount_after
        assert reversal.amount_after == original.amount_before
        assert reversal.average_unit_cost_before == original.average_unit_cost_after
        assert reversal.average_unit_cost_after == original.average_unit_cost_before


@pytest.mark.django_db
def test_database_random_business_sequence_reconciles_every_step():
    rng = Random(_BASE_SEED)
    company = make_company("RAND-ACCOUNTING")
    actor = make_user("random-accounting-warehouse", "warehouse")
    department = make_department(company, "RAND-USE")
    category = make_supply_category(company, "RAND-CAT")
    item = make_supply_item(company, category, "RAND-ITEM")
    warehouses = [
        make_supply_warehouse(company, "RAND-A"),
        make_supply_warehouse(company, "RAND-B"),
    ]
    oracle_by_warehouse = {
        warehouse.pk: _OracleStock() for warehouse in warehouses
    }
    return_sources: list[_ReturnSourceOracle] = []
    sequence = 0
    latest_operation: _PostedOperation | None = None

    def next_key(label: str) -> str:
        nonlocal sequence
        sequence += 1
        return f"random-accounting-{sequence:03d}-{label}"

    def post_receipt(
        warehouse,
        quantity,
        unit_cost,
        *,
        document_type=SupplyDocumentType.RECEIPT,
    ) -> _PostedOperation:
        nonlocal latest_operation
        key = next_key(document_type)
        stock = oracle_by_warehouse[warehouse.pk]
        snapshots = {warehouse.pk: stock.snapshot()}
        expected = stock.receive_at_unit_cost(quantity, unit_cost)
        payload = {
            "business_date": date(2026, 8, 31),
            "target_warehouse": warehouse,
            "idempotency_key": key,
        }
        lines = [
            {
                "item": item,
                "quantity": Decimal(quantity),
                "entered_unit_cost": Decimal(unit_cost),
                "line_remark": "零成本压力测试" if _cost(unit_cost) == _ZERO_COST else "",
            }
        ]
        document = create_supply_document(
            actor=actor,
            company=company,
            document_type=document_type,
            data=payload,
            lines=lines,
        )
        if sequence % 7 == 1:
            duplicate = create_supply_document(
                actor=actor,
                company=company,
                document_type=document_type,
                data=payload,
                lines=lines,
            )
            assert duplicate.pk == document.pk
        document = _post_once_and_retry(document, actor, key)
        line = document.lines.get()
        assert line.quantity == expected.quantity
        assert line.posted_unit_cost == expected.unit_cost
        assert line.posted_amount == expected.amount
        latest_operation = _PostedOperation(document=document, snapshots=snapshots)
        return latest_operation

    def post_issue(warehouse, quantity) -> _PostedOperation:
        nonlocal latest_operation
        key = next_key("issue")
        stock = oracle_by_warehouse[warehouse.pk]
        snapshots = {warehouse.pk: stock.snapshot()}
        expected = stock.issue(quantity)
        document = create_supply_document(
            actor=actor,
            company=company,
            document_type=SupplyDocumentType.ISSUE,
            data={
                "business_date": date(2026, 8, 31),
                "source_warehouse": warehouse,
                "department": department,
                "idempotency_key": key,
            },
            lines=[
                {
                    "item": item,
                    "quantity": Decimal(quantity),
                    "entered_unit_cost": None,
                }
            ],
        )
        document = _post_once_and_retry(document, actor, key)
        line = document.lines.get()
        assert line.quantity == expected.quantity
        assert line.posted_unit_cost == expected.unit_cost
        assert line.posted_amount == expected.amount
        source = _ReturnSourceOracle(
            line=line,
            total_quantity=expected.quantity,
            total_amount=expected.amount,
            unit_cost=expected.unit_cost,
        )
        return_sources.append(source)
        latest_operation = _PostedOperation(
            document=document,
            snapshots=snapshots,
            issued_source=source,
        )
        return latest_operation

    def post_return(source: _ReturnSourceOracle, warehouse, quantity) -> _PostedOperation:
        nonlocal latest_operation
        key = next_key("return")
        stock = oracle_by_warehouse[warehouse.pk]
        snapshots = {warehouse.pk: stock.snapshot()}
        return_quantity, return_amount = source.allocate(quantity)
        expected = stock.receive_authoritative_amount(
            return_quantity,
            return_amount,
            reported_unit_cost=source.unit_cost,
        )
        document = create_supply_document(
            actor=actor,
            company=company,
            document_type=SupplyDocumentType.RETURN,
            data={
                "business_date": date(2026, 8, 31),
                "target_warehouse": warehouse,
                "idempotency_key": key,
                "remark": "随机序列未使用退回",
            },
            lines=[
                {
                    "item": item,
                    "quantity": return_quantity,
                    "entered_unit_cost": None,
                    "source_issue_line": source.line,
                    "line_remark": "随机序列未使用退回",
                }
            ],
        )
        document = _post_once_and_retry(document, actor, key)
        line = document.lines.get()
        assert line.quantity == expected.quantity
        assert line.posted_unit_cost == source.unit_cost
        assert line.posted_amount == expected.amount
        latest_operation = _PostedOperation(
            document=document,
            snapshots=snapshots,
            returned_source=source,
            returned_quantity=return_quantity,
            returned_amount=return_amount,
        )
        return latest_operation

    def post_transfer(source_warehouse, target_warehouse, quantity) -> _PostedOperation:
        nonlocal latest_operation
        key = next_key("transfer")
        source_stock = oracle_by_warehouse[source_warehouse.pk]
        target_stock = oracle_by_warehouse[target_warehouse.pk]
        snapshots = {
            source_warehouse.pk: source_stock.snapshot(),
            target_warehouse.pk: target_stock.snapshot(),
        }
        expected_out = source_stock.issue(quantity)
        expected_in = target_stock.receive_authoritative_amount(
            expected_out.quantity,
            expected_out.amount,
            reported_unit_cost=expected_out.unit_cost,
        )
        document = create_supply_document(
            actor=actor,
            company=company,
            document_type=SupplyDocumentType.TRANSFER,
            data={
                "business_date": date(2026, 8, 31),
                "source_warehouse": source_warehouse,
                "target_warehouse": target_warehouse,
                "idempotency_key": key,
            },
            lines=[
                {
                    "item": item,
                    "quantity": Decimal(quantity),
                    "entered_unit_cost": None,
                }
            ],
        )
        document = _post_once_and_retry(document, actor, key)
        line = document.lines.get()
        assert line.quantity == expected_out.quantity == expected_in.quantity
        assert line.posted_unit_cost == expected_out.unit_cost
        assert line.posted_amount == expected_out.amount == expected_in.amount
        ledgers = list(document.stock_ledgers.all())
        assert len(ledgers) == 2
        assert sum(
            (ledger.quantity_delta for ledger in ledgers),
            _ZERO_QTY,
        ) == _ZERO_QTY
        assert sum(
            (ledger.amount_delta for ledger in ledgers),
            _ZERO_MONEY,
        ) == _ZERO_MONEY
        latest_operation = _PostedOperation(document=document, snapshots=snapshots)
        return latest_operation

    def reverse_latest(operation: _PostedOperation) -> None:
        nonlocal latest_operation
        key = next_key("reversal")
        reversal = reverse_supply_document(
            document=operation.document,
            actor=actor,
            idempotency_key=key,
            reason="随机序列最新单据冲销",
        )
        duplicate = reverse_supply_document(
            document=operation.document,
            actor=actor,
            idempotency_key=key,
            reason="随机序列最新单据冲销",
        )
        assert duplicate.pk == reversal.pk
        for warehouse_id, snapshot in operation.snapshots.items():
            oracle_by_warehouse[warehouse_id].restore(snapshot)
        if operation.issued_source is not None:
            operation.issued_source.active = False
        if operation.returned_source is not None:
            operation.returned_source.undo(
                operation.returned_quantity,
                operation.returned_amount,
            )
        operation.document.refresh_from_db()
        assert operation.document.status == SupplyDocumentStatus.REVERSED
        assert reversal.reversal_of_id == operation.document.pk
        latest_operation = None

    def assert_all() -> None:
        _assert_database_accounting_invariants(
            company=company,
            item=item,
            warehouses=warehouses,
            oracle_by_warehouse=oracle_by_warehouse,
        )

    # A deterministic cent-tail prefix guarantees coverage of cumulative
    # source allocation: 4 x 0.005 = CNY 0.02, split into four one-unit returns
    # must post 0.01, 0.00, 0.01, 0.00 rather than manufacture extra cents.
    post_receipt(
        warehouses[0],
        Decimal("4"),
        Decimal("0.005000"),
        document_type=SupplyDocumentType.OPENING,
    )
    assert_all()
    tiny_source_operation = post_issue(warehouses[0], Decimal("4"))
    tiny_source = tiny_source_operation.issued_source
    assert tiny_source is not None
    assert_all()
    tail_amounts = []
    for _ in range(4):
        operation = post_return(tiny_source, warehouses[1], Decimal("1"))
        tail_amounts.append(operation.returned_amount)
        assert_all()
    assert tail_amounts == [
        Decimal("0.01"),
        Decimal("0.00"),
        Decimal("0.01"),
        Decimal("0.00"),
    ]
    assert tiny_source.returned_amount == tiny_source.total_amount == Decimal("0.02")
    reverse_latest(latest_operation)
    assert_all()
    post_return(tiny_source, warehouses[1], Decimal("1"))
    assert_all()

    # Guaranteed coverage of mixed-cost moving average, partial issue/return,
    # cross-warehouse transfer, and exact latest-document reversal.
    post_receipt(warehouses[0], Decimal("10.00005"), Decimal("3.3366665"))
    assert_all()
    post_receipt(warehouses[0], Decimal("6"), Decimal("7.125000"))
    assert_all()
    source_operation = post_issue(warehouses[0], Decimal("4"))
    source = source_operation.issued_source
    assert source is not None
    assert_all()
    post_return(source, warehouses[1], Decimal("1.5000"))
    assert_all()
    post_transfer(warehouses[0], warehouses[1], Decimal("2"))
    assert_all()
    reverse_latest(latest_operation)
    assert_all()

    # The remainder is a fixed-seed random service-level sequence.  Each step
    # immediately reconciles immutable ledger deltas, cached balances, line
    # amounts, source-return totals, and the independent Decimal oracle.
    for _ in range(36):
        eligible_sources = [
            source
            for source in return_sources
            if source.active and source.remaining_quantity > _ZERO_QTY
        ]
        nonempty = [
            warehouse
            for warehouse in warehouses
            if oracle_by_warehouse[warehouse.pk].quantity > _ZERO_QTY
        ]
        choices = ["receipt", "receipt"]
        if nonempty:
            choices.extend(("issue", "transfer"))
        if eligible_sources:
            choices.extend(("return", "return"))
        if latest_operation is not None:
            choices.append("reverse")
        action = rng.choice(choices)

        if action == "receipt":
            warehouse = rng.choice(warehouses)
            post_receipt(
                warehouse,
                _random_raw_quantity(rng),
                _random_raw_cost(rng),
            )
        elif action == "issue":
            warehouse = rng.choice(nonempty)
            quantity = _random_available_quantity(
                rng,
                oracle_by_warehouse[warehouse.pk].quantity,
            )
            post_issue(warehouse, quantity)
        elif action == "return":
            source = rng.choice(eligible_sources)
            warehouse = rng.choice(warehouses)
            quantity = _random_available_quantity(
                rng,
                source.remaining_quantity,
            )
            post_return(source, warehouse, quantity)
        elif action == "transfer":
            source_warehouse = rng.choice(nonempty)
            target_warehouse = next(
                warehouse
                for warehouse in warehouses
                if warehouse.pk != source_warehouse.pk
            )
            quantity = _random_available_quantity(
                rng,
                oracle_by_warehouse[source_warehouse.pk].quantity,
            )
            post_transfer(source_warehouse, target_warehouse, quantity)
        else:
            assert latest_operation is not None
            reverse_latest(latest_operation)
        assert_all()

    assert SupplyDocument.objects.filter(company=company).count() >= 48
    assert SupplyStockLedger.objects.filter(company=company, item=item).count() >= 48
