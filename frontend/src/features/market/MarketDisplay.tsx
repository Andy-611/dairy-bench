import { useState } from "react";
import type { CSSProperties } from "react";

import { companyLabel, productLabel } from "../../shared/labels";
import {
  formatExactDecimal,
  isZeroDecimal,
} from "../../shared/format";
import {
  DECISION_PROCESSING_ORDER_LABEL,
  ORDER_BOOK_PRIORITY_LABEL,
  decisionProcessingOrderSummary,
  plural,
} from "../../shared/timelineFormatters";
import { simulationDayLabel } from "../../shared/simulationCalendar";
import type {
  MarketFrameView,
  MarketOrderFlowItemView,
  MarketPriceLevelView,
  ObserverOrderBookView,
  OpenOrderView,
} from "../../shared/api/types";
import { DefinitionItem as MarketMeta } from "../../shared/ui/DefinitionItem";
import { DetailDrawer } from "../../shared/ui/DetailDrawer";
import { DrawerSection as MarketDrawerSection } from "../../shared/ui/DrawerSection";

interface MarketDisplayProps {
  readonly absoluteDay: number;
  readonly frame: MarketFrameView;
}

type BookSide = "ask" | "bid";
type MarketSelection =
  | {
      readonly kind: "flow";
      readonly flow: MarketOrderFlowItemView;
    }
  | {
      readonly kind: "level";
      readonly level: MarketPriceLevelView;
      readonly product: string;
      readonly side: BookSide;
    };

export function MarketDisplay({ absoluteDay, frame }: MarketDisplayProps) {
  const [selectedProduct, setSelectedProduct] = useState(
    frame.closingOrderBooks[0]?.product ?? "",
  );
  const [selection, setSelection] = useState<MarketSelection | null>(null);
  const book =
    frame.closingOrderBooks.find(
      (candidate) => candidate.product === selectedProduct,
    ) ?? frame.closingOrderBooks[0];
  if (book === undefined) {
    return null;
  }

  return (
    <aside
      aria-label={`End-of-day market state on ${simulationDayLabel(absoluteDay)}`}
      className="market-display"
      data-day={absoluteDay}
    >
      <header className="market-display-header">
        <strong>Market display</strong>
        <small>End-of-day state · v{frame.stateVersion}</small>
      </header>
      <OrderFlow
        items={frame.orderFlow}
        onSelect={(flow) => setSelection({ flow, kind: "flow" })}
        selected={selection?.kind === "flow" ? selection.flow : null}
      />
      <TradeTape absoluteDay={absoluteDay} frame={frame} />
      <EndOfDayOrderBook
        absoluteDay={absoluteDay}
        book={book}
        books={frame.closingOrderBooks}
        onSelectLevel={(level, side) =>
          setSelection({ kind: "level", level, product: book.product, side })
        }
        onSelectProduct={(product) => {
          setSelectedProduct(product);
          setSelection(null);
        }}
        selection={selection}
      />
      {selection !== null && (
        <MarketDetailDrawer
          absoluteDay={absoluteDay}
          onClose={() => setSelection(null)}
          selection={selection}
        />
      )}
    </aside>
  );
}

