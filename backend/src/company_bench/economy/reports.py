"""Authoritative weekly private and public report projection."""

from __future__ import annotations

from decimal import Decimal

from company_bench.domain.models import (
    ZERO,
    CompanyId,
    CompanyState,
    CompanyWeeklyReport,
    CompanyWeeklyReportBase,
    DomainEvent,
    FarmOperation,
    FarmWeeklyReport,
    Money,
    ProcessorOperation,
    ProcessorWeeklyReport,
    ProductId,
    PublicRetailerPerformance,
    PublicRetailMarketReport,
    Quantity,
    RetailerOperation,
    RetailerWeeklyReport,
    RetailPrice,
    ScenarioSpec,
    WeeklyOperationState,
    WorldState,
)
from company_bench.domain.precision import EconomicPrecision
from company_bench.economy.ledger import CompanyLedger
from company_bench.economy.valuation import EnterpriseValuation

__all__ = ("WeeklyReportBook",)


class WeeklyReportBook:
    """Build durable reports from authoritative state and event facts."""

    def build(
        self,
        *,
        scenario: ScenarioSpec,
        week: int,
        opening: WorldState,
        closing_companies: tuple[CompanyState, ...],
        operation_states: tuple[WeeklyOperationState, ...],
        retail_prices: tuple[RetailPrice, ...],
        events: tuple[DomainEvent, ...],
    ) -> tuple[tuple[CompanyWeeklyReport, ...], PublicRetailMarketReport]:
        """Return one private report per company and one public retail report."""
        opening_states = {company.company_id: company for company in opening.companies}
        closing_states = {company.company_id: company for company in closing_companies}
        operations = {state.company_id: state for state in operation_states}
        ledgers = {
            company.company_id: CompanyLedger.from_events(company.company_id, events)
            for company in scenario.companies
        }
        total_retail_sales = sum(
            (ledger.consumer_sold_quantity for ledger in ledgers.values()),
            start=ZERO,
        )
        reports = tuple(
            self._company_report(
                scenario=scenario,
                week=week,
                company_id=company.company_id,
                opening=opening_states[company.company_id],
                closing=closing_states[company.company_id],
                operation_state=operations.get(company.company_id),
                ledger=ledgers[company.company_id],
                total_retail_sales=total_retail_sales,
            )
            for company in scenario.companies
        )
        return reports, self._public_retail_report(
            scenario,
            week,
            closing_companies,
            retail_prices,
            ledgers,
            total_retail_sales,
        )

    def _company_report(
        self,
        *,
        scenario: ScenarioSpec,
        week: int,
        company_id: CompanyId,
        opening: CompanyState,
        closing: CompanyState,
        operation_state: WeeklyOperationState | None,
        ledger: CompanyLedger,
        total_retail_sales: Quantity,
    ) -> CompanyWeeklyReport:
        company = scenario.company(company_id)
        common = self._common_report(
            scenario,
            week,
            company_id,
            opening,
            closing,
            ledger,
        )
        operation = company.operation
        if isinstance(operation, FarmOperation):
            if operation_state is None:
                raise RuntimeError("farm weekly report requires operation state")
            sold = ledger.wholesale_sale(ProductId.RAW_MILK).quantity
            return FarmWeeklyReport(
                **common.model_dump(),
                realized_capacity=operation_state.weekly_capacity,
                weekly_base_unit_cost=operation_state.weekly_base_unit_cost,
                produced_quantity=ledger.produced_quantity,
                production_cost=ledger.production_cost,
                average_production_cost=_average(
                    ledger.production_cost,
                    ledger.produced_quantity,
                ),
                capacity_utilization=_ratio(
                    operation_state.used_capacity,
                    operation_state.weekly_capacity,
                ),
                raw_milk_sold=sold,
                raw_milk_unsold=closing.inventory_quantity(ProductId.RAW_MILK),
            )
        if isinstance(operation, ProcessorOperation):
            if operation_state is None:
                raise RuntimeError("processor weekly report requires operation state")
            raw_purchases = ledger.purchase(ProductId.RAW_MILK)
            sold = ledger.wholesale_sale(ProductId.BOTTLED_MILK).quantity
            return ProcessorWeeklyReport(
                **common.model_dump(),
                realized_capacity=operation_state.weekly_capacity,
                weekly_base_unit_cost=operation_state.weekly_base_unit_cost,
                raw_milk_purchased=raw_purchases.quantity,
                raw_milk_purchase_spend=raw_purchases.value,
                raw_milk_purchase_vwap=raw_purchases.volume_weighted_unit_price,
                raw_milk_processed=ledger.processed_input_quantity,
                bottled_milk_output=ledger.processed_output_quantity,
                realized_yield=_ratio(
                    ledger.processed_output_quantity,
                    ledger.processed_input_quantity,
                ),
                processing_cost=ledger.processing_cost,
                capacity_utilization=_ratio(
                    operation_state.used_capacity,
                    operation_state.weekly_capacity,
                ),
                bottled_milk_sold=sold,
                bottled_milk_unsold=closing.inventory_quantity(ProductId.BOTTLED_MILK),
            )
        if isinstance(operation, RetailerOperation):
            sale = ledger.require_weekly_consumer_sale()
            procured = ledger.purchase(ProductId.BOTTLED_MILK)
            expired = ledger.expiry(ProductId.BOTTLED_MILK)
            return RetailerWeeklyReport(
                **common.model_dump(),
                procured_quantity=procured.quantity,
                procurement_spend=procured.value,
                procurement_vwap=procured.volume_weighted_unit_price,
                saleable_quantity=sale.saleable_quantity,
                saleable_book_value=sale.saleable_book_value,
                weighted_unit_cost=_average(
                    sale.saleable_book_value,
                    sale.saleable_quantity,
                ),
                committed_retail_price=(
                    sale.retail_price or scenario.consumer_market.initial_retail_price
                ),
                sold_quantity=sale.sold_quantity,
                consumer_revenue=sale.revenue,
                cost_of_goods_sold=sale.cost_of_goods_sold,
                gross_profit=sale.gross_profit,
                operating_profit=sale.gross_profit - common.operation_cost,
                ending_inventory_quantity=closing.inventory_quantity(ProductId.BOTTLED_MILK),
                ending_inventory_book_value=closing.inventory_book_value(ProductId.BOTTLED_MILK),
                sell_through_rate=_ratio(
                    sale.sold_quantity,
                    sale.saleable_quantity,
                ),
                sold_out=sale.sold_out,
                market_share=_ratio(sale.sold_quantity, total_retail_sales),
                expiry_quantity=expired.quantity,
                expiry_book_loss=expired.value,
            )
        raise TypeError(f"unsupported operation: {type(operation).__name__}")

    @staticmethod
    def _common_report(
        scenario: ScenarioSpec,
        week: int,
        company_id: CompanyId,
        opening: CompanyState,
        closing: CompanyState,
        ledger: CompanyLedger,
    ) -> CompanyWeeklyReportBase:
        opening_value = EnterpriseValuation.settled_value(scenario, opening)
        closing_value = EnterpriseValuation.settled_value(scenario, closing)
        company = scenario.company(company_id)
        return CompanyWeeklyReportBase(
            week=week,
            company_id=company_id,
            tier=company.tier,
            status=closing.status,
            opening_enterprise_value=opening_value,
            closing_enterprise_value=closing_value,
            weekly_surplus_change=closing_value - opening_value,
            cumulative_surplus=closing_value - company.initial_cash,
            ending_cash=closing.cash,
            ending_operating_cost_payable=closing.operating_cost_payable,
            purchases=ledger.purchases,
            wholesale_sales=ledger.wholesale_sales,
            operation_cost=ledger.operation_cost_accrued,
            expired_inventory=ledger.expired_inventory,
            ending_inventory=closing.inventory_positions(),
        )

    @staticmethod
    def _public_retail_report(
        scenario: ScenarioSpec,
        week: int,
        closing_companies: tuple[CompanyState, ...],
        retail_prices: tuple[RetailPrice, ...],
        ledgers: dict[CompanyId, CompanyLedger],
        total_sales: Quantity,
    ) -> PublicRetailMarketReport:
        states = {company.company_id: company for company in closing_companies}
        prices = {(price.company_id, price.product): price.unit_price for price in retail_prices}
        retailers = tuple(
            company
            for company in scenario.companies
            if isinstance(company.operation, RetailerOperation)
        )
        sales = {
            company.company_id: ledgers[company.company_id].require_weekly_consumer_sale()
            for company in retailers
        }
        performances = tuple(
            PublicRetailerPerformance(
                company_id=company.company_id,
                status=states[company.company_id].status,
                posted_price=(
                    sales[company.company_id].retail_price
                    if company.company_id in sales
                    else prices.get((company.company_id, company.operation.input_product))
                ),
                sold_quantity=sales[company.company_id].sold_quantity,
                market_share=_ratio(
                    sales[company.company_id].sold_quantity,
                    total_sales,
                ),
                sold_out=sales[company.company_id].sold_out,
            )
            for company in retailers
        )
        total_revenue = sum(
            (sales[company.company_id].revenue for company in retailers),
            start=ZERO,
        )
        return PublicRetailMarketReport(
            week=week,
            active_retailer_count=sum(
                states[company.company_id].is_active for company in retailers
            ),
            total_sold_quantity=total_sales,
            volume_weighted_average_price=_average(total_revenue, total_sales),
            retailers=performances,
        )


def _average(value: Decimal, quantity: Decimal) -> Money | None:
    return EconomicPrecision.round(value / quantity) if quantity > ZERO else None


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    if denominator <= ZERO:
        return ZERO
    return EconomicPrecision.round(min(Decimal("1"), numerator / denominator))
