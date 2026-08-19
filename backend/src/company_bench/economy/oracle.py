"""Full-information enterprise-surplus Oracle for the V9 dairy economy."""

from __future__ import annotations

import hashlib
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Final

import cvxpy as cp
import numpy as np

from company_bench.domain.models import (
    ZERO,
    CompanySpec,
    FarmOperation,
    Money,
    ProcessorOperation,
    ProductId,
    RetailerOperation,
    ScenarioSpec,
    StrictModel,
)
from company_bench.domain.precision import ECONOMIC_QUANTUM
from company_bench.economy.consumer import SharedConsumerMarket
from company_bench.economy.operations import OperatingEconomics

__all__ = ("EnterpriseOracle", "OracleResult")

ORACLE_VERSION: Final = "enterprise-oracle-v2"
_MAX_SHELF_LIFE_LAG: Final = 3
_PROCESS_CACHE: dict[str, OracleResult] = {}


class OracleResult(StrictModel):
    """Auditable result of one deterministic full-information optimization."""

    oracle_version: str
    input_hash: str
    solver: str
    solver_status: str
    enterprise_surplus_upper_bound: Money


class EnterpriseOracle:
    """Maximize enterprise surplus under the realized physical economy."""

    def __init__(self, cache_directory: Path | None = None) -> None:
        self._cache_directory = cache_directory

    def evaluate(
        self,
        scenario: ScenarioSpec,
        seed: int,
        weeks: int | None = None,
    ) -> OracleResult:
        """Return a versioned disk- and process-cached reference."""
        horizon = scenario.weeks if weeks is None else weeks
        if not 1 <= horizon <= scenario.weeks:
            raise ValueError("Oracle horizon must belong to the scenario")
        evaluated_scenario = scenario.model_copy(update={"weeks": horizon})
        input_hash = _input_hash(_economic_input_json(evaluated_scenario), seed)
        cache_path = (
            self._cache_directory / f"{input_hash}.json"
            if self._cache_directory is not None
            else None
        )
        if cache_path is not None and cache_path.is_file():
            cached = OracleResult.model_validate_json(cache_path.read_text(encoding="utf-8"))
            if cached.input_hash == input_hash and cached.oracle_version == ORACLE_VERSION:
                return cached
        result = _PROCESS_CACHE.get(input_hash)
        if result is None:
            result = _solve(evaluated_scenario.model_dump_json(), seed, input_hash)
            _PROCESS_CACHE[input_hash] = result
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = cache_path.with_suffix(".tmp")
            temporary.write_text(result.model_dump_json(), encoding="utf-8")
            temporary.replace(cache_path)
        return result


