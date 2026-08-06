import { companyLabel, humanizeIdentifier, productLabel } from "./domainLabels";
import { formatExactDecimal } from "./format";
import type {
  CommandStateChangeView,
  EconomicEffectView,
  QuoteAlertView,
  TimelineCommandView,
  TurnTimelineItemView,
} from "./types";

export const ACCEPTED_BY_ENGINE_LABEL = "Accepted by Engine";
export const COMMAND_PROCESSING_ORDER_LABEL = "Command Processing Order";
export const ORDER_BOOK_PRIORITY_LABEL = "Order-book Priority";

const COMMAND_LABELS: Readonly<Record<TimelineCommandView["kind"], string>> = {
  produce: "Produce",
  set_quote_ladder: "Set quote ladder",
  set_retail_price: "Set retail price",
  transform: "Transform",
  wait: "Wait",
};

const WAKE_LABELS: Readonly<Record<string, string>> = {
  command_rejected: "Previous command was rejected",
  continue: "Decision interval elapsed",
  day_open: "Market day opened",
  delivery_completed: "Delivery arrived",
  external_event: "Relevant external event",
  operation_completed: "Operation completed",
  price_alert: "Watched price reached",
  trade_executed: "Own order executed",
  turn_limit_reached: "Daily turn limit reached",
  wait_expired: "Fallback review became due",
};

const SYSTEM_LABELS: Readonly<Record<string, string>> = {
  consumer_sales: "Consumer sales",
  day_close: "Day close",
  day_open: "Day open",
  delivery_completed: "Delivery completed",
  market_close: "Continuous-market close",
  operation_completed: "Operation completed",
  turn_limit_reached: "Daily turn limit reached",
};

export function commandLabel(kind: TimelineCommandView["kind"]): string {
  return COMMAND_LABELS[kind];
}

export function wakeLabel(reason: string): string {
  return WAKE_LABELS[reason] ?? humanizeIdentifier(reason);
}

export function systemLabel(kind: string): string {
  return SYSTEM_LABELS[kind] ?? humanizeIdentifier(kind);
}

export function decisionContextSummary(turn: TurnTimelineItemView): string {
  return `Decision based on economic state v${turn.stateVersion} · ${commandProcessingOrderSummary(turn.applySequence)}`;
}

export function commandProcessingOrderSummary(sequence: number): string {
  return `${COMMAND_PROCESSING_ORDER_LABEL} #${sequence}`;
}

export function commandDispositionLabel(turn: TurnTimelineItemView): string {
  if (turn.accepted) {
    return ACCEPTED_BY_ENGINE_LABEL;
  }
  return turn.dispositionSource === "economic_engine"
    ? "Rejected by Engine"
    : "Rejected by Runtime";
}

export function commandProcessingResult(turn: TurnTimelineItemView): string {
  if (!turn.accepted) {
    const disposition = rejectedDisposition(turn.dispositionSource);
    return turn.reason === null ? disposition : `${disposition} · ${turn.reason}`;
  }
  return `Accepted by economic engine · ${acceptedCommandResult(turn)}`;
}

export function nextDecisionTiming(turn: TurnTimelineItemView): string {
  if (turn.nextAvailableMinute === null) {
    return "No same-day follow-up scheduled";
  }
  const time = clockTime(turn.nextAvailableMinute);
  if (turn.accepted && turn.command.kind === "wait") {
    return turn.command.alerts.length > 0
      ? `Price alert, or fallback review at ${time}`
      : `Fallback review at ${time}`;
  }
  return `Routine follow-up at ${time}`;
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

export function commandSummary(command: TimelineCommandView): string {
  switch (command.kind) {
    case "produce":
      return `Produce ${formatExactDecimal(command.quantity)} ${productLabel(command.product)}`;
    case "transform":
      return `Transform ${formatExactDecimal(command.inputQuantity)} ${productLabel(command.inputProduct)} into ${productLabel(command.outputProduct)}`;
    case "set_quote_ladder":
      return quoteLadderSummary(command);
    case "set_retail_price":
      return `Set ${productLabel(command.product)} retail price to ${formatExactDecimal(command.unitPrice)}`;
    case "wait":
      return waitSummary(command);
  }
}

function acceptedCommandResult(turn: TurnTimelineItemView): string {
  switch (turn.command.kind) {
    case "produce":
      return identifiedResult("Production job started", turn.outcomeJobId);
    case "transform":
      return identifiedResult("Transformation job started", turn.outcomeJobId);
    case "set_quote_ladder":
      return quoteLadderResultSummary(turn);
    case "set_retail_price":
      return "Retail price updated";
    case "wait":
      return "Attention plan armed";
  }
}

function quoteLadderSummary(
  command: Extract<TimelineCommandView, { readonly kind: "set_quote_ladder" }>,
): string {
  const scope = `${command.side} ${productLabel(command.product)} quote ladder`;
  if (command.levels.length === 0) {
    return `Clear ${scope}`;
  }
  const levels = command.levels
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
  const labels = {
    keep: "kept",
    place: "placed",
    replace: "replaced",
  } as const;
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

export function waitFallbackSummary(
  command: Extract<TimelineCommandView, { readonly kind: "wait" }>,
): string {
  return command.untilMinute === null
    ? "use the runtime's bounded fallback review"
    : `review at ${clockTime(command.untilMinute)}`;
}

function waitSummary(
  command: Extract<TimelineCommandView, { readonly kind: "wait" }>,
): string {
  const fallback = waitFallbackSummary(command);
  if (command.alerts.length === 0) {
    return `Wait · ${fallback}`;
  }
  return `Watch ${command.alerts.map(quoteAlertSummary).join(" or ")} · fallback ${fallback}`;
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
    case "inventory_expired":
      return `${companyLabel(effect.companyId)} discarded ${formatExactDecimal(effect.quantity)} ${productLabel(effect.product)}`;
    case "decision_rejected":
    case "policy_failed":
      return effect.reason;
  }
}

export function stateChangeSummary(change: CommandStateChangeView): string {
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

export function stateChangeKey(change: CommandStateChangeView): string {
  return change.changeType === "retail_price_changed"
    ? `${change.changeType}-${change.product}`
    : `${change.changeType}-${change.orderId}`;
}

export function clockTime(absoluteMinute: number): string {
  const minuteOfDay = absoluteMinute % (24 * 60);
  const hour = Math.floor(minuteOfDay / 60);
  const minute = minuteOfDay % 60;
  return `${String(hour).padStart(2, "0")}:${String(minute).padStart(2, "0")}`;
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
