export type CompanyRole = "farm" | "processor" | "retailer";
export type CompanyStatus = "active" | "bankrupt";
export type PolicyMode = "baseline" | "model" | "replay";
export type InvocationOutcome =
  | "success"
  | "agent_error"
  | "infrastructure_error";
export type RunStatus =
  | "queued"
  | "running"
  | "interrupted"
  | "completed"
  | "failed"
  | "stopped";
export type RunStopReason = "user_requested" | "quota_exhausted";

declare const decimalTextBrand: unique symbol;
export type DecimalText = string & { readonly [decimalTextBrand]: true };

export type JsonValue =
  | boolean
  | number
  | string
  | null
  | JsonValue[]
  | { readonly [key: string]: JsonValue };

export interface ScoreView {
  readonly finalScore: DecimalText;
  readonly efficiencyRaw: DecimalText;
  readonly efficiencyReference: DecimalText;
  readonly efficiencyScore: DecimalText;
  readonly globalGini: DecimalText;
  readonly fairnessScore: DecimalText;
  readonly profitParticipationScore: DecimalText;
  readonly bankruptCompanyCount: number;
  readonly lossMakingCompanyCount: number;
}

export interface TokenUsageView {
  readonly inputTokens: number;
  readonly cachedTokens: number;
  readonly outputTokens: number;
  readonly reasoningTokens: number;
  readonly totalTokens: number;
}

export type ProtocolIssueKind =
  | "context_too_large"
  | "invalid_arguments"
  | "invalid_response"
  | "missing_tool_call"
  | "multiple_tool_calls"
  | "unauthorized_decision_tool";

export interface ProtocolIssueCountView {
  readonly kind: ProtocolIssueKind;
  readonly count: number;
}

export interface EpisodeQualityView {
  readonly benchmarkEligible: boolean;
  readonly totalTurnCount: number;
  readonly invalidTurnCount: number;
  readonly issues: readonly ProtocolIssueCountView[];
}

export interface AgentUsageSummaryView {
  readonly invocationCount: number;
  readonly successfulInvocations: number;
  readonly providers: readonly string[];
  readonly models: readonly string[];
  readonly usage: TokenUsageView;
}

export interface CompanyResultView {
  readonly companyId: string;
  readonly companyName: string;
  readonly role: CompanyRole;
  readonly status: CompanyStatus;
  readonly policyName: string;
  readonly initialCash: DecimalText;
  readonly finalCash: DecimalText;
  readonly inventoryValue: DecimalText;
  readonly surplus: DecimalText;
  readonly growth: DecimalText;
}

export interface WeeklySnapshotView {
  readonly week: number;
  readonly companyId: string;
  readonly cash: DecimalText;
  readonly inventoryValue: DecimalText;
  readonly cumulativeSurplus: DecimalText;
  readonly consumerSalesQuantity: DecimalText;
  readonly expiredQuantity: DecimalText;
}

export interface EpisodeView {
  readonly runId: string;
  readonly scenarioId: string;
  readonly seed: number;
  readonly weeks: number;
  readonly score: ScoreView;
  readonly quality: EpisodeQualityView;
  readonly agentUsage: AgentUsageSummaryView | null;
  readonly companies: readonly CompanyResultView[];
  readonly snapshots: readonly WeeklySnapshotView[];
}

export interface PolicyProfileView {
  readonly mode: PolicyMode;
  readonly label: string;
  readonly available: boolean;
  readonly provider: string | null;
  readonly model: string | null;
  readonly models: readonly string[];
  readonly description: string;
  readonly unavailableReason: string | null;
}

export interface RunProgressView {
  readonly runId: string;
  readonly status: RunStatus;
  readonly currentAbsoluteDay: number;
  readonly totalWeeks: number;
  readonly errorMessage: string | null;
}