def _solve(scenario_json: str, seed: int, input_hash: str) -> OracleResult:
    scenario = ScenarioSpec.model_validate_json(scenario_json)
    if any(
        isinstance(company.operation, FarmOperation)
        and company.operation.output_product is not ProductId.RAW_MILK
        for company in scenario.companies
    ):
        return _coarse_upper_bound(scenario, seed, input_hash)
    farms = tuple(
        company for company in scenario.companies if isinstance(company.operation, FarmOperation)
    )
    processors = tuple(
        company
        for company in scenario.companies
        if isinstance(company.operation, ProcessorOperation)
    )
    weeks = scenario.weeks
    cohort_count = len(scenario.consumer_market.cohorts)
    farm_count = len(farms)
    processor_count = len(processors)

    farm_quantity = cp.Variable((farm_count, weeks), nonneg=True)
    processor_input = cp.Variable((processor_count, weeks), nonneg=True)
    raw_same_week = cp.Variable((farm_count, processor_count, weeks), nonneg=True)
    raw_next_week = (
        cp.Variable(
            (farm_count, processor_count, weeks - 1),
            nonneg=True,
        )
        if weeks > 1
        else None
    )
    terminal_raw = cp.Variable(farm_count, nonneg=True)
    bottled_sales = tuple(
        cp.Variable(
            (processor_count, cohort_count, weeks - lag),
            nonneg=True,
        )
        for lag in range(min(_MAX_SHELF_LIFE_LAG, weeks - 1) + 1)
    )
    terminal_bottled_weeks = min(3, weeks)
    terminal_bottled = cp.Variable(
        (processor_count, terminal_bottled_weeks),
        nonneg=True,
    )

    farm_capacity, farm_cost, processor_capacity, processor_cost = _weekly_economics(
        scenario,
        seed,
        farms,
        processors,
    )
    constraints: list[cp.Constraint] = [
        farm_quantity <= farm_capacity,
        processor_input <= processor_capacity,
    ]

    for farm_index in range(farm_count):
        for week in range(weeks):
            raw_use: cp.Expression = cp.sum(raw_same_week[farm_index, :, week])
            if week < weeks - 1 and raw_next_week is not None:
                raw_use += cp.sum(raw_next_week[farm_index, :, week])
            if week == weeks - 1:
                raw_use += terminal_raw[farm_index]
            constraints.append(raw_use <= farm_quantity[farm_index, week])

    for processor_index in range(processor_count):
        for week in range(weeks):
            supplied: cp.Expression = cp.sum(raw_same_week[:, processor_index, week])
            if week > 0 and raw_next_week is not None:
                supplied += cp.sum(raw_next_week[:, processor_index, week - 1])
            constraints.append(processor_input[processor_index, week] == supplied)

    for processor_index, company in enumerate(processors):
        operation = company.operation
        if not isinstance(operation, ProcessorOperation):
            raise TypeError("processor roster contains a non-processor operation")
        for production_week in range(weeks):
            dispatched: cp.Expression = 0
            for lag, flow in enumerate(bottled_sales):
                if production_week < weeks - lag:
                    dispatched += cp.sum(flow[processor_index, :, production_week])
            if production_week >= weeks - terminal_bottled_weeks:
                dispatched += terminal_bottled[
                    processor_index,
                    production_week - (weeks - terminal_bottled_weeks),
                ]
            constraints.append(
                dispatched
                <= float(operation.yield_rate) * processor_input[processor_index, production_week]
            )

    market = SharedConsumerMarket(scenario.consumer_market)
    populations = tuple(market.population(seed, week) for week in range(1, weeks + 1))
    for week in range(weeks):
        for group_index in range(cohort_count):
            sales: cp.Expression = 0
            for lag, flow in enumerate(bottled_sales):
                production_week = week - lag
                if production_week >= 0:
                    sales += cp.sum(flow[:, group_index, production_week])
            constraints.append(sales <= float(populations[week].cohorts[group_index].quantity))

    revenue: cp.Expression = 0
    for sales_week, population in enumerate(populations):
        for group_index, cohort in enumerate(population.cohorts):
            for lag, flow in enumerate(bottled_sales):
                production_week = sales_week - lag
                if production_week >= 0:
                    revenue += float(cohort.maximum_willingness_to_pay) * cp.sum(
                        flow[:, group_index, production_week]
                    )
    terminal_value = float(scenario.product(ProductId.RAW_MILK).reference_value) * cp.sum(
        terminal_raw
    ) + float(scenario.product(ProductId.BOTTLED_MILK).reference_value) * cp.sum(terminal_bottled)
    production_cost = _convex_cost(farm_quantity, farm_cost, farm_capacity, farms)
    processing_cost = _convex_cost(
        processor_input,
        processor_cost,
        processor_capacity,
        processors,
    )
    retail_operating_cost = float(_retail_operating_cost(scenario))
    problem = cp.Problem(
        cp.Maximize(
            revenue + terminal_value - production_cost - processing_cost - retail_operating_cost
        ),
        constraints,
    )
    value = problem.solve(
        solver=cp.OSQP,
        eps_abs=1e-7,
        eps_rel=1e-7,
        max_iter=250_000,
        polishing=True,
        verbose=False,
        canon_backend=cp.SCIPY_CANON_BACKEND,
    )
    if problem.status not in {cp.OPTIMAL, cp.OPTIMAL_INACCURATE} or value is None:
        raise RuntimeError(f"enterprise Oracle failed with status {problem.status}")

    raw_value = Decimal(str(value))
    safety_margin = max(Decimal("0.1000"), abs(raw_value) * Decimal("0.000001"))
    upper_bound = (raw_value + safety_margin).quantize(
        ECONOMIC_QUANTUM,
        rounding=ROUND_CEILING,
    )
    return OracleResult(
        oracle_version=ORACLE_VERSION,
        input_hash=input_hash,
        solver="OSQP",
        solver_status=problem.status,
        enterprise_surplus_upper_bound=max(ZERO, upper_bound),
    )


