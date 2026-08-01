"""Focused invariants for the fully collateralized continuous spot market."""

from decimal import Decimal

import pytest

from company_bench.market import (
    AssetLedger,
    BuyOrder,
    ContinuousSpotMarket,
    MarketError,
    MarketState,
    SellOrder,
    TradeFill,
)
from company_bench.models import CompanyState, InventoryLot, ProductId
from company_bench.runtime_models import MarketSide, SimTime

RAW_MILK = ProductId.RAW_MILK


def _lot(
    lot_id: str,
    quantity: str,
    *,
    produced_day: int = 1,
    expires_end_of_day: int = 2,
) -> InventoryLot:
    return InventoryLot(
        lot_id=lot_id,
        product=RAW_MILK,
        quantity=Decimal(quantity),
        produced_day=produced_day,
        expires_end_of_day=expires_end_of_day,
    )


def _company(
    company_id: str,
    *,
    cash: str = "0",
    inventory: tuple[InventoryLot, ...] = (),
) -> CompanyState:
    return CompanyState(
        company_id=company_id,
        cash=Decimal(cash),
        inventory=inventory,
    )


def _market(*companies: CompanyState) -> tuple[ContinuousSpotMarket, AssetLedger]:
    assets = AssetLedger.from_companies(companies)
    return ContinuousSpotMarket.open(RAW_MILK, assets), assets


def _place(
    market: ContinuousSpotMarket,
    order_id: str,
    owner_id: str,
    side: MarketSide,
    quantity: str,
    price: str,
    priority: int,
    *,
    minute: int = 540,
) -> tuple[TradeFill, ...]:
    return market.place(
        order_id=order_id,
        owner_id=owner_id,
        side=side,
        quantity=Decimal(quantity),
        limit_price=Decimal(price),
        placed_at=SimTime(absolute_minute=minute),
        priority_sequence=priority,
        trade_id_factory=lambda index: f"{order_id}.trade.{index}",
    )


def _quantity(lots: tuple[InventoryLot, ...]) -> Decimal:
    return sum((lot.quantity for lot in lots), start=Decimal("0"))


def test_unbacked_orders_are_rejected_without_mutating_transaction() -> None:
    market, assets = _market(
        _company("buyer", cash="5"),
        _company("seller", inventory=(_lot("lot_1", "2"),)),
    )
    initial_state = market.state
    initial_companies = assets.freeze_states()

    with pytest.raises(MarketError, match="insufficient cash"):
        _place(market, "large_bid", "buyer", MarketSide.BUY, "3", "2", 1)
    with pytest.raises(MarketError, match="insufficient inventory"):
        _place(market, "large_ask", "seller", MarketSide.SELL, "3", "2", 2)

    assert market.state == initial_state
    assert assets.freeze_states() == initial_companies


@pytest.mark.parametrize(
    ("maker_side", "maker_price", "taker_price"),
    [
        (MarketSide.SELL, "2", "3"),
        (MarketSide.BUY, "3", "2"),
    ],
)
def test_fill_uses_the_resting_maker_price(
    maker_side: MarketSide,
    maker_price: str,
    taker_price: str,
) -> None:
    market, _ = _market(
        _company("buyer", cash="100"),
        _company("seller", inventory=(_lot("lot_1", "2"),)),
    )
    maker_owner = "seller" if maker_side is MarketSide.SELL else "buyer"
    taker_owner = "buyer" if maker_side is MarketSide.SELL else "seller"

    _place(market, "maker", maker_owner, maker_side, "1", maker_price, 1)
    fills = _place(
        market,
        "taker",
        taker_owner,
        MarketSide.BUY if maker_side is MarketSide.SELL else MarketSide.SELL,
        "1",
        taker_price,
        2,
    )

    assert fills[0].unit_price == Decimal(maker_price)
    assert fills[0].maker_order_id == "maker"


def test_matching_obeys_price_then_time_then_persisted_priority() -> None:
    sellers = tuple(
        _company(
            f"seller_{suffix}",
            inventory=(_lot(f"lot_{suffix}", "1"),),
        )
        for suffix in ("worse", "late", "priority_late", "priority_early")
    )
    market, _ = _market(_company("buyer", cash="100"), *sellers)
    asks = (
        ("worse", "3", 1, 500),
        ("late", "2", 2, 501),
        ("priority_late", "2", 4, 500),
        ("priority_early", "2", 3, 500),
    )
    for name, price, priority, minute in asks:
        _place(
            market,
            f"ask_{name}",
            f"seller_{name}",
            MarketSide.SELL,
            "1",
            price,
            priority,
            minute=minute,
        )

    fills = _place(market, "sweep", "buyer", MarketSide.BUY, "4", "3", 5, minute=502)

    assert tuple(fill.maker_order_id for fill in fills) == (
        "ask_priority_early",
        "ask_priority_late",
        "ask_late",
        "ask_worse",
    )