export interface RunJobView extends RunProgressView {
  readonly revision: number;
  readonly mode: PolicyMode;
  readonly model: string | null;
  readonly seed: number;
  readonly sourceRunId: string | null;
  readonly scenarioId: string;
  readonly submittedAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
  readonly stopReason: RunStopReason | null;
  readonly quality: EpisodeQualityView | null;
}

export interface ReplaySourceView {
  readonly runId: string;
  readonly submittedAt: string;
  readonly benchmarkEligible: boolean;
}

export interface RunDiagnosticsView {
  readonly completedWeeks: number;
  readonly benchmarkEligible: boolean | null;
  readonly protocolInvalidTurns: number;
  readonly economicRejections: number;
  readonly attentionRejections: number;
  readonly tradeCount: number;
  readonly lastTradeWeek: number | null;
  readonly zeroTradeWeekStreak: number;
  readonly consumerDemand: DecimalText;
  readonly consumerSales: DecimalText;
  readonly consumerFillRate: DecimalText;
  readonly expiredQuantity: DecimalText;
  readonly nearInsolventCompanyIds: readonly string[];
}

export interface TimelineContextView {
  readonly currentRunId: string;
  readonly scenarioId: string;
  readonly scenarioVersion: number;
  readonly totalWeeks: number;
  readonly status: RunStatus;
  readonly mode: string;
  readonly isReplay: boolean;
  readonly sourceRunId: string | null;
  readonly traceRunId: string;
  readonly currentModelCallCount: number;
  readonly sourceModelCallCount: number;
  readonly currentUsage: TokenUsageView;
  readonly sourceUsage: TokenUsageView;
  readonly checkpointDay: SimDayView | null;
  readonly checkpointStateVersion: number | null;
  readonly diagnostics: RunDiagnosticsView;
}

export interface TimelineWeekSummaryView {
  readonly week: number;
  readonly turnCount: number;
  readonly acceptedCount: number;
  readonly rejectedCount: number;
  readonly idleCount: number;
  readonly systemStepCount: number;
  readonly eventCount: number;
  readonly tradeQuantity: DecimalText;
  readonly consumerSales: DecimalText;
  readonly expiredQuantity: DecimalText;
}

export interface WakeSignalView {
  readonly reason: string;
  readonly sourceEntryId: string | null;
  readonly sourceEntryType: "turn" | "system_step" | null;
  readonly referenceIds: readonly string[];
}

export interface QuoteAlertView {
  readonly product: string;
  readonly quote: "best_ask" | "best_bid";
  readonly operator: "at_least" | "at_most";
  readonly price: DecimalText;
}

export type EconomicEffectView =
  | {
      readonly kind: "milk_produced";
      readonly companyId: string;
      readonly requestedQuantity: DecimalText;
      readonly actualQuantity: DecimalText;
      readonly unitCost: DecimalText;
      readonly cashCost: DecimalText;
    }
  | {
      readonly kind: "trade_executed";
      readonly tradeId: string;
      readonly sellerId: string;
      readonly buyerId: string;
      readonly product: string;
      readonly quantity: DecimalText;
      readonly unitPrice: DecimalText;
      readonly totalValue: DecimalText;
    }
  | {
      readonly kind: "delivery_completed";
      readonly tradeId: string;
      readonly companyId: string;
      readonly product: string;
      readonly quantity: DecimalText;
    }
  | {
      readonly kind: "milk_processed";
      readonly companyId: string;
      readonly requestedInput: DecimalText;
      readonly actualInput: DecimalText;
      readonly outputQuantity: DecimalText;
      readonly cashCost: DecimalText;
    }
  | {
      readonly kind: "consumer_sale";
      readonly companyId: string;
      readonly potentialDemand: DecimalText;
      readonly demandQuantity: DecimalText;
      readonly soldQuantity: DecimalText;
      readonly retailPrice: DecimalText | null;
      readonly revenue: DecimalText;
    }
  | {
      readonly kind: "inventory_expired";
      readonly companyId: string;
      readonly product: string;
      readonly quantity: DecimalText;
      readonly valueLoss: DecimalText;
    }
  | {
      readonly kind: "company_bankrupt";
      readonly companyId: string;
      readonly totalAssets: DecimalText;
      readonly cancelledOrderIds: readonly string[];
      readonly retailPriceRemoved: boolean;
    }
