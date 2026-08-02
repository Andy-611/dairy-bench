import { useState } from "react";
import type { CSSProperties, ReactNode } from "react";

import { companyLabel, productLabel } from "../domainLabels";
import {
  formatExactDecimal,
  formatMarketPrice,
  isZeroDecimal,
} from "../format";
import { clockTime, plural } from "../timelineFormatters";
import type {
  MarketFrameView,
  MarketOrderFlowItemView,
  MarketPriceLevelView,
  ObserverOrderBookView,
  OpenOrderView,
} from "../types";
import { DetailDrawer } from "./DetailDrawer";

interface MarketDisplayProps {
  readonly frame: MarketFrameView;
  readonly minute: number;
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

export function MarketDisplay({ frame, minute }: MarketDisplayProps) {
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
      aria-label={`End-of-minute market state at ${clockTime(minute)}`}
      className="market-display"
      data-minute={minute}
    >
      <header className="market-display-header">
        <strong>Market display</strong>
        <small>End-of-minute state · v{frame.stateVersion}</small>
      </header>
      <OrderFlow
        items={frame.orderFlow}
        onSelect={(flow) => setSelection({ flow, kind: "flow" })}
        selected={selection?.kind === "flow" ? selection.flow : null}
      />
      <TradeTape frame={frame} minute={minute} />
      <EndOfMinuteOrderBook
        book={book}
        books={frame.closingOrderBooks}
        minute={minute}
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
          minute={minute}
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
        <span>ORDER FLOW</span>
        <strong>Order commands accepted by the economic engine</strong>
      </header>
      {items.length === 0 ? (
        <p>
          No order commands were accepted by the economic engine during this
          minute.
        </p>
      ) : (
        <ol>
          {items.map((flow) => {
            const order = flowOrder(flow);
            const isSelected =
              selected?.applySequence === flow.applySequence &&
              selected.action === flow.action;
            return (
              <li key={`${flow.applySequence}-${flow.action}`}>
                <button
                  aria-label={`Open ${flow.action} order ${order.orderId} matching detail`}
                  className={isSelected ? "selected" : undefined}
                  onClick={() => onSelect(flow)}
                  type="button"
                >
                  <span className="market-flow-sequence">#{flow.applySequence}</span>
                  <span className={`market-flow-action ${flow.action}`}>
                    {flow.action.toUpperCase()}
                  </span>
                  <span className="market-flow-contract">
                    <strong>
                      {bookSide(order.side).toUpperCase()} {formatExactDecimal(order.remainingQuantity)}{" "}
                      {productLabel(order.product)} @ {formatMarketPrice(order.limitPrice)}
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
  frame,
  minute,
}: {
  readonly frame: MarketFrameView;
  readonly minute: number;
}) {
  return (
    <section className="trade-tape">
      <header>
        <span>TRADE TAPE</span>
        <strong>Trades during {clockTime(minute)}</strong>
      </header>
      {frame.trades.length === 0 ? (
        <p>No trades during this minute</p>
      ) : (
        <ol>
          {frame.trades.map((trade) => (
            <li key={trade.tradeId} title={`Trade ${trade.tradeId}`}>
              <span className="trade-sequence">#{trade.applySequence}</span>
              <span className="trade-contract">
                <strong>{productLabel(trade.product)}</strong>
                <span>
                  {formatExactDecimal(trade.quantity)} @ {formatMarketPrice(trade.unitPrice)}
                </span>
              </span>
              <span className="trade-route">
                <strong>
                  {companyLabel(trade.sellerId)} {"→"} {companyLabel(trade.buyerId)}
                </strong>
                <span>Arrives {clockTime(trade.arrivesAtMinute)}</span>
              </span>
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}

function EndOfMinuteOrderBook({
  book,
  books,
  minute,
  onSelectLevel,
  onSelectProduct,
  selection,
}: {
  readonly book: ObserverOrderBookView;
  readonly books: readonly ObserverOrderBookView[];
  readonly minute: number;
  readonly onSelectLevel: (level: MarketPriceLevelView, side: BookSide) => void;
  readonly onSelectProduct: (product: string) => void;
  readonly selection: MarketSelection | null;
}) {
  return (
    <section className="market-book">
      <header className="market-book-header">
        <div>
          <span>ACTIVE ORDER BOOK</span>
          <strong>End-of-minute order book · {clockTime(minute)}</strong>
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
      ? [String(orderCount), formatExactDecimal(level.size), formatMarketPrice(level.unitPrice)]
      : [formatMarketPrice(level.unitPrice), formatExactDecimal(level.size), String(orderCount)];
  return (
    <div className="price-level">
      <button
        aria-label={`Open ${side} ${formatMarketPrice(level.unitPrice)} with ${orderCount} ${plural(orderCount, "order")}`}
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
  minute,
  onClose,
  selection,
}: {
  readonly minute: number;
  readonly onClose: () => void;
  readonly selection: MarketSelection;
}) {
  return selection.kind === "flow" ? (
    <OrderFlowDetail flow={selection.flow} minute={minute} onClose={onClose} />
  ) : (
    <PriceLevelDetail minute={minute} onClose={onClose} selection={selection} />
  );
}

function OrderFlowDetail({
  flow,
  minute,
  onClose,
}: {
  readonly flow: MarketOrderFlowItemView;
  readonly minute: number;
  readonly onClose: () => void;
}) {
  const order = flowOrder(flow);
  const title = `${flow.action.toUpperCase()} ${bookSide(order.side).toUpperCase()} · ${clockTime(minute)}`;
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
          title="Order command accepted by the economic engine"
        >
          <dl className="market-detail-grid">
            <MarketMeta
              label="Command Processing Order"
              value={`#${flow.applySequence}`}
            />
            <MarketMeta label="Action" value={flow.action.toUpperCase()} />
            <MarketMeta label="Company" value={companyLabel(order.ownerId)} />
            <MarketMeta label="Side" value={bookSide(order.side).toUpperCase()} />
            <MarketMeta label="Product" value={productLabel(order.product)} />
            <MarketMeta
              label="Quantity / limit"
              value={`${formatExactDecimal(order.remainingQuantity)} @ ${formatMarketPrice(order.limitPrice)}`}
            />
            <MarketMeta label="Order ID" value={order.orderId} />
            <MarketMeta label="Priority" value={`#${order.prioritySequence}`} />
          </dl>
          {flow.action === "replace" && (
            <p className="market-replacement-note">
              Atomically replaced <code>{flow.replacedOrder.orderId}</code>.
            </p>
          )}
        </MarketDrawerSection>
        {flow.action === "cancel" ? (
          <MarketDrawerSection label="2" title="End-of-minute result">
            <p className="market-result-copy">
              The active order was removed and does not appear in the
              end-of-minute order book.
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
                          {formatMarketPrice(match.unitPrice)}
                        </strong>
                      </header>
                      <span>
                        Maker {bookSide(match.makerOrder.side).toUpperCase()} ·{" "}
                        {companyLabel(match.makerOrder.ownerId)}
                      </span>
                      <small>
                        <code>{match.makerOrder.orderId}</code> · Priority #{match.makerOrder.prioritySequence}
                        {" · "}{formatExactDecimal(match.makerOrder.remainingQuantity)} available before fill
                      </small>
                    </li>
                  ))}
                </ol>
              )}
            </MarketDrawerSection>
            <MarketDrawerSection label="3" title="End-of-minute result">
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
  minute,
  onClose,
  selection,
}: {
  readonly minute: number;
  readonly onClose: () => void;
  readonly selection: Extract<MarketSelection, { readonly kind: "level" }>;
}) {
  const { level, product, side } = selection;
  const orderCount = level.orders.length;
  return (
    <DetailDrawer
      ariaLabel={`${side} end-of-minute order book price level detail`}
      eyebrow="END-OF-MINUTE ORDER BOOK"
      onClose={onClose}
      title={`${side.toUpperCase()} ${formatMarketPrice(level.unitPrice)} · ${productLabel(product)}`}
    >
      <div className="drawer-body market-drawer-body">
        <MarketDrawerSection
          label="BOOK"
          title={`End-of-minute order book · ${clockTime(minute)}`}
        >
          <dl className="market-detail-grid compact">
            <MarketMeta label="Side" value={side.toUpperCase()} />
            <MarketMeta label="Price" value={formatMarketPrice(level.unitPrice)} />
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
              <span>Priority</span>
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

function MarketDrawerSection({
  children,
  label,
  title,
}: {
  readonly children: ReactNode;
  readonly label: string;
  readonly title: string;
}) {
  return (
    <section className="drawer-section">
      <header>
        <span>{label}</span>
        <h3>{title}</h3>
      </header>
      <div>{children}</div>
    </section>
  );
}

function MarketMeta({ label, value }: { readonly label: string; readonly value: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={value}>{value}</dd>
    </div>
  );
}

function flowOrder(flow: MarketOrderFlowItemView): OpenOrderView {
  return flow.action === "cancel" ? flow.cancelledOrder : flow.incomingOrder;
}

function flowResult(flow: MarketOrderFlowItemView): string {
  if (flow.action === "cancel") {
    return "Removed";
  }
  if (isZeroDecimal(flow.matchedQuantity)) {
    return "Resting";
  }
  return isZeroDecimal(flow.remainingQuantity)
    ? "Fully matched"
    : `${formatExactDecimal(flow.remainingQuantity)} resting`;
}

function bookSide(side: OpenOrderView["side"]): BookSide {
  return side === "buy" ? "bid" : "ask";
}

function priceOrDash(value: ObserverOrderBookView["bestBid"]): string {
  return value === null ? "—" : formatMarketPrice(value);
}
