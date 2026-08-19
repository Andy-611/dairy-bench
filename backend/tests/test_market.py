"""Focused invariants for the fully collateralized continuous spot market."""

from decimal import Decimal

import pytest

from company_bench.domain.models import CompanyState, InventoryLot, ProductId
from company_bench.economy.market import (
    AssetLedger,
    BuyOrder,
    ContinuousSpotMarket,
    MarketError,
    MarketState,
    OrderIdentity,
    QuoteLadderExecution,
    SellOrder,
    TradeFill,
)
from company_bench.runtime.models import MarketSide, QuoteLadder, QuoteLevel, SimDay

RAW_MILK = ProductId.RAW_MILK


def _lot(
    lot_id: str,
    quantity: str,
    *,
    produced_week: int = 1,
    expires_end_of_week: int = 2,
) -> InventoryLot:
    return InventoryLot(
        lot_id=lot_id,
        product=RAW_MILK,
        quantity=Decimal(quantity),
        produced_week=produced_week,
        expires_end_of_week=expires_end_of_week,
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
    return market.set_quote_ladder(
        owner_id=owner_id,
        ladder=QuoteLadder(
            side=side,
            levels=(
                QuoteLevel(
                    quantity=Decimal(quantity),
                    limit_price=Decimal(price),
                ),
            ),
        ),
        placed_on=SimDay(absolute_day=minute),
        order_identity_factory=lambda _: OrderIdentity(
            order_id=order_id,
            priority_sequence=priority,
        ),
        trade_id_factory=lambda index: f"{order_id}.trade.{index}",
    ).fills


def _clear(
    market: ContinuousSpotMarket,
    owner_id: str,
    side: MarketSide,
    priority: int,
) -> None:
    market.set_quote_ladder(
        owner_id=owner_id,
        ladder=QuoteLadder(side=side),
        placed_on=SimDay(absolute_day=541),
        order_identity_factory=lambda _: OrderIdentity(
            order_id=f"unused_{priority}",
            priority_sequence=priority,
        ),
        trade_id_factory=lambda index: f"clear.trade.{index}",
    )


def _ladder(
    market: ContinuousSpotMarket,
    owner_id: str,
    side: MarketSide,
    levels: tuple[tuple[str, str], ...],
    first_priority: int,
    *,
    prefix: str,
    minute: int = 540,
) -> QuoteLadderExecution:
    return market.set_quote_ladder(
        owner_id=owner_id,
        ladder=QuoteLadder(
            side=side,
            levels=tuple(
                QuoteLevel(quantity=Decimal(quantity), limit_price=Decimal(price))
                for price, quantity in levels
            ),
        ),
        placed_on=SimDay(absolute_day=minute),
        order_identity_factory=lambda offset: OrderIdentity(
            order_id=f"{prefix}_{offset}",
            priority_sequence=first_priority + offset - 1,
        ),
        trade_id_factory=lambda index: f"{prefix}.trade.{index}",
    )


def _quantity(lots: tuple[InventoryLot, ...]) -> Decimal:
    return sum((lot.quantity for lot in lots), start=Decimal("0"))


def test_direct_ledger_mutations_normalize_or_reject_precision() -> None:
    assets = AssetLedger.from_companies(
        (_company("seller", cash="5", inventory=(_lot("lot_1", "5"),)),)
    )
    initial = assets.freeze_states()

    with pytest.raises(ValueError, match=r"exact multiple of 0\.0001"):
        assets.reserve_inventory("seller", RAW_MILK, Decimal("1.00001"))
    assert assets.freeze_states() == initial

    reserved = assets.reserve_inventory("seller", RAW_MILK, Decimal("1.00000"))
    assets.debit_cash("seller", Decimal("1.00000"))

    assert str(reserved[0].quantity) == "1.0000"
    assert str(assets.quantity("seller", RAW_MILK)) == "4.0000"
    assert str(assets.cash("seller")) == "4.0000"


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
    assert fills[0].maker_remaining_quantity == Decimal("0.0000")
    assert fills[0].taker_remaining_quantity == Decimal("2.0000")
    remaining = market.state.orders[0]
    assert isinstance(remaining, BuyOrder)
    assert remaining.remaining_quantity == Decimal("2")
    assert remaining.reserved_cash == Decimal("6")
    assert assets.cash("buyer") == Decimal("88")
    assert assets.cash("seller") == Decimal("6")
    assert market.state.volume == Decimal("3")
    assert market.state.traded_value == Decimal("6")


def test_trade_cash_uses_half_even_four_place_settlement() -> None:
    market, assets = _market(
        _company("buyer", cash="1"),
        _company("seller", inventory=(_lot("lot_1", "0.0003"),)),
    )
    _place(market, "ask", "seller", MarketSide.SELL, "0.0003", "1.5", 1)

    fills = _place(market, "bid", "buyer", MarketSide.BUY, "0.0003", "1.5", 2)

    assert fills[0].total_value == Decimal("0.0004")
    assert assets.cash("buyer") == Decimal("0.9996")
    assert assets.cash("seller") == Decimal("0.0004")
    assert market.state.traded_value == Decimal("0.0004")


@pytest.mark.parametrize("side", (MarketSide.BUY, MarketSide.SELL))
def test_zero_value_order_is_rejected_atomically(side: MarketSide) -> None:
    market, assets = _market(
        _company("buyer", cash="1"),
        _company("seller", inventory=(_lot("lot_1", "0.0001"),)),
    )
    owner = "buyer" if side is MarketSide.BUY else "seller"
    initial_state = market.state
    initial_companies = assets.freeze_states()

    with pytest.raises(MarketError, match="commitment rounds to zero"):
        _place(market, "dust", owner, side, "0.0001", "0.0001", 1)

    assert market.state == initial_state
    assert assets.freeze_states() == initial_companies


def test_rounding_conflicted_partial_fill_cancels_only_the_unfunded_remainder() -> None:
    market, assets = _market(
        _company("buyer", cash="1"),
        _company("seller", inventory=(_lot("lot_1", "0.0001"),)),
    )
    _place(market, "ask", "seller", MarketSide.SELL, "0.0001", "1.5", 1)
    fills = _place(market, "bid", "buyer", MarketSide.BUY, "0.0002", "1.5", 2)

    assert fills[0].quantity == Decimal("0.0001")
    assert fills[0].total_value == Decimal("0.0002")
    assert fills[0].maker_remaining_quantity == Decimal("0.0000")
    assert fills[0].taker_remaining_quantity == Decimal("0.0000")
    assert market.state.orders == ()
    assert assets.cash("buyer") == Decimal("0.9998")
    assert assets.cash("seller") == Decimal("0.0002")


def test_rounding_conflicted_resting_remainder_is_reported_as_withdrawn() -> None:
    market, assets = _market(
        _company("buyer", cash="1"),
        _company("seller", inventory=(_lot("lot_1", "0.0001"),)),
    )
    _place(market, "bid", "buyer", MarketSide.BUY, "0.0002", "1.5", 1)

    fills = _place(market, "ask", "seller", MarketSide.SELL, "0.0001", "1.5", 2)

    assert fills[0].maker_remaining_quantity == Decimal("0.0000")
    assert fills[0].taker_remaining_quantity == Decimal("0.0000")
    assert market.state.orders == ()
    assert assets.cash("buyer") == Decimal("0.9998")
    assert assets.cash("seller") == Decimal("0.0002")


def test_zero_value_sell_remainder_is_released_after_a_partial_fill() -> None:
    market, assets = _market(
        _company("buyer", cash="1"),
        _company("seller", inventory=(_lot("lot_1", "0.0002"),)),
    )
    _place(market, "bid", "buyer", MarketSide.BUY, "0.0001", "1", 1)

    fills = _place(market, "ask", "seller", MarketSide.SELL, "0.0002", "0.5", 2)

    assert fills[0].quantity == Decimal("0.0001")
    assert fills[0].total_value == Decimal("0.0001")
    assert fills[0].maker_remaining_quantity == Decimal("0.0000")
    assert fills[0].taker_remaining_quantity == Decimal("0.0000")
    assert market.state.orders == ()
    assert assets.quantity("seller", RAW_MILK) == Decimal("0.0001")
    assert assets.cash("seller") == Decimal("0.0001")


def test_cancel_releases_remaining_cash_and_inventory() -> None:
    market, assets = _market(
        _company("buyer", cash="10"),
        _company("seller", inventory=(_lot("lot_1", "5"),)),
    )
    _place(market, "bid", "buyer", MarketSide.BUY, "2", "1", 1)
    _place(market, "ask", "seller", MarketSide.SELL, "2", "2", 2)

    _clear(market, "buyer", MarketSide.BUY, 3)
    _clear(market, "seller", MarketSide.SELL, 4)

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

    replacement = _place(
        market,
        "bid_a_new",
        "buyer_a",
        MarketSide.BUY,
        "2",
        "2",
        3,
        minute=500,
    )
    assert replacement == ()
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
        market.set_quote_ladder(
            owner_id="buyer",
            ladder=QuoteLadder(
                side=MarketSide.BUY,
                levels=(QuoteLevel(quantity=Decimal("10"), limit_price=Decimal("2")),),
            ),
            placed_on=SimDay(absolute_day=541),
            order_identity_factory=lambda _: OrderIdentity(
                order_id="too_large",
                priority_sequence=2,
            ),
            trade_id_factory=lambda index: f"failed.trade.{index}",
        )

    assert market.state == state_before
    assert assets.freeze_states() == companies_before
    assert assets.next_lot_sequence == next_lot_sequence_before
    _clear(market, "buyer", MarketSide.BUY, 3)
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
                _lot("old", "5", expires_end_of_week=1),
                _lot("new", "5", expires_end_of_week=2),
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

    assert tuple(lot.expires_end_of_week for lot in fills[0].delivery_lots) == (1, 2)
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

    _clear(market, "seller", MarketSide.SELL, 3)
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


def test_order_book_view_exposes_every_aggregated_price_level() -> None:
    bids = (("1", "1"), ("4", "1"), ("3", "1"), ("2", "1"), ("4", "2"))
    asks = (("5", "1"), ("8", "1"), ("6", "1"), ("7", "1"), ("5", "2"))
    buyers = tuple(_company(f"buyer_{index}", cash="100") for index in range(len(bids)))
    sellers = tuple(
        _company(
            f"seller_{index}",
            inventory=(_lot(f"lot_{index}", quantity),),
        )
        for index, (_, quantity) in enumerate(asks)
    )
    market, _ = _market(*buyers, *sellers)
    priority = 1
    for index, (price, quantity) in enumerate(bids):
        _place(
            market,
            f"bid_{priority}",
            f"buyer_{index}",
            MarketSide.BUY,
            quantity,
            price,
            priority,
        )
        priority += 1
    for index, (price, quantity) in enumerate(asks):
        _place(
            market,
            f"ask_{priority}",
            f"seller_{index}",
            MarketSide.SELL,
            quantity,
            price,
            priority,
        )
        priority += 1

    view = market.view()

    assert tuple((level.unit_price, level.quantity, level.order_count) for level in view.bids) == (
        (Decimal("4"), Decimal("3"), 2),
        (Decimal("3"), Decimal("1"), 1),
        (Decimal("2"), Decimal("1"), 1),
        (Decimal("1"), Decimal("1"), 1),
    )
    assert tuple((level.unit_price, level.quantity, level.order_count) for level in view.asks) == (
        (Decimal("5"), Decimal("3"), 2),
        (Decimal("6"), Decimal("1"), 1),
        (Decimal("7"), Decimal("1"), 1),
        (Decimal("8"), Decimal("1"), 1),
    )


def test_owned_order_queue_tracks_partial_fill_cancel_and_replace() -> None:
    market, _ = _market(
        _company("buyer_a", cash="100"),
        _company("buyer_b", cash="100"),
        _company("buyer_c", cash="100"),
        _company("seller", inventory=(_lot("lot_1", "10"),)),
    )
    _place(market, "bid_a", "buyer_a", MarketSide.BUY, "10", "1", 1)
    _place(market, "bid_b", "buyer_b", MarketSide.BUY, "20", "1", 2)

    order = market.state.company_order_views("buyer_b")[0]
    assert order.queue_ahead_quantity == Decimal("10")

    _place(market, "ask", "seller", MarketSide.SELL, "4", "1", 3)
    assert market.state.company_order_views("buyer_b")[0].queue_ahead_quantity == Decimal("6")

    _clear(market, "buyer_a", MarketSide.BUY, 4)
    assert market.state.company_order_views("buyer_b")[0].queue_ahead_quantity == Decimal("0")

    _place(market, "bid_c", "buyer_c", MarketSide.BUY, "5", "1", 5)
    _place(market, "bid_b_replaced", "buyer_b", MarketSide.BUY, "19", "1", 6)
    assert market.state.company_order_views("buyer_c")[0].queue_ahead_quantity == Decimal("0")
    assert market.state.company_order_views("buyer_b")[0].queue_ahead_quantity == Decimal("5")


def test_three_level_ladder_is_independently_collateralized_and_ordered() -> None:
    market, assets = _market(_company("buyer", cash="100"))

    execution = _ladder(
        market,
        "buyer",
        MarketSide.BUY,
        (("3", "4"), ("2", "3"), ("1", "2")),
        7,
        prefix="bid",
    )

    assert tuple(level.action.value for level in execution.result.levels) == (
        "place",
        "place",
        "place",
    )
    assert tuple(order.limit_price for order in market.state.orders) == (
        Decimal("3"),
        Decimal("2"),
        Decimal("1"),
    )
    assert tuple(order.priority_sequence for order in market.state.orders) == (7, 8, 9)
    assert assets.cash("buyer") == Decimal("80")


def test_exact_target_ladder_preserves_every_order_identity_and_priority() -> None:
    market, _ = _market(_company("seller", inventory=(_lot("lot", "3"),)))
    first = _ladder(
        market,
        "seller",
        MarketSide.SELL,
        (("1", "1"), ("2", "1"), ("3", "1")),
        1,
        prefix="ask",
    )
    state_before = market.state

    second = market.set_quote_ladder(
        owner_id="seller",
        ladder=QuoteLadder(
            side=MarketSide.SELL,
            levels=tuple(level.level for level in first.result.levels),
        ),
        placed_on=SimDay(absolute_day=600),
        order_identity_factory=lambda _: (_ for _ in ()).throw(
            AssertionError("an unchanged ladder must not allocate an identity")
        ),
        trade_id_factory=lambda index: f"unused.{index}",
    )

    assert all(level.action.value == "keep" for level in second.result.levels)
    assert market.state == state_before


def test_ladder_reconciliation_replaces_then_cancels_and_places_deterministically() -> None:
    market, _ = _market(_company("seller", inventory=(_lot("lot", "10"),)))
    _ladder(
        market,
        "seller",
        MarketSide.SELL,
        (("1", "1"), ("2", "1"), ("3", "1")),
        1,
        prefix="old",
    )

    reduced = _ladder(
        market,
        "seller",
        MarketSide.SELL,
        (("1", "1"), ("2.5", "2")),
        4,
        prefix="reduced",
    )
    assert tuple(level.action.value for level in reduced.result.levels) == (
        "keep",
        "replace",
    )
    assert reduced.result.cancelled_order_ids == ("old_3",)

    expanded = _ladder(
        market,
        "seller",
        MarketSide.SELL,
        (("1", "1"), ("2.5", "3"), ("4", "1")),
        5,
        prefix="expanded",
    )
    assert tuple(level.action.value for level in expanded.result.levels) == (
        "keep",
        "replace",
        "place",
    )


def test_ladder_collateral_failure_rolls_back_the_complete_group() -> None:
    market, assets = _market(_company("seller", inventory=(_lot("lot", "2"),)))
    _ladder(
        market,
        "seller",
        MarketSide.SELL,
        (("1", "1"),),
        1,
        prefix="old",
    )
    state_before = market.state
    companies_before = assets.freeze_states()

    with pytest.raises(MarketError, match="insufficient inventory for quote ladder"):
        _ladder(
            market,
            "seller",
            MarketSide.SELL,
            (("1", "1"), ("2", "1"), ("3", "1")),
            2,
            prefix="failed",
        )

    assert market.state == state_before
    assert assets.freeze_states() == companies_before


def test_multi_level_matching_uses_unique_command_wide_trade_ids() -> None:
    market, _ = _market(
        _company("buyer", cash="100"),
        _company("seller_a", inventory=(_lot("lot_a", "1"),)),
        _company("seller_b", inventory=(_lot("lot_b", "1"),)),
    )
    _place(market, "ask_a", "seller_a", MarketSide.SELL, "1", "1", 1)
    _place(market, "ask_b", "seller_b", MarketSide.SELL, "1", "2.5", 2)

    execution = _ladder(
        market,
        "buyer",
        MarketSide.BUY,
        (("4", "1"), ("3", "1"), ("2", "1")),
        3,
        prefix="sweep",
        minute=541,
    )

    assert tuple(fill.trade_id for fill in execution.fills) == (
        "sweep.trade.1",
        "sweep.trade.2",
    )
    assert tuple(fill.taker_order_id for fill in execution.fills) == (
        "sweep_1",
        "sweep_2",
    )