;

export type EconomicActionView =
  | {
      readonly kind: "produce";
      readonly product: string;
      readonly quantity: DecimalText;
    }
  | {
      readonly kind: "transform";
      readonly inputProduct: string;
      readonly outputProduct: string;
      readonly inputQuantity: DecimalText;
    }
  | {
      readonly kind: "set_quote_ladder";
      readonly side: "buy" | "sell";
      readonly product: string;
      readonly levels: readonly QuoteLevelView[];
    }
  | {
      readonly kind: "set_retail_price";
      readonly product: string;
      readonly unitPrice: DecimalText;
    };

export interface AttentionPlanView {
  readonly reviewAfterDays: number | null;
  readonly alerts: readonly QuoteAlertView[];
}

export type TimelineDecisionView =
  | {
      readonly kind: "action";
      readonly action: EconomicActionView;
      readonly attention: AttentionPlanView;
    }
  | {
      readonly kind: "idle";
      readonly attention: AttentionPlanView;
    };

export type DecisionStateChangeView =
  | {
      readonly changeType: "order_placed";
      readonly orderId: string;
      readonly side: "buy" | "sell";
      readonly product: string;
      readonly quantity: DecimalText;
      readonly limitPrice: DecimalText;
    }
  | {
      readonly changeType: "order_cancelled";
      readonly orderId: string;
    }
  | {
      readonly changeType: "order_replaced";
      readonly replacedOrderId: string;
      readonly orderId: string;
      readonly quantity: DecimalText;
      readonly limitPrice: DecimalText;
    }
  | {
      readonly changeType: "retail_price_changed";
      readonly product: string;
      readonly before: DecimalText | null;
      readonly after: DecimalText;
    };

export interface ObservationFactsView {
  readonly cash: DecimalText;
  readonly reservedCash: DecimalText;
  readonly markedSurplus: DecimalText;
  readonly inventory: Readonly<Record<string, DecimalText>>;
  readonly inventoryExpiry: readonly InventoryExpiryBucketView[];
  readonly retailPrice: DecimalText | null;
  readonly openOrders: readonly OpenOrderView[];
  readonly orderBooks: readonly OrderBookView[];
  readonly pendingDeliveries: readonly IncomingDeliveryView[];
  readonly activeOperation: OperationJobView | null;
  readonly remainingOperationCapacity: DecimalText | null;
  readonly visibleEventCount: number;
  readonly visibleEvents: readonly EconomicEffectView[];
}

export interface OpenOrderView {
  readonly orderId: string;
  readonly ownerId: string;
  readonly side: "buy" | "sell";
  readonly product: string;
  readonly remainingQuantity: DecimalText;
  readonly limitPrice: DecimalText;
  readonly placedOn: SimDayView;
  readonly prioritySequence: number;
  readonly queueAheadQuantity: DecimalText;
}

export interface PriceLevelView {
  readonly unitPrice: DecimalText;
  readonly quantity: DecimalText;
  readonly orderCount: number;
}

export interface QuoteLevelView {
  readonly quantity: DecimalText;
  readonly limitPrice: DecimalText;
}

export interface QuoteLadderLevelResultView {
  readonly level: QuoteLevelView;
  readonly action: "keep" | "place" | "replace";
  readonly orderId: string;
  readonly replacedOrderId: string | null;
  readonly prioritySequence: number;
  readonly remainingQuantity: DecimalText;
}

export interface QuoteLadderResultView {
  readonly levels: readonly QuoteLadderLevelResultView[];
  readonly cancelledOrderIds: readonly string[];
}

