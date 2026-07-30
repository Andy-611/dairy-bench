import { companyLabel, humanizeIdentifier, productLabel } from "./domainLabels";
import { formatValue } from "./format";
import type {
  CommandStateChangeView,
  EconomicEffectView,
  TimelineCommandView,
} from "./types";

const COMMAND_LABELS: Readonly<Record<TimelineCommandView["kind"], string>> = {
  cancel_order: "Cancel order",
  place_order: "Place order",
  produce: "Produce",
  set_retail_price: "Set retail price",
  transform: "Transform",
  wait: "Wait",
};

const WAKE_LABELS: Readonly<Record<string, string>> = {
  continue: "Previous action completed",
  day_open: "Market day opened",
  external_event: "Relevant external event",
  market_cleared: "Spot market cleared",
  wait_expired: "Requested wait expired",
};

const SYSTEM_LABELS: Readonly<Record<string, string>> = {
  consumer_sales: "Consumer sales",
  day_close: "Day close",
  day_open: "Day open",
  market_clear: "Spot-market clearing",
  production_completed: "Production completed",
  run_end: "Run completed",
  transformation_completed: "Transformation completed",
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

export function commandSummary(command: TimelineCommandView): string {
  switch (command.kind) {
    case "produce":
      return `Produce ${formatValue(command.quantity)} ${productLabel(command.product)}`;
    case "transform":
      return `Transform ${formatValue(command.inputQuantity)} ${productLabel(command.inputProduct)} into ${productLabel(command.outputProduct)}`;
    case "place_order":
      return `${capitalize(command.side)} ${formatValue(command.quantity)} ${productLabel(command.product)} at a ${formatValue(command.limitPrice)} limit`;
    case "cancel_order":
      return `Cancel order ${command.orderId}`;
    case "set_retail_price":
      return `Set ${productLabel(command.product)} retail price to ${formatValue(command.unitPrice)}`;
    case "wait":
      return command.untilMinute === null
        ? "Wait for the next relevant event"
        : `Wait until ${clockTime(command.untilMinute)}`;
  }
}

export function effectSummary(effect: EconomicEffectView): string {
  switch (effect.kind) {
    case "milk_produced":
      return `${companyLabel(effect.companyId)} produced ${formatValue(effect.actualQuantity)} raw milk`;
    case "milk_processed":
      return `${companyLabel(effect.companyId)} converted ${formatValue(effect.actualInput)} raw milk into ${formatValue(effect.outputQuantity)} bottled milk`;
    case "trade_executed":
      return `${companyLabel(effect.sellerId)} sold ${formatValue(effect.quantity)} ${productLabel(effect.product)} to ${companyLabel(effect.buyerId)} at ${formatValue(effect.unitPrice)}`;
    case "consumer_sale":
      return `${companyLabel(effect.companyId)} sold ${formatValue(effect.soldQuantity)} to consumers for ${formatValue(effect.revenue)}`;
    case "inventory_expired":
      return `${companyLabel(effect.companyId)} discarded ${formatValue(effect.quantity)} ${productLabel(effect.product)}`;
    case "decision_rejected":
    case "policy_failed":
      return effect.reason;
  }
}

export function stateChangeSummary(change: CommandStateChangeView): string {
  switch (change.changeType) {
    case "order_placed":
      return `${capitalize(change.side)} order ${shortId(change.orderId)} · ${formatValue(change.quantity)} ${productLabel(change.product)} at ${formatValue(change.limitPrice)}`;
    case "order_cancelled":
      return `Order ${shortId(change.orderId)} was cancelled`;
    case "retail_price_changed":
      return `${productLabel(change.product)} price ${change.before === null ? "not set" : formatValue(change.before)} → ${formatValue(change.after)}`;
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
