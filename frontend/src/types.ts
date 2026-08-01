export type CompanyRole = "farm" | "processor" | "retailer";
export type PolicyMode = "baseline" | "codex" | "openai" | "replay";
export type InvocationOutcome =
  | "success"
  | "agent_error"
  | "infrastructure_error";
export type RunStatus =
  | "queued"
  | "running"
  | "interrupted"
  | "completed"
  | "failed";

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
  readonly eligible: boolean;
  readonly efficiency: number;
  readonly fairness: number;
  readonly fulfillmentRate: number;
  readonly expiredQuantity: number;
}

export interface TokenUsageView {
  readonly inputTokens: number;
  readonly cachedTokens: number;
  readonly outputTokens: number;
  readonly reasoningTokens: number;
  readonly totalTokens: number;
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
  readonly policyName: string;
  readonly initialCash: number;
  readonly finalCash: number;
  readonly inventoryValue: number;
  readonly surplus: number;
  readonly growth: number;
}

export interface DailySnapshotView {
  readonly day: number;
  readonly companyId: string;
  readonly cash: number;
  readonly inventoryValue: number;
  readonly cumulativeSurplus: number;
  readonly consumerSalesQuantity: number;
  readonly expiredQuantity: number;
}

export interface EpisodeView {
  readonly runId: string;
  readonly scenarioId: string;
  readonly seed: number;
  readonly days: number;
  readonly score: ScoreView;
  readonly agentUsage: AgentUsageSummaryView | null;
  readonly companies: readonly CompanyResultView[];
  readonly snapshots: readonly DailySnapshotView[];
}

export interface PolicyProfileView {
  readonly mode: PolicyMode;
  readonly label: string;
  readonly available: boolean;
  readonly provider: string | null;
  readonly model: string | null;
  readonly reasoningEffort: string | null;
  readonly description: string;
  readonly unavailableReason: string | null;
}

export interface RunProgressView {
  readonly runId: string;
  readonly status: RunStatus;
  readonly currentDay: number;
  readonly totalDays: number;
  readonly errorMessage: string | null;
}

export interface RunJobView extends RunProgressView {
  readonly revision: number;
  readonly mode: PolicyMode;
  readonly seed: number;
  readonly sourceRunId: string | null;
  readonly scenarioId: string;
  readonly submittedAt: string;
  readonly startedAt: string | null;
  readonly finishedAt: string | null;
}

export interface TimelineContextView {
  readonly currentRunId: string;
  readonly scenarioId: string;
  readonly scenarioVersion: number;
  readonly totalDays: number;
  readonly mode: string;
  readonly isReplay: boolean;
  readonly sourceRunId: string | null;
  readonly traceRunId: string;
  readonly currentModelCallCount: number;
  readonly sourceModelCallCount: number;
  readonly currentUsage: TokenUsageView;
  readonly sourceUsage: TokenUsageView;
  readonly checkpointMinute: number | null;
  readonly checkpointStateVersion: number | null;
}

export interface TimelineDaySummaryView {
  readonly day: number;
  readonly turnCount: number;
  readonly acceptedCount: number;
  readonly rejectedCount: number;
  readonly waitCount: number;
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
      readonly kind: "decision_rejected" | "policy_failed";
      readonly companyId: string;
      readonly reason: string;
    };

export type TimelineCommandView =
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
      readonly kind: "place_order";
      readonly side: "buy" | "sell";
      readonly product: string;
      readonly quantity: DecimalText;
      readonly limitPrice: DecimalText;
    }
  | {
      readonly kind: "cancel_order";
      readonly orderId: string;
    }
  | {
      readonly kind: "replace_order";
      readonly orderId: string;
      readonly quantity: DecimalText;
      readonly limitPrice: DecimalText;
    }
  | {
      readonly kind: "set_retail_price";
      readonly product: string;
      readonly unitPrice: DecimalText;
    }
  | {
      readonly kind: "wait";
      readonly untilMinute: number | null;
      readonly alerts: readonly QuoteAlertView[];
    };

export type CommandStateChangeView =
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
  readonly inventory: Readonly<Record<string, DecimalText>>;
  readonly reservedInventory: Readonly<Record<string, DecimalText>>;
  readonly retailPrice: DecimalText | null;
  readonly openOrders: readonly OpenOrderView[];
  readonly marketViews: readonly MarketView[];
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
  readonly placedAtMinute: number;
  readonly prioritySequence: number;
}