def _input_hash(scenario_json: str, seed: int) -> str:
    return hashlib.sha256(f"{ORACLE_VERSION}|{seed}|{scenario_json}".encode()).hexdigest()


def _economic_input_json(scenario: ScenarioSpec) -> str:
    """Exclude policy/runtime metadata that cannot change the Oracle economy."""
    return scenario.model_dump_json(exclude={"scenario_id", "version", "scoring", "runtime"})


def _coarse_upper_bound(
    scenario: ScenarioSpec,
    seed: int,
    input_hash: str,
) -> OracleResult:
    """Return a safe analytical relaxation for noncanonical test compositions."""
    market = SharedConsumerMarket(scenario.consumer_market)
    revenue = sum(
        (
            cohort.quantity * cohort.maximum_willingness_to_pay
            for week in range(1, scenario.weeks + 1)
            for cohort in market.population(seed, week).cohorts
        ),
        start=ZERO,
    )
    economics = OperatingEconomics(scenario, seed)
    states = economics.initial_states()
    terminal_relaxation = ZERO
    for week in range(1, scenario.weeks + 1):
        states = economics.open_week(week, states)
        state_by_company = {state.company_id: state for state in states}
        for company in scenario.productive_companies:
            operation = company.operation
            output_quantity = state_by_company[company.company_id].weekly_capacity
            if isinstance(operation, ProcessorOperation):
                output_quantity *= operation.yield_rate
                product = operation.output_product
            elif isinstance(operation, FarmOperation):
                product = operation.output_product
            else:
                raise TypeError("productive company has an unsupported operation")
            terminal_relaxation += output_quantity * scenario.product(product).reference_value
    upper = (
        revenue + terminal_relaxation - _retail_operating_cost(scenario) + Decimal("1")
    ).quantize(
        ECONOMIC_QUANTUM,
        rounding=ROUND_CEILING,
    )
    return OracleResult(
        oracle_version=ORACLE_VERSION,
        input_hash=input_hash,
        solver="analytical-relaxation",
        solver_status="upper_bound",
        enterprise_surplus_upper_bound=max(ZERO, upper),
    )


def _retail_operating_cost(scenario: ScenarioSpec) -> Decimal:
    """Return mandatory weekly store costs across the full Oracle horizon."""
    return Decimal(scenario.weeks) * sum(
        (
            company.operation.weekly_operating_cost
            for company in scenario.companies
            if isinstance(company.operation, RetailerOperation)
        ),
        start=ZERO,
    )


def _weekly_economics(
    scenario: ScenarioSpec,
    seed: int,
    farms: tuple[CompanySpec, ...],
    processors: tuple[CompanySpec, ...],
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    economics = OperatingEconomics(scenario, seed)
    previous = economics.initial_states()
    by_week = []
    for week in range(1, scenario.weeks + 1):
        previous = economics.open_week(week, previous)
        by_week.append({state.company_id: state for state in previous})
    farm_capacity = np.array(
        [
            [
                float(by_week[week][company.company_id].weekly_capacity)
                for week in range(scenario.weeks)
            ]
            for company in farms
        ]
    )
    farm_cost = np.array(
        [
            [
                float(by_week[week][company.company_id].weekly_base_unit_cost)
                for week in range(scenario.weeks)
            ]
            for company in farms
        ]
    )
    processor_capacity = np.array(
        [
            [
                float(by_week[week][company.company_id].weekly_capacity)
                for week in range(scenario.weeks)
            ]
            for company in processors
        ]
    )
    processor_cost = np.array(
        [
            [
                float(by_week[week][company.company_id].weekly_base_unit_cost)
                for week in range(scenario.weeks)
            ]
            for company in processors
        ]
    )
    return farm_capacity, farm_cost, processor_capacity, processor_cost


def _convex_cost(
    quantity: cp.Variable,
    base_cost: np.ndarray,
    capacity: np.ndarray,
    companies: tuple[CompanySpec, ...],
) -> cp.Expression:
    cost: cp.Expression = 0
    for company_index, company in enumerate(companies):
        operation = company.operation
        curvature = float(operation.cost.curvature)
        linear = cp.multiply(base_cost[company_index], quantity[company_index])
        quadratic_coefficients = (
            curvature * base_cost[company_index] / (2 * capacity[company_index])
        )
        quadratic = cp.multiply(
            quadratic_coefficients,
            cp.square(quantity[company_index]),
        )
        cost += cp.sum(linear + quadratic)
    return cost
