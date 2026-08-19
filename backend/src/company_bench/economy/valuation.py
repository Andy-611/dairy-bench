"""Reference-value accounting for settled and in-flight enterprise assets."""

from __future__ import annotations

from typing import Protocol

from company_bench.domain.models import (
    ZERO,
    CompanyId,
    CompanyState,
    InventoryLot,
    Money,
    ProductId,
    Quantity,
    ScenarioSpec,
)
from company_bench.domain.precision import EconomicPrecision
from company_bench.economy.market import BuyOrder, MarketState, SellOrder


class _ValuedOperation(Protocol):
    company_id: CompanyId
    output_product: ProductId
    output_quantity: Quantity


class _PendingInventory(Protocol):
    buyer_id: CompanyId
    lots: tuple[InventoryLot, ...]


class ActiveEconomy(Protocol):
    """Structural view required to value one active enterprise."""

    scenario: ScenarioSpec
    companies: tuple[CompanyState, ...]
    markets: tuple[MarketState, ...]
    jobs: tuple[_ValuedOperation, ...]
    deliveries: tuple[_PendingInventory, ...]


class EnterpriseValuation:
    """Value every guaranteed enterprise asset at immutable reference prices."""

    def active_value(self, economy: ActiveEconomy, company_id: CompanyId) -> Money:
        """Include available, reserved, in-transit, and funded-operation assets."""
        company = self._company(economy.companies, company_id)
        reserved_cash = sum(
            (
                order.reserved_cash
                for market in economy.markets
                for order in market.orders
                if isinstance(order, BuyOrder) and order.owner_id == company_id
            ),
            start=ZERO,
        )
        inventory = (
            *company.inventory,
            *(
                lot
                for market in economy.markets
                for order in market.orders
                if isinstance(order, SellOrder) and order.owner_id == company_id
                for lot in order.reserved_lots
            ),
            *(
                lot
                for delivery in economy.deliveries
                if delivery.buyer_id == company_id
                for lot in delivery.lots
            ),
        )
        operation_value = sum(
            (
                job.output_quantity * economy.scenario.product(job.output_product).reference_value
                for job in economy.jobs
                if job.company_id == company_id
            ),
            start=ZERO,
        )
        return max(
            ZERO,
            EconomicPrecision.round(
                company.cash
                + reserved_cash
                + economy.scenario.inventory_value(inventory)
                + operation_value
                - company.operating_cost_payable
            ),
        )

    @staticmethod
    def settled_value(scenario: ScenarioSpec, company: CompanyState) -> Money:
        """Value one commitment-free company state."""
        return max(
            ZERO,
            EconomicPrecision.round(
                company.cash
                + scenario.inventory_value(company.inventory)
                - company.operating_cost_payable
            ),
        )

    @staticmethod
    def _company(
        companies: tuple[CompanyState, ...],
        company_id: CompanyId,
    ) -> CompanyState:
        """Return one canonical company state."""
        try:
            return next(company for company in companies if company.company_id == company_id)
        except StopIteration as error:
            raise ValueError(f"unknown company: {company_id}") from error