export interface OrderBookView {
  readonly product: string;
  readonly bids: readonly PriceLevelView[];
  readonly asks: readonly PriceLevelView[];
  readonly lastTradePrice: DecimalText | null;
  readonly weeklyVolume: DecimalText;
}

export interface InventoryExpiryBucketView {
  readonly product: string;
  readonly expiresEndOfWeek: number;
  readonly availableQuantity: DecimalText;
  readonly reservedQuantity: DecimalText;
}

export interface MarketPriceLevelView {
  readonly unitPrice: DecimalText;
  readonly size: DecimalText;
  readonly orders: readonly OpenOrderView[];
}

export interface ObserverOrderBookView {
  readonly product: string;
  readonly bids: readonly MarketPriceLevelView[];
  readonly asks: readonly MarketPriceLevelView[];
  readonly lastTradePrice: DecimalText | null;
  readonly bestBid: DecimalText | null;
  readonly bestAsk: DecimalText | null;
  readonly spread: DecimalText | null;
}

export interface MarketMatchLegView {
  readonly tradeId: string;
  readonly makerOrder: OpenOrderView;
  readonly quantity: DecimalText;
  readonly unitPrice: DecimalText;
  readonly makerRemainingQuantity: DecimalText;
  readonly makerWithdrawnQuantity: DecimalText;
}

interface AppliedMarketOrderView {
  readonly applySequence: number;
  readonly incomingOrder: OpenOrderView;
  readonly matches: readonly MarketMatchLegView[];
  readonly matchedQuantity: DecimalText;
  readonly remainingQuantity: DecimalText;
  readonly withdrawnQuantity: DecimalText;
}

export interface MarketOrderPlacedView extends AppliedMarketOrderView {
  readonly action: "place";
}

export interface MarketOrderReplacedView extends AppliedMarketOrderView {
  readonly action: "replace";
  readonly replacedOrder: OpenOrderView;
}

export interface MarketOrderCancelledView {
  readonly action: "cancel";
  readonly applySequence: number;
  readonly cancelledOrder: OpenOrderView;
}

export interface MarketOrderKeptView {
  readonly action: "keep";
  readonly applySequence: number;
  readonly preservedOrder: OpenOrderView;
}

export type MarketOrderFlowItemView =
  | MarketOrderPlacedView
  | MarketOrderReplacedView
  | MarketOrderCancelledView
  | MarketOrderKeptView;

export interface TimelineTradeView {
  readonly applySequence: number;
  readonly tradeId: string;
  readonly makerOrderId: string;
  readonly takerOrderId: string;
  readonly product: string;
  readonly sellerId: string;
  readonly buyerId: string;
  readonly quantity: DecimalText;
  readonly unitPrice: DecimalText;
  readonly arrivesOn: SimDayView;
}

export interface MarketFrameView {
  readonly stateVersion: number;
  readonly orderFlow: readonly MarketOrderFlowItemView[];
  readonly trades: readonly TimelineTradeView[];
  readonly closingOrderBooks: readonly ObserverOrderBookView[];
}

export interface IncomingDeliveryView {
  readonly tradeId: string;
  readonly product: string;
  readonly quantity: DecimalText;
  readonly arrivesOn: SimDayView;
  readonly expiryBuckets: readonly DeliveryExpiryBucketView[];
}

export interface DeliveryExpiryBucketView {
  readonly quantity: DecimalText;
  readonly expiresEndOfWeek: number;
}

export interface OperationJobView {
  readonly jobId: string;
  readonly kind: "production" | "transformation";
  readonly completesOn: SimDayView;
  readonly outputProduct: string;
  readonly outputQuantity: DecimalText;
}

export interface InventoryDeltaView {
  readonly product: string;
  readonly before: DecimalText | null;
  readonly after: DecimalText;
  readonly change: DecimalText | null;
}