def test_partial_fill_keeps_exact_reserve_and_refunds_price_improvement() -> None:
    market, assets = _market(
        _company("buyer", cash="100"),
        _company("seller", inventory=(_lot("lot_1", "3"),)),
    )
    _place(market, "ask", "seller", MarketSide.SELL, "3", "2", 1)

    fills = _place(market, "bid", "buyer", MarketSide.BUY, "5", "3", 2)

    assert fills[0].quantity == Decimal("3")
    assert _quantity(fills[0].delivery_lots) == Decimal("3")
    remaining = market.state.orders[0]
    assert isinstance(remaining, BuyOrder)
    assert remaining.remaining_quantity == Decimal("2")
    assert remaining.reserved_cash == Decimal("6")
    assert assets.cash("buyer") == Decimal("88")
    assert assets.cash("seller") == Decimal("6")
    assert market.state.volume == Decimal("3")
    assert market.state.traded_value == Decimal("6")


def test_cancel_releases_remaining_cash_and_inventory() -> None:
    market, assets = _market(
        _company("buyer", cash="10"),
        _company("seller", inventory=(_lot("lot_1", "5"),)),
    )
    _place(market, "bid", "buyer", MarketSide.BUY, "2", "1", 1)
    _place(market, "ask", "seller", MarketSide.SELL, "2", "2", 2)

    market.cancel(order_id="bid", owner_id="buyer")
    market.cancel(order_id="ask", owner_id="seller")

    assert market.state.orders == ()
    assert assets.cash("buyer") == Decimal("10")
    assert assets.quantity("seller", RAW_MILK) == Decimal("5")


def test_successful_replace_loses_the_old_orders_priority() -> None:
    market, _ = _market(
        _company("buyer_a", cash="10"),
        _company("buyer_b", cash="10"),
        _company("seller", inventory=(_lot("lot_1", "1"),)),
    )
    _place(market, "bid_a", "buyer_a", MarketSide.BUY, "1", "2", 1, minute=500)
    _place(market, "bid_b", "buyer_b", MarketSide.BUY, "1", "2", 2, minute=500)

    market.replace(
        old_order_id="bid_a",
        owner_id="buyer_a",
        new_order_id="bid_a_new",
        quantity=Decimal("1"),
        limit_price=Decimal("2"),
        placed_at=SimTime(absolute_minute=500),
        priority_sequence=3,
        trade_id_factory=lambda index: f"replace.trade.{index}",
    )
    fills = _place(
        market,
        "ask",
        "seller",
        MarketSide.SELL,
        "1",
        "2",
        4,
        minute=501,
    )

    assert fills[0].maker_order_id == "bid_b"
    assert {order.order_id for order in market.state.orders} == {"bid_a_new"}


def test_failed_replace_is_atomic_on_the_same_market_instance() -> None:
    market, assets = _market(_company("buyer", cash="10"))
    _place(market, "old_bid", "buyer", MarketSide.BUY, "1", "2", 1)
    state_before = market.state
    companies_before = assets.freeze_states()
    next_lot_sequence_before = assets.next_lot_sequence

    with pytest.raises(MarketError, match="insufficient cash"):
        market.replace(
            old_order_id="old_bid",
            owner_id="buyer",
            new_order_id="too_large",
            quantity=Decimal("10"),
            limit_price=Decimal("2"),
            placed_at=SimTime(absolute_minute=541),
            priority_sequence=2,
            trade_id_factory=lambda index: f"failed.trade.{index}",
        )

    assert market.state == state_before
    assert assets.freeze_states() == companies_before
    assert assets.next_lot_sequence == next_lot_sequence_before
    market.cancel(order_id="old_bid", owner_id="buyer")
    assert assets.cash("buyer") == Decimal("10")