function OrderFlow({
  items,
  onSelect,
  selected,
}: {
  readonly items: readonly MarketOrderFlowItemView[];
  readonly onSelect: (flow: MarketOrderFlowItemView) => void;
  readonly selected: MarketOrderFlowItemView | null;
}) {
  return (
    <section className="market-order-flow">
      <header>
        <span>QUOTE LADDER FLOW</span>
        <strong>
          Atomic reconciliation actions produced by quote-ladder decisions
        </strong>
      </header>
      {items.length === 0 ? (
        <p>
          No quote-ladder reconciliation changed or preserved an order during
          this day.
        </p>
      ) : (
        <ol>
          {items.map((flow) => {
            const order = flowOrder(flow);
            const isSelected =
              selected?.applySequence === flow.applySequence &&
              flowOrder(selected).orderId === order.orderId;
            return (
              <li key={`${flow.applySequence}-${order.orderId}`}>
                <button
                  aria-label={`Open ${flow.action} quote ${order.orderId} reconciliation detail`}
                  className={isSelected ? "selected" : undefined}
                  onClick={() => onSelect(flow)}
                  type="button"
                >
                  <span
                    className="market-flow-sequence"
                    title={decisionProcessingOrderSummary(flow.applySequence)}
                  >
                    Ladder #{flow.applySequence}
                  </span>
                  <span className={`market-flow-action ${flow.action}`}>
                    {flow.action.toUpperCase()}
                  </span>
                  <span className="market-flow-contract">
                    <strong>
                      {bookSide(order.side).toUpperCase()} {formatExactDecimal(order.remainingQuantity)}{" "}
                      {productLabel(order.product)} @ {formatExactDecimal(order.limitPrice)}
                    </strong>
                    <code>{order.orderId}</code>
                  </span>
                  <span className="market-flow-owner">{companyLabel(order.ownerId)}</span>
                  <span className="market-flow-result">{flowResult(flow)}</span>
                </button>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}

function TradeTape({
  absoluteDay,
  frame,
}: {
  readonly absoluteDay: number;
  readonly frame: MarketFrameView;
}) {
  return (
    <section className="trade-tape">
      <header>
        <span>TRADE TAPE</span>
        <strong>Trades on {simulationDayLabel(absoluteDay)} · this day only</strong>
      </header>
      {frame.trades.length === 0 ? (
        <p>No trades during this day</p>
      ) : (
        <ol>
          {frame.trades.map((trade) => (
            <li key={trade.tradeId} title={`Trade ${trade.tradeId}`}>
              <span
                className="trade-sequence"
                title={decisionProcessingOrderSummary(trade.applySequence)}
              >
                Decision #{trade.applySequence}
              </span>
              <span className="trade-contract">
                <strong>{productLabel(trade.product)}</strong>
                <span>
                  {formatExactDecimal(trade.quantity)} @ {formatExactDecimal(trade.unitPrice)}
                </span>
              </span>
              <span className="trade-route">
                <strong>
                  {companyLabel(trade.sellerId)} → {companyLabel(trade.buyerId)}
                </strong>
                <span>Arrives {simulationDayLabel(trade.arrivesOn)}</span>
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function EndOfDayOrderBook({
  absoluteDay,
  book,
  books,
  onSelectLevel,
  onSelectProduct,
  selection,
}: {
  readonly absoluteDay: number;
  readonly book: ObserverOrderBookView;
  readonly books: readonly ObserverOrderBookView[];
  readonly onSelectLevel: (level: MarketPriceLevelView, side: BookSide) => void;
  readonly onSelectProduct: (product: string) => void;
  readonly selection: MarketSelection | null;
}) {
  return (
    <section className="market-book">
      <header className="market-book-header">
        <div>
          <span>ACTIVE ORDER BOOK</span>
          <strong>End-of-day order book · {simulationDayLabel(absoluteDay)}</strong>
        </div>
        <div className="market-product-tabs" role="tablist" aria-label="Market product">
          {books.map((candidate) => (
            <button
              aria-selected={candidate.product === book.product}
              className={candidate.product === book.product ? "selected" : undefined}
              key={candidate.product}
              onClick={() => onSelectProduct(candidate.product)}
              role="tab"
              type="button"
            >
              {productLabel(candidate.product)}
            </button>
          ))}
        </div>
      </header>
      <MarketSummary book={book} />
      <div className="market-book-scroll">
        <div className="market-book-ladder">
          <OrderBookSide
            levels={book.bids}
            onSelect={onSelectLevel}
            product={book.product}
            selection={selection}
            side="bid"
          />
          <OrderBookSide
            levels={book.asks}
            onSelect={onSelectLevel}
            product={book.product}
            selection={selection}
            side="ask"
          />
        </div>
      </div>
    </section>
  );
}

function MarketSummary({ book }: { readonly book: ObserverOrderBookView }) {
  return (
    <dl className="market-summary">
      <MarketMetric label="Last" value={priceOrDash(book.lastTradePrice)} />
      <MarketMetric label="Best Bid" value={priceOrDash(book.bestBid)} />
      <MarketMetric label="Best Ask" value={priceOrDash(book.bestAsk)} />
      <MarketMetric label="Spread" value={priceOrDash(book.spread)} />
    </dl>
  );
}

function MarketMetric({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd>{value}</dd>
    </div>
  );
}

function OrderBookSide({
  levels,
  onSelect,
  product,
  selection,
  side,
}: {
  readonly levels: readonly MarketPriceLevelView[];
  readonly onSelect: (level: MarketPriceLevelView, side: BookSide) => void;
  readonly product: string;
  readonly selection: MarketSelection | null;
  readonly side: BookSide;
}) {
  const maximumSize = Math.max(0, ...levels.map((level) => Number(level.size)));
  const columns = side === "bid" ? ["Orders", "Size", "Price"] : ["Price", "Size", "Orders"];
  return (
    <section className={`order-book-side ${side}`}>
      <h4>{side.toUpperCase()}</h4>
      <div className="order-book-columns" aria-hidden="true">
        {columns.map((column) => (
          <span key={column}>{column}</span>
        ))}
      </div>
      {levels.length === 0 ? (
        <div className="empty-book-side">No active {side}s</div>
      ) : (
        levels.map((level) => (
          <PriceLevel
            depth={maximumSize === 0 ? 0 : (Number(level.size) / maximumSize) * 100}
            key={level.unitPrice}
            level={level}
            onSelect={() => onSelect(level, side)}
            selected={
              selection?.kind === "level" &&
              selection.product === product &&
              selection.side === side &&
              selection.level.unitPrice === level.unitPrice
            }
            side={side}
          />
        ))
      )}
    </section>
  );
}

function PriceLevel({
  depth,
  level,
  onSelect,
  selected,
  side,
}: {
  readonly depth: number;
  readonly level: MarketPriceLevelView;
  readonly onSelect: () => void;
  readonly selected: boolean;
  readonly side: BookSide;
}) {
  const orderCount = level.orders.length;
  const cells =
    side === "bid"
      ? [String(orderCount), formatExactDecimal(level.size), formatExactDecimal(level.unitPrice)]
      : [formatExactDecimal(level.unitPrice), formatExactDecimal(level.size), String(orderCount)];
  return (
    <div className="price-level">
      <button
        aria-label={`Open ${side} ${formatExactDecimal(level.unitPrice)} with ${orderCount} ${plural(orderCount, "order")}`}
        className={`price-level-summary${selected ? " selected" : ""}`}
        data-price={level.unitPrice}
        data-side={side}
        onClick={onSelect}
        style={{ "--depth": `${depth}%` } as CSSProperties}
        type="button"
      >
        {cells.map((cell, index) => (
          <span key={`${index}-${cell}`}>{cell}</span>
        ))}
      </button>
    </div>
  );
}

function MarketDetailDrawer({
  absoluteDay,
  onClose,
  selection,
}: {
  readonly absoluteDay: number;
  readonly onClose: () => void;
  readonly selection: MarketSelection;
}) {
  return selection.kind === "flow" ? (
    <OrderFlowDetail
      absoluteDay={absoluteDay}
      flow={selection.flow}
      onClose={onClose}
    />
  ) : (
    <PriceLevelDetail
      absoluteDay={absoluteDay}
      onClose={onClose}
      selection={selection}
    />
  );
}

function OrderFlowDetail({
  absoluteDay,
  flow,
  onClose,
}: {
  readonly absoluteDay: number;
  readonly flow: MarketOrderFlowItemView;
  readonly onClose: () => void;
}) {
  const order = flowOrder(flow);
  const title = `${flow.action.toUpperCase()} ${bookSide(order.side).toUpperCase()} · ${simulationDayLabel(absoluteDay)}`;
  return (
    <DetailDrawer
      ariaLabel={`Market order ${order.orderId} detail`}
      eyebrow="MARKET ORDER AUDIT"
      onClose={onClose}
      title={title}
    >
      <div className="drawer-body market-drawer-body">
        <MarketDrawerSection
          label="1"
          title="Atomic quote-ladder reconciliation"
        >
          <dl className="market-detail-grid">
            <MarketMeta
              label={DECISION_PROCESSING_ORDER_LABEL}
              value={`#${flow.applySequence}`}
            />
            <MarketMeta label="Action" value={flow.action.toUpperCase()} />
            <MarketMeta label="Company" value={companyLabel(order.ownerId)} />
            <MarketMeta label="Side" value={bookSide(order.side).toUpperCase()} />
            <MarketMeta label="Product" value={productLabel(order.product)} />
            <MarketMeta
              label="Quantity / limit"
              value={`${formatExactDecimal(order.remainingQuantity)} @ ${formatExactDecimal(order.limitPrice)}`}
            />
            <MarketMeta label="Order ID" value={order.orderId} />
            <MarketMeta
              label={ORDER_BOOK_PRIORITY_LABEL}
              value={`#${order.prioritySequence}`}
            />
          </dl>
          {flow.action === "replace" && (
            <p className="market-replacement-note">
              Atomically replaced <code>{flow.replacedOrder.orderId}</code>.
            </p>
          )}
        </MarketDrawerSection>
        {flow.action === "keep" ? (
          <MarketDrawerSection
            label="2"
            title="Immediate reconciliation result"
          >
            <p className="market-result-copy">
              This quote was unchanged, so its existing order ID and time
              priority were preserved.
            </p>
          </MarketDrawerSection>
        ) : flow.action === "cancel" ? (
          <MarketDrawerSection label="2" title="End-of-day result">
            <p className="market-result-copy">
              The active order was removed and does not appear in the
              end-of-day order book.
            </p>
          </MarketDrawerSection>
        ) : (
          <>
            <MarketDrawerSection label="2" title="Immediate matching trace">
              {flow.matches.length === 0 ? (
                <p className="market-result-copy">
                  No maker order crossed this incoming limit.
                </p>
              ) : (
                <ol className="market-match-list">
                  {flow.matches.map((match) => (
                    <li key={match.tradeId}>
                      <header>
                        <code>{match.tradeId}</code>
                        <strong>
                          {formatExactDecimal(match.quantity)} @{" "}
                          {formatExactDecimal(match.unitPrice)}
                        </strong>
                      </header>
                      <span>
                        Maker {bookSide(match.makerOrder.side).toUpperCase()} ·{" "}
                        {companyLabel(match.makerOrder.ownerId)}
                      </span>
                      <small>
                        <code>{match.makerOrder.orderId}</code>
                        {" · "}
                        {ORDER_BOOK_PRIORITY_LABEL} #{match.makerOrder.prioritySequence}
                        {" · "}
                        {formatExactDecimal(match.makerOrder.remainingQuantity)}
                        {" available before fill"}
                        {" · "}
                        {formatExactDecimal(match.makerRemainingQuantity)}
                        {" remaining"}
                        {!isZeroDecimal(match.makerWithdrawnQuantity) && (
                          <>
                            {" · "}
                            {formatExactDecimal(match.makerWithdrawnQuantity)}
                            {" auto-withdrawn"}
                          </>
                        )}
                      </small>
                    </li>
                  ))}
                </ol>
              )}
            </MarketDrawerSection>
            <MarketDrawerSection label="3" title="Immediate decision result">
              <dl className="market-detail-grid compact">
                <MarketMeta
                  label="Submitted"
                  value={formatExactDecimal(flow.incomingOrder.remainingQuantity)}
                />
                <MarketMeta label="Matched" value={formatExactDecimal(flow.matchedQuantity)} />
                <MarketMeta
                  label="Remaining"
                  value={formatExactDecimal(flow.remainingQuantity)}
                />
                {!isZeroDecimal(flow.withdrawnQuantity) && (
                  <MarketMeta
                    label="Auto-withdrawn"
                    value={formatExactDecimal(flow.withdrawnQuantity)}
                  />
                )}
                <MarketMeta label="Result" value={flowResult(flow)} />
              </dl>
            </MarketDrawerSection>
          </>
        )}
      </div>
    </DetailDrawer>
  );
}

function PriceLevelDetail({
  absoluteDay,
  onClose,
  selection,
}: {
  readonly absoluteDay: number;
  readonly onClose: () => void;
  readonly selection: Extract<MarketSelection, { readonly kind: "level" }>;
}) {
  const { level, product, side } = selection;
  const orderCount = level.orders.length;
  return (
    <DetailDrawer
      ariaLabel={`${side} end-of-day order book price level detail`}
      eyebrow="END-OF-DAY ORDER BOOK"
      onClose={onClose}
      title={`${side.toUpperCase()} ${formatExactDecimal(level.unitPrice)} · ${productLabel(product)}`}
    >
      <div className="drawer-body market-drawer-body">
        <MarketDrawerSection
          label="BOOK"
          title={`End-of-day order book · ${simulationDayLabel(absoluteDay)}`}
        >
          <dl className="market-detail-grid compact">
            <MarketMeta label="Side" value={side.toUpperCase()} />
            <MarketMeta label="Price" value={formatExactDecimal(level.unitPrice)} />
            <MarketMeta label="Total size" value={formatExactDecimal(level.size)} />
            <MarketMeta label="Active orders" value={String(orderCount)} />
          </dl>
        </MarketDrawerSection>
        <MarketDrawerSection label="ORD" title={`${orderCount} active ${plural(orderCount, "order")}`}>
          <div className="market-order-detail-table">
            <div className="market-order-detail-head" aria-hidden="true">
              <span>Order ID</span>
              <span>Company</span>
              <span>Remaining</span>
              <span>{ORDER_BOOK_PRIORITY_LABEL}</span>
            </div>
            {level.orders.map((order) => (
              <div className="market-order-detail-row" key={order.orderId}>
                <code>{order.orderId}</code>
                <span>{companyLabel(order.ownerId)}</span>
                <span>{formatExactDecimal(order.remainingQuantity)}</span>
                <span>#{order.prioritySequence}</span>
              </div>
            ))}
          </div>
        </MarketDrawerSection>
      </div>
    </DetailDrawer>
  );
}

function flowOrder(flow: MarketOrderFlowItemView): OpenOrderView {
  if (flow.action === "keep") {
    return flow.preservedOrder;
  }
  return flow.action === "cancel" ? flow.cancelledOrder : flow.incomingOrder;
}

function flowResult(flow: MarketOrderFlowItemView): string {
  if (flow.action === "keep") {
    return "Priority kept";
  }
  if (flow.action === "cancel") {
    return "Removed";
  }
  if (isZeroDecimal(flow.matchedQuantity)) {
    return "Resting";
  }
  if (!isZeroDecimal(flow.withdrawnQuantity)) {
    return `${formatExactDecimal(flow.withdrawnQuantity)} remainder withdrawn`;
  }
  return isZeroDecimal(flow.remainingQuantity)
    ? "Fully matched"
    : `${formatExactDecimal(flow.remainingQuantity)} resting`;
}

function bookSide(side: OpenOrderView["side"]): BookSide {
  return side === "buy" ? "bid" : "ask";
}

function priceOrDash(value: ObserverOrderBookView["bestBid"]): string {
  return value === null ? "—" : formatExactDecimal(value);
}
