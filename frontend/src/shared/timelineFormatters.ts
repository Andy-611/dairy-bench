import type {
  AttentionPlanView,
  DecisionStateChangeView,
  EconomicActionView,
  EconomicEffectView,
  QuoteAlertView,
  TimelineDecisionView,
  TurnTimelineItemView,
} from "./api/types";
import { formatExactDecimal } from "./format";
import { companyLabel, humanizeIdentifier, productLabel } from "./labels";
import { simulationDayLabel } from "./simulationCalendar";

export const ACCEPTED_BY_ENGINE_LABEL = "Accepted by Engine";
export const DECISION_PROCESSING_ORDER_LABEL = "Decision Processing Order";
export const ORDER_BOOK_PRIORITY_LABEL = "Order-book Priority";

export type DecisionKind = EconomicActionView["kind"] | "idle";

const DECISION_LABELS: Readonly<Record<DecisionKind, string>> = {
  idle: "Idle",
  produce: "Produce",
  set_quote_ladder: "Set quote ladder",
  set_retail_price: "Set retail price",
  transform: "Transform",
};

const WAKE_LABELS: Readonly<Record<string, string>> = {
  decision_day_started: "Decision day started",
  decision_rejected: "Previous decision was rejected",
  delivery_completed: "Delivery arrived",
  external_event: "Relevant external event",
  operation_completed: "Operation completed",
  price_alert: "Watched price reached",
  review_due: "Fallback review became due",
  trade_executed: "Own order executed",
  week_open: "Trading week opened",
};

const SYSTEM_LABELS: Readonly<Record<string, string>> = {
  consumer_sales: "Consumer sales",
  day_started: "Simulation day started",
  delivery_completed: "Delivery completed",
  market_close: "Continuous-market close",
  operation_completed: "Operation completed",
  turn_limit_reached: "Weekly turn limit reached",
  week_close: "Trading week close",
  week_open: "Trading week open",
};

export function decisionKind(decision: TimelineDecisionView): DecisionKind {
  return decision.kind === "idle" ? "idle" : decision.action.kind;
}

export function decisionLabel(kind: DecisionKind): string {
  return DECISION_LABELS[kind];
}

export function wakeLabel(reason: string): string {
  return WAKE_LABELS[reason] ?? humanizeIdentifier(reason);
}

export function systemLabel(kind: string): string {
  return SYSTEM_LABELS[kind] ?? humanizeIdentifier(kind);
}

export function decisionContextSummary(turn: TurnTimelineItemView): string {
  return `Decision based on economic state v${turn.stateVersion} · ${decisionProcessingOrderSummary(turn.applySequence)}`;
}

export function decisionProcessingOrderSummary(sequence: number): string {
  return `${DECISION_PROCESSING_ORDER_LABEL} #${sequence}`;
}

export function decisionDispositionLabel(turn: TurnTimelineItemView): string {
  if (turn.accepted) {
    return ACCEPTED_BY_ENGINE_LABEL;
  }
  return turn.dispositionSource === "economic_engine"
    ? "Rejected by Engine"
    : "Rejected by Runtime";
}

export function decisionProcessingResult(turn: TurnTimelineItemView): string {
  if (!turn.accepted) {
    const disposition = rejectedDisposition(turn.dispositionSource);
    return turn.reason === null ? disposition : `${disposition} · ${turn.reason}`;
  }
  return `Accepted by economic engine · ${acceptedDecisionResult(turn)}`;
}

export function nextDecisionTiming(turn: TurnTimelineItemView): string {
  const alerts = turn.decision.attention.alerts;
  const review = turn.reviewOn;
  if (review === null) {
    return alerts.length > 0
      ? "On a matching price alert or the next decision day"
      : "At the next decision day";
  }
  const at = simulationDayLabel(review);
  return alerts.length > 0
    ? `On a matching price alert, or fallback review at ${at}`
    : `Fallback review at ${at}`;
}

export function economicStateTransitionSummary(
  before: number | null,
  after: number | null,
): string {
  if (before === null || after === null) {
    return "Not available";
  }
  return before === after
    ? `Economic state unchanged · v${before}`
    : `v${before} → v${after}`;
}

export function decisionSummary(decision: TimelineDecisionView): string {
  return decision.kind === "idle" ? "Idle" : actionSummary(decision.action);
}

export function actionSummary(action: EconomicActionView): string {
  switch (action.kind) {
    case "produce":
      return `Produce ${formatExactDecimal(action.quantity)} ${productLabel(action.product)}`;
    case "transform":
      return `Transform ${formatExactDecimal(action.inputQuantity)} ${productLabel(action.inputProduct)} into ${productLabel(action.outputProduct)}`;
    case "set_quote_ladder":
      return quoteLadderSummary(action);
    case "set_retail_price":
      return `Set ${productLabel(action.product)} retail price to ${formatExactDecimal(action.unitPrice)}`;
  }
}

function acceptedDecisionResult(turn: TurnTimelineItemView): string {
  if (turn.decision.kind === "idle") {
    return "Economic state unchanged; attention plan armed";
  }
  switch (turn.decision.action.kind) {
    case "produce":
      return identifiedResult("Production job started", turn.outcomeJobId);
    case "transform":
      return identifiedResult("Transformation job started", turn.outcomeJobId);
    case "set_quote_ladder":
      return quoteLadderResultSummary(turn);
    case "set_retail_price":
      return "Retail price updated";
  }
}

