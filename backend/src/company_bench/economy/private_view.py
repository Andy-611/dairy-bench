"""Private unit-economics projection for one company Agent."""

from __future__ import annotations

from decimal import Decimal

from company_bench.domain.models import (
    CompanyId,
    CompanyObservation,
    DomainEvent,
    FarmOperation,
    ProcessorOperation,
    RetailerOperation,
)
from company_bench.domain.precision import ECONOMIC_QUANTUM, EconomicPrecision
from company_bench.economy.ledger import CompanyLedger
from company_bench.runtime.models import (
    OrderBookView,
    PrivateCashFlow,
    PrivateEconomicsView,
    PrivateUnitEconomics,
)

__all__ = ("PrivateEconomicsProjector",)


class PrivateEconomicsProjector:
    """Derive a company's private ledger without exposing rival state."""

    def project(
        self,
        company_id: CompanyId,
        observation: CompanyObservation,
        order_books: tuple[OrderBookView, ...],
        events: tuple[DomainEvent, ...],
    ) -> PrivateEconomicsView:
        """Return cumulative cash flow and current executable unit economics."""
        ledger = CompanyLedger.from_events(company_id, events)
        return PrivateEconomicsView(
            cash_flow=self._cash_flow(ledger),
            unit_economics=self._unit_economics(observation, order_books),
        )

    @staticmethod
    def _cash_flow(ledger: CompanyLedger) -> PrivateCashFlow:
        return PrivateCashFlow(
            purchase_spend=ledger.purchase_spend,
            wholesale_revenue=ledger.wholesale_revenue,
            operation_cost=ledger.operation_cash_paid,
            consumer_revenue=ledger.consumer_revenue,
            expiry_reference_loss=ledger.expiry_reference_loss,
            net_cash_flow=EconomicPrecision.round(
                ledger.wholesale_revenue
                + ledger.consumer_revenue
                - ledger.purchase_spend
                - ledger.operation_cash_paid
            ),
        )

    @staticmethod
    def _unit_economics(
        observation: CompanyObservation,
        order_books: tuple[OrderBookView, ...],
    ) -> PrivateUnitEconomics:
        operation = observation.operation
        books = {book.product: book for book in order_books}
        if isinstance(operation, FarmOperation):
            output_bid = _best_bid(books.get(operation.output_product))
            marginal_cost = _marginal_cost(observation)
            return PrivateUnitEconomics(
                output_product=operation.output_product,
                best_output_bid=output_bid,
                marginal_operation_cost=marginal_cost,
                output_per_input=Decimal("1"),
                break_even_output_price=marginal_cost,
                expected_unit_margin=_margin(output_bid, marginal_cost),
            )
        if isinstance(operation, ProcessorOperation):
            input_ask = _best_ask(books.get(operation.input_product))
            output_bid = _best_bid(books.get(operation.output_product))
            processing_cost = _marginal_cost(observation)
            break_even = (
                None
                if input_ask is None or processing_cost is None
                else EconomicPrecision.round((input_ask + processing_cost) / operation.yield_rate)
            )
            return PrivateUnitEconomics(
                input_product=operation.input_product,
                output_product=operation.output_product,
                best_input_ask=input_ask,
                best_output_bid=output_bid,
                marginal_operation_cost=processing_cost,
                output_per_input=operation.yield_rate,
                break_even_output_price=break_even,
                expected_unit_margin=_margin(output_bid, break_even),
            )
        if isinstance(operation, RetailerOperation):
            input_ask = _best_ask(books.get(operation.input_product))
            return PrivateUnitEconomics(
                input_product=operation.input_product,
                best_input_ask=input_ask,
                consumer_unit_price=observation.retail_price,
                expected_unit_margin=_margin(observation.retail_price, input_ask),
            )
        raise TypeError(f"unsupported operation: {type(operation).__name__}")


def _marginal_cost(observation: CompanyObservation) -> Decimal | None:
    """Return the next minimum batch's private cost per input unit."""
    operation = observation.operation
    weekly = observation.weekly_operation
    if not isinstance(operation, (FarmOperation, ProcessorOperation)) or weekly is None:
        return None
    if weekly.remaining_capacity < ECONOMIC_QUANTUM:
        return None
    cost = operation.cost.incremental_cost(
        weekly_capacity=weekly.weekly_capacity,
        used_capacity=weekly.used_capacity,
        quantity=ECONOMIC_QUANTUM,
        weekly_base_unit_cost=weekly.weekly_base_unit_cost,
    )
    return EconomicPrecision.round(cost / ECONOMIC_QUANTUM)


def _best_ask(book: OrderBookView | None) -> Decimal | None:
    return None if book is None or not book.asks else book.asks[0].unit_price


def _best_bid(book: OrderBookView | None) -> Decimal | None:
    return None if book is None or not book.bids else book.bids[0].unit_price


def _margin(revenue: Decimal | None, cost: Decimal | None) -> Decimal | None:
    return None if revenue is None or cost is None else EconomicPrecision.round(revenue - cost)
