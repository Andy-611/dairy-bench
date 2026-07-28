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

export interface PolicyInvocationView {
  readonly invocationId: string;
  readonly companyId: string;
  readonly day: number;
  readonly outcome: InvocationOutcome;
  readonly provider: string;
  readonly model: string;
  readonly decision: JsonValue | null;
  readonly errorMessage: string | null;
  readonly threadId: string | null;
  readonly turnId: string | null;
  readonly usage: TokenUsageView;
  readonly attempts: number;
  readonly latencyMs: number;
}

export interface InvocationArtifactsView {
  readonly invocationId: string;
  readonly threadId: string;
  readonly turnId: string;
  readonly model: string;
  readonly reasoningMarkdown: string;
  readonly finalOutput: string;
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

export interface EventView {
  readonly sequence: number;
  readonly day: number;
  readonly type: string;
  readonly actorCompanyId: string | null;
  readonly counterpartyCompanyId: string | null;
  readonly payload: Readonly<Record<string, JsonValue>>;
}

export interface EpisodeView {
  readonly runId: string;
  readonly scenarioId: string;
  readonly seed: number;
  readonly days: number;
  readonly score: ScoreView;
  readonly agentUsage: AgentUsageSummaryView | null;
  readonly invocations: readonly PolicyInvocationView[];
  readonly companies: readonly CompanyResultView[];
  readonly snapshots: readonly DailySnapshotView[];
  readonly events: readonly EventView[];
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

export type RunRequest =
  | {
      readonly policyMode: "baseline" | "codex" | "openai";
      readonly seed: number;
    }
  | {
      readonly policyMode: "replay";
      readonly sourceRunId: string;
    };