def test_self_cross_is_rejected_before_assets_change() -> None:
    market, assets = _market(
        _company(
            "trader",
            cash="10",
            inventory=(_lot("lot_1", "2"),),
        )
    )
    _place(market, "ask", "trader", MarketSide.SELL, "1", "2", 1)
    state_before = market.state
    companies_before = assets.freeze_states()

    with pytest.raises(MarketError, match="self-crossing"):
        _place(market, "bid", "trader", MarketSide.BUY, "1", "2", 2)

    assert market.state == state_before
    assert assets.freeze_states() == companies_before


def test_fefo_fill_and_partial_lot_slices_keep_unique_ids() -> None:
    market, assets = _market(
        _company("buyer", cash="100"),
        _company(
            "seller",
            inventory=(
                _lot("old", "5", expires_end_of_day=1),
                _lot("new", "5", expires_end_of_day=2),
            ),
        ),
    )
    _place(market, "ask", "seller", MarketSide.SELL, "7", "2", 1)
    reserved = market.state.orders[0]
    assert isinstance(reserved, SellOrder)
    assert tuple(lot.quantity for lot in reserved.reserved_lots) == (
        Decimal("5"),
        Decimal("2"),
    )
    assert reserved.reserved_lots[1].lot_id != "new"

    persisted_market = MarketState.model_validate_json(market.state.model_dump_json())
    persisted_companies = assets.freeze_states()
    assets = AssetLedger.from_companies(
        persisted_companies,
        next_lot_sequence=assets.next_lot_sequence,
    )
    market = ContinuousSpotMarket(persisted_market, assets)

    fills = _place(market, "bid", "buyer", MarketSide.BUY, "6", "2", 2)

    assert tuple(lot.expires_end_of_day for lot in fills[0].delivery_lots) == (1, 2)
    assert tuple(lot.quantity for lot in fills[0].delivery_lots) == (
        Decimal("5"),
        Decimal("1"),
    )
    remaining = market.state.orders[0]
    assert isinstance(remaining, SellOrder)
    on_hand_ids = {
        lot.lot_id
        for company in assets.freeze_states()
        if company.company_id == "seller"
        for lot in company.inventory
    }
    assert on_hand_ids.isdisjoint(lot.lot_id for lot in remaining.reserved_lots)

    market.cancel(order_id="ask", owner_id="seller")
    seller_lots = assets.freeze_states()[1].inventory
    assert _quantity(seller_lots) == Decimal("4")
    assert len({lot.lot_id for lot in seller_lots}) == len(seller_lots)


def test_close_releases_every_resting_commitment() -> None:
    market, assets = _market(
        _company("buyer", cash="10"),
        _company("seller", inventory=(_lot("lot_1", "2"),)),
    )
    _place(market, "bid", "buyer", MarketSide.BUY, "2", "1", 1)
    _place(market, "ask", "seller", MarketSide.SELL, "2", "3", 2)

    released = market.close()

    assert {order.order_id for order in released} == {"bid", "ask"}
    assert not market.state.is_open
    assert market.state.orders == ()
    assert assets.cash("buyer") == Decimal("10")
    assert assets.quantity("seller", RAW_MILK) == Decimal("2")


def test_market_view_aggregates_and_limits_each_side_to_top_three_levels() -> None:
    market, _ = _market(
        _company("buyer", cash="1000"),
        _company("seller", inventory=(_lot("lot_1", "20"),)),
    )
    bids = (("1", "1"), ("4", "1"), ("3", "1"), ("2", "1"), ("4", "2"))
    asks = (("5", "1"), ("8", "1"), ("6", "1"), ("7", "1"), ("5", "2"))
    priority = 1
    for price, quantity in bids:
        _place(
            market,
            f"bid_{priority}",
            "buyer",
            MarketSide.BUY,
            quantity,
            price,
            priority,
        )
        priority += 1
    for price, quantity in asks:
        _place(
            market,
            f"ask_{priority}",
            "seller",
            MarketSide.SELL,
            quantity,
            price,
            priority,
        )
        priority += 1

    view = market.view()

    assert view.best_bid == Decimal("4")
    assert view.best_ask == Decimal("5")
    assert tuple((level.unit_price, level.quantity) for level in view.top_bids) == (
        (Decimal("4"), Decimal("3")),
        (Decimal("3"), Decimal("1")),
        (Decimal("2"), Decimal("1")),
    )
    assert tuple((level.unit_price, level.quantity) for level in view.top_asks) == (
        (Decimal("5"), Decimal("3")),
        (Decimal("6"), Decimal("1")),
        (Decimal("7"), Decimal("1")),
    )