export interface ObservationDeltaView {
  readonly cashBefore: DecimalText | null;
  readonly cashAfter: DecimalText;
  readonly cashChange: DecimalText | null;
  readonly inventory: readonly InventoryDeltaView[];
  readonly retailPriceBefore: DecimalText | null;
  readonly retailPriceAfter: DecimalText | null;
  readonly openOrderCountBefore: number | null;
  readonly openOrderCountAfter: number;
}

export interface TracePreviewView {
  readonly traceRunId: string;
  readonly invocationId: string;
  readonly isSourceTrace: boolean;
  readonly provider: string;
  readonly model: string;
  readonly outcome: InvocationOutcome;
  readonly usage: TokenUsageView;
  readonly latencyMs: number;
  readonly attempts: number;
  readonly appliedToCommittedTurn: boolean;
}

export type DecisionDispositionSource =
  | "economic_engine"
  | "runtime_attention"
  | "runtime_protocol";

export interface TurnTimelineItemView {
  readonly entryType: "turn";
  readonly entryId: string;
  readonly companyId: string;
  readonly companyName: string;
  readonly role: CompanyRole;
  readonly simDay: SimDayView;
  readonly stateVersion: number;
  readonly applySequence: number;
  readonly journalSequence: number | null;
  readonly turnNumberThisWeek: number;
  readonly turnLimitThisWeek: number;
  readonly wakeSignals: readonly WakeSignalView[];
  readonly observation: ObservationFactsView;
  readonly observationDelta: ObservationDeltaView;
  readonly decision: TimelineDecisionView;
  readonly accepted: boolean;
  readonly dispositionSource: DecisionDispositionSource;
  readonly reason: string | null;
  readonly resultingStateVersion: number;
  readonly quoteLadderResult: QuoteLadderResultView | null;
  readonly outcomeJobId: string | null;
  readonly effects: readonly EconomicEffectView[];
  readonly stateChanges: readonly DecisionStateChangeView[];
  readonly nextAvailableOn: SimDayView | null;
  readonly reviewOn: SimDayView | null;
  readonly sourceRunId: string | null;
  readonly sourceTurnId: string | null;
  readonly traces: readonly TracePreviewView[];
  readonly protocolError: string | null;
  readonly title: string;
  readonly summary: string;
}

export interface SystemTimelineItemView {
  readonly entryType: "system";
  readonly entryId: string;
  readonly simDay: SimDayView;
  readonly kind: string;
  readonly journalSequence: number | null;
  readonly stateVersionBefore: number | null;
  readonly stateVersionAfter: number | null;
  readonly referenceIds: readonly string[];
  readonly effects: readonly EconomicEffectView[];
  readonly affectedCompanyIds: readonly string[];
  readonly title: string;
  readonly summary: string;
}

export interface SimDayView {
  readonly absoluteDay: number;
}

export interface TimelineDayFrameView {
  readonly simDay: SimDayView;
  readonly systemSteps: readonly SystemTimelineItemView[];
  readonly turns: readonly TurnTimelineItemView[];
  readonly market: MarketFrameView;
}

export interface TimelineWeekView {
  readonly context: TimelineContextView;
  readonly selectedWeek: number;
  readonly weekSummaries: readonly TimelineWeekSummaryView[];
  readonly days: readonly TimelineDayFrameView[];
}

export interface AgentTraceView {
  readonly preview: TracePreviewView;
}

export interface TimelineDetailView {
  readonly entry: TurnTimelineItemView | SystemTimelineItemView;
  readonly context: TimelineContextView;
  readonly turnRecord: JsonValue | null;
  readonly systemStepRecord: JsonValue | null;
  readonly traces: readonly AgentTraceView[];
}

export type RunRequest =
  | {
      readonly policyMode: "baseline";
      readonly seed: number;
    }
  | {
      readonly policyMode: "model";
      readonly model: string;
      readonly seed: number;
    }
  | {
      readonly policyMode: "replay";
      readonly sourceRunId: string;
    };