export interface PriceLevelView {
  readonly unitPrice: DecimalText;
  readonly quantity: DecimalText;
}

export interface MarketView {
  readonly product: string;
  readonly bestBid: DecimalText | null;
  readonly bestAsk: DecimalText | null;
  readonly topBids: readonly PriceLevelView[];
  readonly topAsks: readonly PriceLevelView[];
  readonly lastTradePrice: DecimalText | null;
  readonly dailyVolume: DecimalText;
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
}

interface AppliedMarketOrderView {
  readonly applySequence: number;
  readonly incomingOrder: OpenOrderView;
  readonly matches: readonly MarketMatchLegView[];
  readonly matchedQuantity: DecimalText;
  readonly remainingQuantity: DecimalText;
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

export type MarketOrderFlowItemView =
  | MarketOrderPlacedView
  | MarketOrderReplacedView
  | MarketOrderCancelledView;

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
  readonly arrivesAtMinute: number;
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
  readonly arrivesAtMinute: number;
  readonly expiryBuckets: readonly DeliveryExpiryBucketView[];
}

export interface DeliveryExpiryBucketView {
  readonly quantity: DecimalText;
  readonly expiresEndOfDay: number;
}

export interface OperationJobView {
  readonly jobId: string;
  readonly kind: "production" | "transformation";
  readonly completesAtMinute: number;
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

export interface TurnTimelineItemView {
  readonly entryType: "turn";
  readonly entryId: string;
  readonly companyId: string;
  readonly companyName: string;
  readonly role: CompanyRole;
  readonly simMinute: number;
  readonly stateVersion: number;
  readonly applySequence: number;
  readonly journalSequence: number | null;
  readonly wakeSignals: readonly WakeSignalView[];
  readonly observation: ObservationFactsView;
  readonly observationDelta: ObservationDeltaView;
  readonly command: TimelineCommandView;
  readonly accepted: boolean;
  readonly reason: string | null;
  readonly outcomeOrderId: string | null;
  readonly outcomeJobId: string | null;
  readonly effects: readonly EconomicEffectView[];
  readonly stateChanges: readonly CommandStateChangeView[];
  readonly nextAvailableMinute: number | null;
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
  readonly simMinute: number;
  readonly kind: string;
  readonly journalSequence: number | null;
  readonly stateVersionBefore: number | null;
  readonly stateVersionAfter: number | null;
  readonly referenceIds: readonly string[];
  readonly effects: readonly EconomicEffectView[];
  readonly affectedCompanyIds: readonly string[];
  readonly reconstructed: false;
  readonly title: string;
  readonly summary: string;
}

export interface TimelineMomentView {
  readonly simMinute: number;
  readonly totalTurnCount: number;
  readonly systemSteps: readonly SystemTimelineItemView[];
  readonly turns: readonly TurnTimelineItemView[];
  readonly market: MarketFrameView;
}

export interface TimelineDayView {
  readonly context: TimelineContextView;
  readonly selectedDay: number;
  readonly daySummaries: readonly TimelineDaySummaryView[];
  readonly moments: readonly TimelineMomentView[];
}

interface AgentTraceBaseView {
  readonly preview: TracePreviewView;
}

export type ArtifactUnavailableReason =
  | "store_not_configured"
  | "provider_not_supported"
  | "identity_unavailable"
  | "not_found"
  | "read_error";

export type AgentTraceView = AgentTraceBaseView &
  (
    | {
        readonly artifactStatus: "available";
        readonly artifactUnavailableReason: null;
        readonly reasoningMarkdown: string;
        readonly finalOutput: string;
      }
    | {
        readonly artifactStatus: "unavailable";
        readonly artifactUnavailableReason: ArtifactUnavailableReason;
        readonly reasoningMarkdown: null;
        readonly finalOutput: null;
      }
  );

export interface TimelineDetailView {
  readonly entry: TurnTimelineItemView | SystemTimelineItemView;
  readonly context: TimelineContextView;
  readonly turnRecord: JsonValue | null;
  readonly systemStepRecord: JsonValue | null;
  readonly traces: readonly AgentTraceView[];
}

export type RunRequest =
  | {
      readonly policyMode: "baseline" | "codex" | "openai";
      readonly seed: number;
    }
  | {
      readonly policyMode: "replay";
      readonly sourceRunId: string;
    };
