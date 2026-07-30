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
}

export interface TimelineDaySummaryView {
  readonly day: number;
  readonly turnCount: number;
  readonly acceptedCount: number;
  readonly rejectedCount: number;
  readonly waitCount: number;
  readonly systemStepCount: number;
  readonly eventCount: number;
  readonly tradeQuantity: number;
  readonly consumerSales: number;
  readonly expiredQuantity: number;
}

export interface WakeSignalView {
  readonly reason: string;
  readonly sourceEntryId: string | null;
  readonly sourceEntryType: "turn" | "system_step" | null;
  readonly referenceIds: readonly string[];
}

export type EconomicEffectView =
  | {
      readonly kind: "milk_produced";
      readonly companyId: string;
      readonly requestedQuantity: number;
      readonly actualQuantity: number;
      readonly unitCost: number;
      readonly cashCost: number;
    }
  | {
      readonly kind: "trade_executed";
      readonly sellerId: string;
      readonly buyerId: string;
      readonly product: string;
      readonly quantity: number;
      readonly unitPrice: number;
      readonly totalValue: number;
    }
  | {
      readonly kind: "milk_processed";
      readonly companyId: string;
      readonly requestedInput: number;
      readonly actualInput: number;
      readonly outputQuantity: number;
      readonly cashCost: number;
    }
  | {
      readonly kind: "consumer_sale";
      readonly companyId: string;
      readonly potentialDemand: number;
      readonly demandQuantity: number;
      readonly soldQuantity: number;
      readonly retailPrice: number | null;
      readonly revenue: number;
    }
  | {
      readonly kind: "inventory_expired";
      readonly companyId: string;
      readonly product: string;
      readonly quantity: number;
      readonly valueLoss: number;
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
      readonly quantity: number;
    }
  | {
      readonly kind: "transform";
      readonly inputProduct: string;
      readonly outputProduct: string;
      readonly inputQuantity: number;
    }
  | {
      readonly kind: "place_order";
      readonly side: "buy" | "sell";
      readonly product: string;
      readonly quantity: number;
      readonly limitPrice: number;
    }
  | {
      readonly kind: "cancel_order";
      readonly orderId: string;
    }
  | {
      readonly kind: "set_retail_price";
      readonly product: string;
      readonly unitPrice: number;
    }
  | {
      readonly kind: "wait";
      readonly untilMinute: number | null;
    };

export type CommandStateChangeView =
  | {
      readonly changeType: "order_placed";
      readonly orderId: string;
      readonly side: "buy" | "sell";
      readonly product: string;
      readonly quantity: number;
      readonly limitPrice: number;
    }
  | {
      readonly changeType: "order_cancelled";
      readonly orderId: string;
    }
  | {
      readonly changeType: "retail_price_changed";
      readonly product: string;
      readonly before: number | null;
      readonly after: number;
    };

export interface ObservationFactsView {
  readonly cash: number;
  readonly inventory: Readonly<Record<string, number>>;
  readonly retailPrice: number | null;
  readonly openOrders: readonly OpenOrderView[];
  readonly visibleEventCount: number;
  readonly visibleEvents: readonly EconomicEffectView[];
}

export interface OpenOrderView {
  readonly orderId: string;
  readonly ownerId: string;
  readonly side: "buy" | "sell";
  readonly product: string;
  readonly remainingQuantity: number;
  readonly limitPrice: number;
  readonly placedAtMinute: number;
}

export interface InventoryDeltaView {
  readonly product: string;
  readonly before: number | null;
  readonly after: number;
  readonly change: number | null;
}

export interface ObservationDeltaView {
  readonly cashBefore: number | null;
  readonly cashAfter: number;
  readonly cashChange: number | null;
  readonly inventory: readonly InventoryDeltaView[];
  readonly retailPriceBefore: number | null;
  readonly retailPriceAfter: number | null;
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
  readonly effects: readonly EconomicEffectView[];
  readonly affectedCompanyIds: readonly string[];
  readonly reconstructed: boolean;
  readonly title: string;
  readonly summary: string;
}

export interface TimelineMomentView {
  readonly simMinute: number;
  readonly totalTurnCount: number;
  readonly systemSteps: readonly SystemTimelineItemView[];
  readonly turns: readonly TurnTimelineItemView[];
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
