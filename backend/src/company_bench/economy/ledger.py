"""Company-owned economic facts projected from the authoritative event stream."""

from __future__ import annotations

from dataclasses import dataclass

from company_bench.domain.models import (
    ZERO,
    CompanyId,
    ConsumerSaleEvent,
    DomainEvent,
    InventoryExpiredEvent,
    MilkProcessedEvent,
    MilkProducedEvent,
    Money,
    ProductFlowSummary,
    ProductId,
    Quantity,
    RetailOperatingCostChargedEvent,
    StrictModel,
    TradeExecutedEvent,
)
from company_bench.domain.precision import EconomicPrecision

__all__ = ("CompanyLedger",)


@dataclass(slots=True)
class _FlowAccumulator:
    quantity: Quantity = ZERO
    value: Money = ZERO

    def add(self, quantity: Quantity, value: Money) -> None:
        """Accumulate one product movement."""
        self.quantity += quantity
        self.value += value

    def summary(self, product: ProductId) -> ProductFlowSummary:
        """Freeze accumulated movement as a typed summary."""
        return ProductFlowSummary(
            product=product,
            quantity=self.quantity,
            value=self.value,
            volume_weighted_unit_price=(
                EconomicPrecision.round(self.value / self.quantity)
                if self.quantity > ZERO
                else None
            ),
        )


class CompanyLedger(StrictModel):
    """One company's complete event-derived economic ledger."""

    company_id: CompanyId
    purchases: tuple[ProductFlowSummary, ...] = ()
    wholesale_sales: tuple[ProductFlowSummary, ...] = ()
    expired_inventory: tuple[ProductFlowSummary, ...] = ()
    purchase_spend: Money = ZERO
    wholesale_revenue: Money = ZERO
    operation_cost_accrued: Money = ZERO
    operation_cash_paid: Money = ZERO
    consumer_revenue: Money = ZERO
    consumer_sold_quantity: Quantity = ZERO
    expiry_reference_loss: Money = ZERO
    produced_quantity: Quantity = ZERO
    production_cost: Money = ZERO
    processed_input_quantity: Quantity = ZERO
    processed_output_quantity: Quantity = ZERO
    processing_cost: Money = ZERO
    consumer_sales: tuple[ConsumerSaleEvent, ...] = ()

    @classmethod
    def from_events(
        cls,
        company_id: CompanyId,
        events: tuple[DomainEvent, ...],
    ) -> CompanyLedger:
        """Project all company-owned economic facts in one event-stream pass."""
        purchases = {product: _FlowAccumulator() for product in ProductId}
        wholesale_sales = {product: _FlowAccumulator() for product in ProductId}
        expiries = {product: _FlowAccumulator() for product in ProductId}
        purchase_spend = ZERO
        wholesale_revenue = ZERO
        operation_cost_accrued = ZERO
        operation_cash_paid = ZERO
        consumer_revenue = ZERO
        consumer_sold_quantity = ZERO
        expiry_reference_loss = ZERO
        produced_quantity = ZERO
        production_cost = ZERO
        processed_input_quantity = ZERO
        processed_output_quantity = ZERO
        processing_cost = ZERO
        consumer_sales: list[ConsumerSaleEvent] = []

        for event in events:
            if isinstance(event, TradeExecutedEvent):
                if event.buyer_id == company_id:
                    purchases[event.product].add(event.quantity, event.total_value)
                    purchase_spend += event.total_value
                if event.seller_id == company_id:
                    wholesale_sales[event.product].add(event.quantity, event.total_value)
                    wholesale_revenue += event.total_value
            elif isinstance(event, MilkProducedEvent) and event.company_id == company_id:
                produced_quantity += event.actual_quantity
                production_cost += event.cash_cost
                operation_cost_accrued += event.cash_cost
                operation_cash_paid += event.cash_cost
            elif isinstance(event, MilkProcessedEvent) and event.company_id == company_id:
                processed_input_quantity += event.actual_input
                processed_output_quantity += event.output_quantity
                processing_cost += event.cash_cost
                operation_cost_accrued += event.cash_cost
                operation_cash_paid += event.cash_cost
            elif (
                isinstance(event, RetailOperatingCostChargedEvent)
                and event.company_id == company_id
            ):
                operation_cost_accrued += event.cost_accrued
                operation_cash_paid += event.cash_paid
            elif isinstance(event, ConsumerSaleEvent) and event.company_id == company_id:
                consumer_sales.append(event)
                consumer_revenue += event.revenue
                consumer_sold_quantity += event.sold_quantity
            elif isinstance(event, InventoryExpiredEvent) and event.company_id == company_id:
                expiries[event.product].add(event.quantity, event.book_value_loss)
                expiry_reference_loss += event.reference_value_loss

        return cls(
            company_id=company_id,
            purchases=_positive_summaries(purchases),
            wholesale_sales=_positive_summaries(wholesale_sales),
            expired_inventory=_positive_summaries(expiries),
            purchase_spend=purchase_spend,
            wholesale_revenue=wholesale_revenue,
            operation_cost_accrued=operation_cost_accrued,
            operation_cash_paid=operation_cash_paid,
            consumer_revenue=consumer_revenue,
            consumer_sold_quantity=consumer_sold_quantity,
            expiry_reference_loss=expiry_reference_loss,
            produced_quantity=produced_quantity,
            production_cost=production_cost,
            processed_input_quantity=processed_input_quantity,
            processed_output_quantity=processed_output_quantity,
            processing_cost=processing_cost,
            consumer_sales=tuple(consumer_sales),
        )

    def purchase(self, product: ProductId) -> ProductFlowSummary:
        """Return this company's purchases of one product."""
        return _product_flow(self.purchases, product)

    def wholesale_sale(self, product: ProductId) -> ProductFlowSummary:
        """Return this company's wholesale sales of one product."""
        return _product_flow(self.wholesale_sales, product)

    def expiry(self, product: ProductId) -> ProductFlowSummary:
        """Return this company's expired book inventory of one product."""
        return _product_flow(self.expired_inventory, product)

    def require_weekly_consumer_sale(self) -> ConsumerSaleEvent:
        """Return the sole weekly consumer settlement for this retailer."""
        if len(self.consumer_sales) != 1:
            raise RuntimeError(
                f"company {self.company_id} requires exactly one consumer sale; "
                f"found {len(self.consumer_sales)}"
            )
        return self.consumer_sales[0]


def _positive_summaries(
    flows: dict[ProductId, _FlowAccumulator],
) -> tuple[ProductFlowSummary, ...]:
    return tuple(flow.summary(product) for product, flow in flows.items() if flow.quantity > ZERO)


def _product_flow(
    flows: tuple[ProductFlowSummary, ...],
    product: ProductId,
) -> ProductFlowSummary:
    return next(
        (flow for flow in flows if flow.product is product),
        ProductFlowSummary(product=product),
    )