function quoteLadderSummary(
  action: Extract<EconomicActionView, { readonly kind: "set_quote_ladder" }>,
): string {
  const scope = `${action.side} ${productLabel(action.product)} quote ladder`;
  if (action.levels.length === 0) {
    return `Clear ${scope}`;
  }
  const levels = action.levels
    .map(
      (level) =>
        `${formatExactDecimal(level.quantity)} @ ${formatExactDecimal(level.limitPrice)}`,
    )
    .join(", ");
  return `Set ${scope}: ${levels}`;
}

function quoteLadderResultSummary(turn: TurnTimelineItemView): string {
  const result = turn.quoteLadderResult;
  if (result === null) {
    return "Quote ladder reconciled";
  }
  const labels = { keep: "kept", place: "placed", replace: "replaced" } as const;
  const actions = (["keep", "place", "replace"] as const)
    .map((action) => ({
      action,
      count: result.levels.filter((level) => level.action === action).length,
    }))
    .filter(({ count }) => count > 0)
    .map(({ action, count }) => `${count} ${labels[action]}`);
  if (result.cancelledOrderIds.length > 0) {
    actions.push(`${result.cancelledOrderIds.length} cancelled`);
  }
  return actions.length === 0
    ? "Quote ladder already clear"
    : `Quote ladder reconciled: ${actions.join(", ")}`;
}

function identifiedResult(label: string, identifier: string | null): string {
  return identifier === null ? label : `${label}: ${identifier}`;
}

function rejectedDisposition(
  source: TurnTimelineItemView["dispositionSource"],
): string {
  switch (source) {
    case "economic_engine":
      return "Rejected by economic engine";
    case "runtime_attention":
      return "Rejected by runtime attention validation";
    case "runtime_protocol":
      return "Rejected by runtime protocol validation";
  }
}

export function quoteAlertSummary(alert: QuoteAlertView): string {
  const quote = alert.quote === "best_bid" ? "best bid" : "best ask";
  const operator = alert.operator === "at_least" ? "≥" : "≤";
  return `${productLabel(alert.product)} ${quote} ${operator} ${formatExactDecimal(alert.price)}`;
}

export function attentionFallbackSummary(attention: AttentionPlanView): string {
  return attention.reviewAfterDays === null
    ? "use the runtime's bounded fallback review"
    : `review after ${attention.reviewAfterDays} ${plural(attention.reviewAfterDays, "day")}`;
}

export function effectSummary(effect: EconomicEffectView): string {
  switch (effect.kind) {
    case "milk_produced":
      return `${companyLabel(effect.companyId)} produced ${formatExactDecimal(effect.actualQuantity)} raw milk`;
    case "milk_processed":
      return `${companyLabel(effect.companyId)} converted ${formatExactDecimal(effect.actualInput)} raw milk into ${formatExactDecimal(effect.outputQuantity)} bottled milk`;
    case "trade_executed":
      return `${companyLabel(effect.sellerId)} sold ${formatExactDecimal(effect.quantity)} ${productLabel(effect.product)} to ${companyLabel(effect.buyerId)} at ${formatExactDecimal(effect.unitPrice)}`;
    case "delivery_completed":
      return `${companyLabel(effect.companyId)} received ${formatExactDecimal(effect.quantity)} ${productLabel(effect.product)}`;
    case "consumer_sale":
      return `${companyLabel(effect.companyId)} sold ${formatExactDecimal(effect.soldQuantity)} to consumers for ${formatExactDecimal(effect.revenue)}`;
    case "retail_operating_cost_charged":
      return `${companyLabel(effect.companyId)} opened with ${formatExactDecimal(effect.openingPayable)} payable, accrued ${formatExactDecimal(effect.costAccrued)} in store operating cost, paid ${formatExactDecimal(effect.cashPaid)}, and closed with ${formatExactDecimal(effect.closingPayable)} payable`;
    case "inventory_expired":
      return `${companyLabel(effect.companyId)} discarded ${formatExactDecimal(effect.quantity)} ${productLabel(effect.product)}`;
    case "company_bankrupt":
      return `${companyLabel(effect.companyId)} exited with ${formatExactDecimal(effect.totalAssets)} in total assets`;
  }
}

export function stateChangeSummary(change: DecisionStateChangeView): string {
  switch (change.changeType) {
    case "order_placed":
      return `${capitalize(change.side)} order ${shortId(change.orderId)} · ${formatExactDecimal(change.quantity)} ${productLabel(change.product)} at ${formatExactDecimal(change.limitPrice)}`;
    case "order_cancelled":
      return `Order ${shortId(change.orderId)} was cancelled`;
    case "order_replaced":
      return `Order ${shortId(change.replacedOrderId)} was replaced by ${shortId(change.orderId)} for ${formatExactDecimal(change.quantity)} units at ${formatExactDecimal(change.limitPrice)}`;
    case "retail_price_changed":
      return `${productLabel(change.product)} price ${change.before === null ? "not set" : formatExactDecimal(change.before)} → ${formatExactDecimal(change.after)}`;
  }
}

export function stateChangeKey(change: DecisionStateChangeView): string {
  return change.changeType === "retail_price_changed"
    ? `${change.changeType}-${change.product}`
    : `${change.changeType}-${change.orderId}`;
}

export function shortId(value: string): string {
  return value.length > 16 ? `${value.slice(0, 8)}…${value.slice(-5)}` : value;
}

export function plural(count: number, noun: string): string {
  return count === 1 ? noun : `${noun}s`;
}

function capitalize(value: string): string {
  return value.length === 0 ? value : value[0]!.toUpperCase() + value.slice(1);
}
