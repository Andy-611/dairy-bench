import { companyLabel } from "./domainLabels";
import { isAbortError } from "./requestErrors";
import { isActiveRun, isGracefulRunTerminal } from "./runStatus";
import type {
  AgentUsageSummaryView,
  AgentTraceView,
  CommandDispositionSource,
  CommandStateChangeView,
  CompanyResultView,
  CompanyRole,
  DailySnapshotView,
  DecimalText,
  EconomicEffectView,
  EpisodeView,
  InvocationOutcome,
  JsonValue,
  MarketMatchLegView,
  PolicyMode,
  PolicyProfileView,
  ReplaySourceView,
  QuoteAlertView,
  RunJobView,
  RunRequest,
  RunStatus,
  ScoreView,
  SystemTimelineItemView,
  TimelineCommandView,
  TimelineContextView,
  TimelineDaySummaryView,
  TimelineDayView,
  TimelineDetailView,
  TimelineMomentView,
  TokenUsageView,
  TracePreviewView,
  TurnTimelineItemView,
  WakeSignalView,
} from "./types";

type JsonRecord = Readonly<Record<string, unknown>>;
type ProgressListener = (progress: RunJobView) => void;

interface InvocationUsageView {
  readonly model: string;
  readonly outcome: InvocationOutcome;
  readonly provider: string;
  readonly usage: TokenUsageView;
}

const POLL_INTERVAL_MS = 500;
const DECIMAL_TEXT_PATTERN =
  /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/;

export class ApiError extends Error {
  public constructor(
    message: string,
    public readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

export class DairyBenchApi {
  public constructor(private readonly baseUrl = "") {}

  public async policyProfiles(
    signal?: AbortSignal,
  ): Promise<readonly PolicyProfileView[]> {
    const payload = await this.getJson("/api/policy-profiles", signal);
    return array(payload, "policy profiles").map((profile, index) =>
      parsePolicyProfile(profile, `profiles[${index}]`),
    );
  }

  public async run(
    request: RunRequest,
    onProgress: ProgressListener,
    signal?: AbortSignal,
  ): Promise<RunJobView> {
    const response = await fetch(`${this.baseUrl}/api/runs`, {
      body: JSON.stringify(runRequestPayload(request)),
      headers: { "Content-Type": "application/json" },
      method: "POST",
      signal,
    });
    const payload = await readJsonBody(response, signal);

    if (!response.ok) {
      throw new ApiError(errorMessage(payload, response.status), response.status);
    }

    let progress = parseRunJob(payload);
    onProgress(progress);
    while (isActiveRun(progress.status)) {
      await delay(POLL_INTERVAL_MS, signal);
      progress = parseRunJob(
        await this.getJson(`/api/run-jobs/${progress.runId}`, signal),
      );
      onProgress(progress);
    }

    if (!isGracefulRunTerminal(progress.status)) {
      throw new ApiError(
        progress.errorMessage ?? "The run did not complete. Check the backend log.",
        500,
      );
    }

    return progress;
  }

  public async runJobs(
    limit = 100,
    signal?: AbortSignal,
  ): Promise<readonly RunJobView[]> {
    const payload = await this.getJson(`/api/run-jobs?limit=${limit}`, signal);
    return array(payload, "RunJob[]").map(parseRunJob);
  }

  public async replaySources(
    signal?: AbortSignal,
  ): Promise<readonly ReplaySourceView[]> {
    const payload = await this.getJson("/api/replay-sources", signal);
    return array(payload, "ReplaySource[]").map(parseReplaySource);
  }

  public async runJob(
    runId: string,
    signal?: AbortSignal,
  ): Promise<RunJobView> {
    const payload = await this.getJson(
      `/api/run-jobs/${encodeURIComponent(runId)}`,
      signal,
    );
    return parseRunJob(payload);
  }

  public async stopRun(
    runId: string,
    signal?: AbortSignal,
  ): Promise<RunJobView> {
    const response = await fetch(
      `${this.baseUrl}/api/run-jobs/${encodeURIComponent(runId)}/stop`,
      { method: "POST", signal },
    );
    const payload = await readJsonBody(response, signal);
    if (!response.ok) {
      throw new ApiError(errorMessage(payload, response.status), response.status);
    }
    return parseRunJob(payload);
  }

  public async episode(
    runId: string,
    signal?: AbortSignal,
  ): Promise<EpisodeView> {
    const encodedRunId = encodeURIComponent(runId);
    const [episode, invocations] = await Promise.all([
      this.getJson(`/api/runs/${encodedRunId}`, signal),
      this.getJson(`/api/runs/${encodedRunId}/invocations`, signal),
    ]);
    return parseEpisode(episode, parseInvocations(invocations));
  }

  public async timelineDay(
    runId: string,
    day: number,
    signal?: AbortSignal,
  ): Promise<TimelineDayView> {
    const payload = await this.getJson(
      `/api/runs/${encodeURIComponent(runId)}/timeline?day=${day}`,
      signal,
    );
    return parseTimelineDay(payload);
  }

  public async timelineDetail(
    runId: string,
    entryId: string,
    signal?: AbortSignal,
  ): Promise<TimelineDetailView> {
    const payload = await this.getJson(
      `/api/runs/${encodeURIComponent(runId)}/timeline/${encodeURIComponent(entryId)}`,
      signal,
    );
    return parseTimelineDetail(payload);
  }

  private async getJson(path: string, signal?: AbortSignal): Promise<unknown> {
    const response = await fetch(`${this.baseUrl}${path}`, { signal });
    const payload = await readJsonBody(response, signal);
    if (!response.ok) {
      throw new ApiError(errorMessage(payload, response.status), response.status);
    }
    return payload;
  }
}

async function readJsonBody(
  response: Response,
  signal?: AbortSignal,
): Promise<unknown> {
  try {
    const payload: unknown = await response.json();
    signal?.throwIfAborted();
    return payload;
  } catch (reason: unknown) {
    if (isAbortError(reason)) {
      throw reason;
    }
    if (signal?.aborted) {
      throw new DOMException("The request was cancelled.", "AbortError");
    }
    return null;
  }
}

function runRequestPayload(request: RunRequest): Readonly<Record<string, unknown>> {
  if (request.policyMode === "replay") {
    return {
      mode: request.policyMode,
      source_run_id: request.sourceRunId,
    };
  }
  return request.policyMode === "claude"
    ? { mode: request.policyMode, model: request.model, seed: request.seed }
    : { mode: request.policyMode, seed: request.seed };
}

function parsePolicyProfile(
  payload: unknown,
  path: string,
): PolicyProfileView {
  const profile = record(payload, path);
  return {
    mode: policyMode(profile.mode, `${path}.mode`),
    label: text(profile.label, `${path}.label`),
    available: boolean(profile.available, `${path}.available`),
    provider: optionalText(profile.provider),
    model: optionalText(profile.model),
    models: array(profile.models, `${path}.models`).map((model, index) =>
      text(model, `${path}.models[${index}]`),
    ),
    reasoningEffort: optionalText(profile.reasoning_effort),
    description: text(profile.description, `${path}.description`),
    unavailableReason: optionalText(profile.unavailable_reason),
  };
}

function parseRunJob(payload: unknown): RunJobView {
  const job = record(payload, "RunJob");
  return {
    runId: text(job.run_id, "run_id"),
    revision: number(job.revision, "revision"),
    mode: policyMode(job.mode, "mode"),
    model: nullableText(job.model, "model"),
    status: runStatus(job.status, "status"),
    seed: number(job.seed, "seed"),
    sourceRunId: nullableText(job.source_run_id, "source_run_id"),
    scenarioId: text(job.scenario_id, "scenario_id"),
    currentDay: number(job.current_day ?? 0, "current_day"),
    totalDays: number(job.total_days ?? 30, "total_days"),
    submittedAt: dateTime(job.submitted_at, "submitted_at"),
    startedAt: nullableDateTime(job.started_at, "started_at"),
    finishedAt: nullableDateTime(job.finished_at, "finished_at"),
    errorMessage: optionalText(job.error_message) ?? optionalText(job.error),
  };
}

function parseReplaySource(payload: unknown): ReplaySourceView {
  const source = record(payload, "ReplaySource");
  return {
    runId: text(source.run_id, "run_id"),
    submittedAt: dateTime(source.submitted_at, "submitted_at"),
  };
}

function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    function abort(): void {
      window.clearTimeout(timer);
      reject(new DOMException("The request was cancelled.", "AbortError"));
    }

    const timer = window.setTimeout(() => {
      signal?.removeEventListener("abort", abort);
      resolve();
    }, milliseconds);

    if (signal?.aborted) {
      abort();
    } else {
      signal?.addEventListener("abort", abort, { once: true });
    }
  });
}

function errorMessage(payload: unknown, status: number): string {
  if (isRecord(payload)) {
    const detail = payload.detail;
    if (typeof detail === "string") {
      return detail;
    }
    if (Array.isArray(detail)) {
      return `The request failed validation (${detail.length} issues).`;
    }
  }
  return `The backend request failed (HTTP ${status}).`;
}

function parseEpisode(
  payload: unknown,
  invocations: readonly InvocationUsageView[],
): EpisodeView {
  const episode = record(payload, "EpisodeResult");
  const scenario = record(episode.scenario, "scenario");
  const score = record(episode.score, "score");
  const companySpecs = array(scenario.companies, "scenario.companies").map(
    (item, index) => record(item, `scenario.companies[${index}]`),
  );
  const policies = array(episode.policies, "policies").map((item, index) =>
    record(item, `policies[${index}]`),
  );
  const companyScores = array(score.companies, "score.companies").map(
    (item, index) => record(item, `score.companies[${index}]`),
  );

  return {
    runId: text(episode.run_id, "run_id"),
    scenarioId: text(scenario.scenario_id, "scenario.scenario_id"),
    seed: number(episode.seed, "seed"),
    days: number(scenario.days, "scenario.days"),
    agentUsage: summarizeAgentUsage(invocations),
    score: parseScore(score),
    companies: companyScores.map((companyScore, index) =>
      parseCompany(
        companyScore,
        companySpecs,
        policies,
        `score.companies[${index}]`,
      ),
    ),
    snapshots: parseSnapshots(episode.snapshots),
  };
}

function parseScore(score: JsonRecord): ScoreView {
  return {
    finalScore: number(score.final_score, "score.final_score"),
    efficiencyRaw: number(score.efficiency_raw, "score.efficiency_raw"),
    efficiencyReference: number(
      score.efficiency_reference,
      "score.efficiency_reference",
    ),
    efficiencyScore: number(score.efficiency_score, "score.efficiency_score"),
    farmGini: number(score.farm_gini, "score.farm_gini"),
    processorGini: number(score.processor_gini, "score.processor_gini"),
    retailerGini: number(score.retailer_gini, "score.retailer_gini"),
    fairnessScore: number(score.fairness_score, "score.fairness_score"),
    bankruptCompanyCount: number(
      score.bankrupt_company_count,
      "score.bankrupt_company_count",
    ),
    bankruptcyRate: number(score.bankruptcy_rate, "score.bankruptcy_rate"),
  };
}

function parseInvocations(payload: unknown): readonly InvocationUsageView[] {
  return array(payload, "PolicyInvocation[]").map((item, index) => {
    const path = `invocations[${index}]`;
    const invocation = record(item, path);
    return {
      outcome: invocationOutcome(invocation.outcome, `${path}.outcome`),
      provider: text(invocation.provider, `${path}.provider`),
      model: text(invocation.model, `${path}.model`),
      usage: parseTokenUsage(invocation.usage, `${path}.usage`),
    };
  });
}

function summarizeAgentUsage(
  invocations: readonly InvocationUsageView[],
): AgentUsageSummaryView | null {
  if (invocations.length === 0) {
    return null;
  }

  const providers = new Set<string>();
  const models = new Set<string>();
  let successfulInvocations = 0;
  let usage: TokenUsageView = {
    inputTokens: 0,
    cachedTokens: 0,
    outputTokens: 0,
    reasoningTokens: 0,
    totalTokens: 0,
  };

  invocations.forEach((invocation) => {
    providers.add(invocation.provider);
    models.add(invocation.model);
    successfulInvocations += invocation.outcome === "success" ? 1 : 0;
    usage = addTokenUsage(usage, invocation.usage);
  });

  return {
    invocationCount: invocations.length,
    successfulInvocations,
    providers: [...providers],
    models: [...models],
    usage,
  };
}

function parseTokenUsage(payload: unknown, path: string): TokenUsageView {
  const usage = record(payload, path);
  return {
    inputTokens: number(usage.input_tokens, `${path}.input_tokens`),
    cachedTokens: number(usage.cached_tokens, `${path}.cached_tokens`),
    outputTokens: number(usage.output_tokens, `${path}.output_tokens`),
    reasoningTokens: number(
      usage.reasoning_tokens,
      `${path}.reasoning_tokens`,
    ),
    totalTokens: number(usage.total_tokens, `${path}.total_tokens`),
  };
}

function addTokenUsage(
  left: TokenUsageView,
  right: TokenUsageView,
): TokenUsageView {
  return {
    inputTokens: left.inputTokens + right.inputTokens,
    cachedTokens: left.cachedTokens + right.cachedTokens,
    outputTokens: left.outputTokens + right.outputTokens,
    reasoningTokens: left.reasoningTokens + right.reasoningTokens,
    totalTokens: left.totalTokens + right.totalTokens,
  };
}

function parseCompany(
  companyScore: JsonRecord,
  companySpecs: readonly JsonRecord[],
  policies: readonly JsonRecord[],
  path: string,
): CompanyResultView {
  const companyId = text(companyScore.company_id, `${path}.company_id`);
  const companySpec = companySpecs.find(
    (candidate) => candidate.company_id === companyId,
  );
  const policy = policies.find((candidate) => candidate.company_id === companyId);

  if (!companySpec) {
    throw new Error(`The backend response has no specification for ${companyId}.`);
  }

  return {
    companyId,
    companyName: companyLabel(
      companyId,
      text(companySpec.name, `company ${companyId}.name`),
    ),
    role: role(companyScore.tier, `${path}.tier`),
    policyName: policy ? text(policy.name, `policy ${companyId}.name`) : "Unknown policy",
    initialCash: number(companyScore.initial_value, `${path}.initial_value`),
    finalCash: number(companyScore.final_cash, `${path}.final_cash`),
    inventoryValue: number(
      companyScore.final_inventory_value,
      `${path}.final_inventory_value`,
    ),
    surplus: number(companyScore.surplus, `${path}.surplus`),
    growth: number(companyScore.growth, `${path}.growth`),
  };
}

function parseSnapshots(payload: unknown): readonly DailySnapshotView[] {
  return array(payload, "snapshots").flatMap((item, dayIndex) => {
    const day = record(item, `snapshots[${dayIndex}]`);
    return array(day.companies, `snapshots[${dayIndex}].companies`).map(
      (company, companyIndex) => {
        const snapshot = record(
          company,
          `snapshots[${dayIndex}].companies[${companyIndex}]`,
        );
        return {
          day: number(snapshot.day, "snapshot.day"),
          companyId: text(snapshot.company_id, "snapshot.company_id"),
          cash: number(snapshot.cash, "snapshot.cash"),
          inventoryValue: number(
            snapshot.inventory_value,
            "snapshot.inventory_value",
          ),
          cumulativeSurplus: number(snapshot.surplus, "snapshot.surplus"),
          consumerSalesQuantity: number(
            snapshot.daily_consumer_sales,
            "snapshot.daily_consumer_sales",
          ),
          expiredQuantity: number(
            snapshot.daily_expired_quantity,
            "snapshot.daily_expired_quantity",
          ),
        };
      },
    );
  });
}

function parseTimelineDay(payload: unknown): TimelineDayView {
  const timeline = record(payload, "TimelineDay");
  return {
    context: parseTimelineContext(timeline.context, "context"),
    selectedDay: number(timeline.selected_day, "selected_day"),
    daySummaries: array(timeline.day_summaries, "day_summaries").map(
      (item, index) =>
        parseTimelineDaySummary(item, `day_summaries[${index}]`),
    ),
    moments: array(timeline.moments, "moments").map((item, index) =>
      parseTimelineMoment(item, `moments[${index}]`),
    ),
  };
}

function parseTimelineDetail(payload: unknown): TimelineDetailView {
  const detail = record(payload, "TimelineDetail");
  const item = parseTimelineItem(detail.item, "item");
  const turnRecord =
    detail.turn === null
      ? null
      : jsonValue(detail.turn, "TimelineDetail.turn");
  const systemStepRecord =
    detail.system_step === null
      ? null
      : jsonValue(detail.system_step, "TimelineDetail.system_step");
  if (
    (item.entryType === "turn") !== (turnRecord !== null) ||
    (item.entryType === "system") !== (systemStepRecord !== null)
  ) {
    throw new Error(
      "Timeline detail must contain the complete journal record for its entry type.",
    );
  }
  return {
    context: parseTimelineContext(detail.context, "context"),
    entry: item,
    turnRecord,
    systemStepRecord,
    traces: array(detail.traces, "traces").map((value, index) =>
      parseAgentTrace(value, `traces[${index}]`),
    ),
  };
}

function parseTimelineContext(
  payload: unknown,
  path: string,
): TimelineContextView {
  const context = record(payload, path);
  return {
    currentRunId: text(context.run_id, `${path}.run_id`),
    scenarioId: text(context.scenario_id, `${path}.scenario_id`),
    scenarioVersion: number(
      context.scenario_version,
      `${path}.scenario_version`,
    ),
    totalDays: number(context.total_days, `${path}.total_days`),
    mode: text(context.mode, `${path}.mode`),
    sourceRunId: nullableText(context.source_run_id, `${path}.source_run_id`),
    traceRunId: text(context.trace_run_id, `${path}.trace_run_id`),
    isReplay: boolean(context.replay, `${path}.replay`),
    currentModelCallCount: number(
      context.model_call_count,
      `${path}.model_call_count`,
    ),
    sourceModelCallCount: number(
      context.source_model_call_count,
      `${path}.source_model_call_count`,
    ),
    currentUsage: parseTokenUsage(
      context.current_usage,
      `${path}.current_usage`,
    ),
    sourceUsage: parseTokenUsage(
      context.source_usage,
      `${path}.source_usage`,
    ),
    checkpointMinute:
      context.checkpoint_at === null
        ? null
        : simMinute(context.checkpoint_at, `${path}.checkpoint_at`),
    checkpointStateVersion: nullableNumber(
      context.checkpoint_state_version,
      `${path}.checkpoint_state_version`,
    ),
  };
}

function parseTimelineDaySummary(
  payload: unknown,
  path: string,
): TimelineDaySummaryView {
  const summary = record(payload, path);
  return {
    day: number(summary.day, `${path}.day`),
    turnCount: number(summary.turn_count, `${path}.turn_count`),
    acceptedCount: number(summary.accepted_count, `${path}.accepted_count`),
    rejectedCount: number(summary.rejected_count, `${path}.rejected_count`),
    waitCount: number(summary.wait_count, `${path}.wait_count`),
    systemStepCount: number(
      summary.system_step_count,
      `${path}.system_step_count`,
    ),
    eventCount: number(summary.event_count, `${path}.event_count`),
    tradeQuantity: decimalText(
      summary.trade_quantity,
      `${path}.trade_quantity`,
    ),
    consumerSales: decimalText(
      summary.consumer_sales,
      `${path}.consumer_sales`,
    ),
    expiredQuantity: decimalText(
      summary.expired_quantity,
      `${path}.expired_quantity`,
    ),
  };
}

function parseTimelineMoment(
  payload: unknown,
  path: string,
): TimelineMomentView {
  const moment = record(payload, path);
  const rawTurns = array(moment.turns, `${path}.turns`);
  return {
    simMinute: simMinute(moment.sim_time, `${path}.sim_time`),
    totalTurnCount: rawTurns.length,
    systemSteps: array(moment.system_steps, `${path}.system_steps`).map(
      (value, index) =>
        parseSystemTimelineItem(value, `${path}.system_steps[${index}]`),
    ),
    turns: rawTurns.map((value, index) =>
      parseTurnTimelineItem(value, `${path}.turns[${index}]`),
    ),
    market: parseMarketFrame(moment.market, `${path}.market`),
  };
}

function parseMarketFrame(
  payload: unknown,
  path: string,
): TimelineMomentView["market"] {
  const frame = record(payload, path);
  return {
    stateVersion: number(frame.state_version, `${path}.state_version`),
    orderFlow: array(frame.order_flow, `${path}.order_flow`).map((value, index) =>
      parseMarketOrderFlow(value, `${path}.order_flow[${index}]`),
    ),
    trades: array(frame.trades, `${path}.trades`).map((value, index) =>
      parseTimelineTrade(value, `${path}.trades[${index}]`),
    ),
    closingOrderBooks: array(
      frame.closing_order_books,
      `${path}.closing_order_books`,
    ).map((value, index) =>
      parseObserverOrderBook(value, `${path}.closing_order_books[${index}]`),
    ),
  };
}

function parseMarketOrderFlow(
  payload: unknown,
  path: string,
): TimelineMomentView["market"]["orderFlow"][number] {
  const flow = record(payload, path);
  const action = text(flow.action, `${path}.action`);
  const applySequence = number(flow.apply_sequence, `${path}.apply_sequence`);
  if (action === "keep") {
    return {
      action,
      applySequence,
      preservedOrder: parseOpenOrder(
        flow.preserved_order,
        `${path}.preserved_order`,
      ),
    };
  }
  if (action === "cancel") {
    return {
      action,
      applySequence,
      cancelledOrder: parseOpenOrder(flow.cancelled_order, `${path}.cancelled_order`),
    };
  }
  if (action !== "place" && action !== "replace") {
    throw new Error(`Backend field ${path}.action is not a market order action.`);
  }
  const applied = {
    applySequence,
    incomingOrder: parseOpenOrder(flow.incoming_order, `${path}.incoming_order`),
    matches: array(flow.matches, `${path}.matches`).map((value, index) =>
      parseMarketMatch(value, `${path}.matches[${index}]`),
    ),
    matchedQuantity: decimalText(flow.matched_quantity, `${path}.matched_quantity`),
    remainingQuantity: decimalText(
      flow.remaining_quantity,
      `${path}.remaining_quantity`,
    ),
  };
  return action === "place"
    ? { action, ...applied }
    : {
        action,
        ...applied,
        replacedOrder: parseOpenOrder(flow.replaced_order, `${path}.replaced_order`),
      };
}

function parseMarketMatch(
  payload: unknown,
  path: string,
): MarketMatchLegView {
  const match = record(payload, path);
  return {
    tradeId: text(match.trade_id, `${path}.trade_id`),
    makerOrder: parseOpenOrder(match.maker_order, `${path}.maker_order`),
    quantity: decimalText(match.quantity, `${path}.quantity`),
    unitPrice: decimalText(match.unit_price, `${path}.unit_price`),
  };
}

function parseTimelineTrade(
  payload: unknown,
  path: string,
): TimelineMomentView["market"]["trades"][number] {
  const trade = record(payload, path);
  return {
    applySequence: number(trade.apply_sequence, `${path}.apply_sequence`),
    tradeId: text(trade.trade_id, `${path}.trade_id`),
    makerOrderId: text(trade.maker_order_id, `${path}.maker_order_id`),
    takerOrderId: text(trade.taker_order_id, `${path}.taker_order_id`),
    product: text(trade.product, `${path}.product`),
    sellerId: text(trade.seller_id, `${path}.seller_id`),
    buyerId: text(trade.buyer_id, `${path}.buyer_id`),
    quantity: decimalText(trade.quantity, `${path}.quantity`),
    unitPrice: decimalText(trade.unit_price, `${path}.unit_price`),
    arrivesAtMinute: simMinute(trade.arrives_at, `${path}.arrives_at`),
  };
}

function parseObserverOrderBook(
  payload: unknown,
  path: string,
): TimelineMomentView["market"]["closingOrderBooks"][number] {
  const book = record(payload, path);
  return {
    product: text(book.product, `${path}.product`),
    bids: array(book.bids, `${path}.bids`).map((value, index) =>
      parseMarketPriceLevel(value, `${path}.bids[${index}]`),
    ),
    asks: array(book.asks, `${path}.asks`).map((value, index) =>
      parseMarketPriceLevel(value, `${path}.asks[${index}]`),
    ),
    lastTradePrice: nullableDecimalText(
      book.last_trade_price,
      `${path}.last_trade_price`,
    ),
    bestBid: nullableDecimalText(book.best_bid, `${path}.best_bid`),
    bestAsk: nullableDecimalText(book.best_ask, `${path}.best_ask`),
    spread: nullableDecimalText(book.spread, `${path}.spread`),
  };
}

function parseMarketPriceLevel(
  payload: unknown,
  path: string,
): TimelineMomentView["market"]["closingOrderBooks"][number]["bids"][number] {
  const level = record(payload, path);
  return {
    unitPrice: decimalText(level.unit_price, `${path}.unit_price`),
    size: decimalText(level.size, `${path}.size`),
    orders: array(level.orders, `${path}.orders`).map((value, index) =>
      parseOpenOrder(value, `${path}.orders[${index}]`),
    ),
  };
}

function parseTimelineItem(
  payload: unknown,
  path: string,
): TurnTimelineItemView | SystemTimelineItemView {
  const item = record(payload, path);
  const entryType = text(item.entry_type, `${path}.entry_type`);
  if (entryType === "turn") {
    return parseTurnTimelineItem(item, path);
  }
  if (entryType === "system_step") {
    return parseSystemTimelineItem(item, path);
  }
  throw new Error(`Backend field ${path}.entry_type is not a timeline item.`);
}

function parseTurnTimelineItem(
  payload: unknown,
  path: string,
): TurnTimelineItemView {
  const item = record(payload, path);
  const outcome = record(item.outcome, `${path}.outcome`);
  const companyId = text(item.company_id, `${path}.company_id`);
  const companyName = text(item.company_name, `${path}.company_name`);
  const replayOrigin =
    item.replay_origin === null
      ? null
      : record(item.replay_origin, `${path}.replay_origin`);
  return {
    entryType: "turn",
    entryId: text(item.entry_id, `${path}.entry_id`),
    companyId,
    companyName: companyLabel(companyId, companyName),
    role: role(item.tier, `${path}.tier`),
    simMinute: simMinute(item.sim_time, `${path}.sim_time`),
    stateVersion: number(item.state_version, `${path}.state_version`),
    applySequence: number(item.apply_sequence, `${path}.apply_sequence`),
    journalSequence: nullableNumber(
      item.journal_sequence,
      `${path}.journal_sequence`,
    ),
    wakeSignals: array(item.wake_signals, `${path}.wake_signals`).map(
      (value, index) =>
        parseWakeSignal(value, `${path}.wake_signals[${index}]`),
    ),
    observation: parseObservation(item.observation, `${path}.observation`),
    observationDelta: parseObservationDelta(
      item.observation_delta,
      `${path}.observation_delta`,
    ),
    command: parseTimelineCommand(item.command, `${path}.command`),
    accepted: boolean(outcome.accepted, `${path}.outcome.accepted`),
    dispositionSource: commandDispositionSource(
      item.disposition_source,
      `${path}.disposition_source`,
    ),
    reason: nullableText(outcome.reason, `${path}.outcome.reason`),
    resultingStateVersion: number(
      outcome.resulting_state_version,
      `${path}.outcome.resulting_state_version`,
    ),
    quoteLadderResult:
      outcome.quote_ladder_result === null
        ? null
        : parseQuoteLadderResult(
            outcome.quote_ladder_result,
            `${path}.outcome.quote_ladder_result`,
          ),
    outcomeJobId: nullableText(outcome.job_id, `${path}.outcome.job_id`),
    effects: parseTimelineEffects(item.effects, `${path}.effects`),
    stateChanges: array(item.state_changes, `${path}.state_changes`).map(
      (value, index) =>
        parseCommandStateChange(value, `${path}.state_changes[${index}]`),
    ),
    nextAvailableMinute:
      item.next_available_at === null
        ? null
        : simMinute(item.next_available_at, `${path}.next_available_at`),
    sourceRunId:
      replayOrigin === null
        ? null
        : text(
            replayOrigin.source_run_id,
            `${path}.replay_origin.source_run_id`,
          ),
    sourceTurnId:
      replayOrigin === null
        ? null
        : text(
            replayOrigin.source_turn_id,
            `${path}.replay_origin.source_turn_id`,
          ),
    traces: array(item.traces, `${path}.traces`).map((value, index) =>
      parseTracePreview(value, `${path}.traces[${index}]`),
    ),
    protocolError: nullableText(
      item.protocol_error,
      `${path}.protocol_error`,
    ),
    title: text(item.title, `${path}.title`),
    summary: text(item.summary, `${path}.summary`),
  };
}

function parseSystemTimelineItem(
  payload: unknown,
  path: string,
): SystemTimelineItemView {
  const item = record(payload, path);
  return {
    entryType: "system",
    entryId: text(item.entry_id, `${path}.entry_id`),
    simMinute: simMinute(item.sim_time, `${path}.sim_time`),
    kind: text(item.kind, `${path}.kind`),
    journalSequence: nullableNumber(
      item.journal_sequence,
      `${path}.journal_sequence`,
    ),
    stateVersionBefore: nullableNumber(
      item.state_version_before,
      `${path}.state_version_before`,
    ),
    stateVersionAfter: nullableNumber(
      item.state_version_after,
      `${path}.state_version_after`,
    ),
    referenceIds: array(item.reference_ids, `${path}.reference_ids`).map(
      (value, index) => text(value, `${path}.reference_ids[${index}]`),
    ),
    effects: parseTimelineEffects(item.effects, `${path}.effects`),
    affectedCompanyIds: array(
      item.affected_company_ids,
      `${path}.affected_company_ids`,
    ).map((value, index) =>
      text(value, `${path}.affected_company_ids[${index}]`),
    ),
    title: text(item.title, `${path}.title`),
    summary: text(item.summary, `${path}.summary`),
  };
}

function parseWakeSignal(payload: unknown, path: string): WakeSignalView {
  const signal = record(payload, path);
  const source =
    signal.source === null
      ? null
      : record(signal.source, `${path}.source`);
  const sourceType =
    source === null
      ? null
      : text(source.entry_type, `${path}.source.entry_type`);
  if (
    sourceType !== null &&
    sourceType !== "turn" &&
    sourceType !== "system_step"
  ) {
    throw new Error(`Backend field ${path}.source.entry_type is invalid.`);
  }
  return {
    reason: text(signal.reason, `${path}.reason`),
    sourceEntryId:
      source === null
        ? null
        : text(source.entry_id, `${path}.source.entry_id`),
    sourceEntryType: sourceType,
    referenceIds: array(signal.reference_ids, `${path}.reference_ids`).map(
      (value, index) => text(value, `${path}.reference_ids[${index}]`),
    ),
  };
}

function parseObservation(
  payload: unknown,
  path: string,
): TurnTimelineItemView["observation"] {
  const observation = record(payload, path);
  return {
    cash: decimalText(observation.cash, `${path}.cash`),
    reservedCash: decimalText(
      observation.reserved_cash,
      `${path}.reserved_cash`,
    ),
    markedSurplus: decimalText(
      observation.marked_surplus,
      `${path}.marked_surplus`,
    ),
    inventory: parseInventoryPositions(observation.inventory, `${path}.inventory`),
    inventoryExpiry: parseInventoryExpiry(
      observation.inventory_expiry,
      `${path}.inventory_expiry`,
    ),
    retailPrice: nullableDecimalText(
      observation.retail_price,
      `${path}.retail_price`,
    ),
    openOrders: array(observation.open_orders, `${path}.open_orders`).map(
      (value, index) =>
        parseOpenOrder(value, `${path}.open_orders[${index}]`),
    ),
    orderBooks: array(observation.order_books, `${path}.order_books`).map(
      (value, index) => parseOrderBook(value, `${path}.order_books[${index}]`),
    ),
    pendingDeliveries: array(
      observation.pending_deliveries,
      `${path}.pending_deliveries`,
    ).map((value, index) =>
      parseIncomingDelivery(value, `${path}.pending_deliveries[${index}]`),
    ),
    activeOperation:
      observation.active_operation === null
        ? null
        : parseOperationJob(
            observation.active_operation,
            `${path}.active_operation`,
          ),
    remainingOperationCapacity: nullableDecimalText(
      observation.remaining_operation_capacity,
      `${path}.remaining_operation_capacity`,
    ),
    visibleEventCount: number(
      observation.visible_event_count,
      `${path}.visible_event_count`,
    ),
    visibleEvents: parseTimelineEffects(
      observation.visible_events,
      `${path}.visible_events`,
    ),
  };
}

function parseOpenOrder(
  payload: unknown,
  path: string,
): TurnTimelineItemView["observation"]["openOrders"][number] {
  const order = record(payload, path);
  const side = text(order.side, `${path}.side`);
  if (side !== "buy" && side !== "sell") {
    throw new Error(`Backend field ${path}.side is not a market side.`);
  }
  return {
    orderId: text(order.order_id, `${path}.order_id`),
    ownerId: text(order.owner_id, `${path}.owner_id`),
    side,
    product: text(order.product, `${path}.product`),
    remainingQuantity: decimalText(
      order.remaining_quantity,
      `${path}.remaining_quantity`,
    ),
    limitPrice: decimalText(order.limit_price, `${path}.limit_price`),
    placedAtMinute: simMinute(order.placed_at, `${path}.placed_at`),
    prioritySequence: number(
      order.priority_sequence,
      `${path}.priority_sequence`,
    ),
    queueAheadQuantity: decimalText(
      order.queue_ahead_quantity,
      `${path}.queue_ahead_quantity`,
    ),
  };
}

function parseInventoryPositions(
  payload: unknown,
  path: string,
): Readonly<Record<string, DecimalText>> {
  return Object.fromEntries(
    array(payload, path).map((value, index) => {
      const itemPath = `${path}[${index}]`;
      const position = record(value, itemPath);
      return [
        text(position.product, `${itemPath}.product`),
        decimalText(position.quantity, `${itemPath}.quantity`),
      ];
    }),
  );
}

function parseOrderBook(
  payload: unknown,
  path: string,
): TurnTimelineItemView["observation"]["orderBooks"][number] {
  const market = record(payload, path);
  return {
    product: text(market.product, `${path}.product`),
    bids: parsePriceLevels(market.bids, `${path}.bids`),
    asks: parsePriceLevels(market.asks, `${path}.asks`),
    lastTradePrice: nullableDecimalText(
      market.last_trade_price,
      `${path}.last_trade_price`,
    ),
    dailyVolume: decimalText(market.daily_volume, `${path}.daily_volume`),
  };
}

function parsePriceLevels(
  payload: unknown,
  path: string,
): TurnTimelineItemView["observation"]["orderBooks"][number]["bids"] {
  return array(payload, path).map((value, index) => {
    const itemPath = `${path}[${index}]`;
    const level = record(value, itemPath);
    return {
      unitPrice: decimalText(level.unit_price, `${itemPath}.unit_price`),
      quantity: decimalText(level.quantity, `${itemPath}.quantity`),
      orderCount: number(level.order_count, `${itemPath}.order_count`),
    };
  });
}

function parseInventoryExpiry(
  payload: unknown,
  path: string,
): TurnTimelineItemView["observation"]["inventoryExpiry"] {
  return array(payload, path).map((value, index) => {
    const itemPath = `${path}[${index}]`;
    const bucket = record(value, itemPath);
    return {
      product: text(bucket.product, `${itemPath}.product`),
      expiresEndOfDay: number(
        bucket.expires_end_of_day,
        `${itemPath}.expires_end_of_day`,
      ),
      availableQuantity: decimalText(
        bucket.available_quantity,
        `${itemPath}.available_quantity`,
      ),
      reservedQuantity: decimalText(
        bucket.reserved_quantity,
        `${itemPath}.reserved_quantity`,
      ),
    };
  });
}

function parseIncomingDelivery(
  payload: unknown,
  path: string,
): TurnTimelineItemView["observation"]["pendingDeliveries"][number] {
  const delivery = record(payload, path);
  return {
    tradeId: text(delivery.trade_id, `${path}.trade_id`),
    product: text(delivery.product, `${path}.product`),
    quantity: decimalText(delivery.quantity, `${path}.quantity`),
    arrivesAtMinute: simMinute(delivery.arrives_at, `${path}.arrives_at`),
    expiryBuckets: array(delivery.expiry_buckets, `${path}.expiry_buckets`).map(
      (value, index) => {
        const bucketPath = `${path}.expiry_buckets[${index}]`;
        const bucket = record(value, bucketPath);
        return {
          quantity: decimalText(bucket.quantity, `${bucketPath}.quantity`),
          expiresEndOfDay: number(
            bucket.expires_end_of_day,
            `${bucketPath}.expires_end_of_day`,
          ),
        };
      },
    ),
  };
}

function parseOperationJob(
  payload: unknown,
  path: string,
): NonNullable<TurnTimelineItemView["observation"]["activeOperation"]> {
  const operation = record(payload, path);
  const kind = text(operation.kind, `${path}.kind`);
  if (kind !== "production" && kind !== "transformation") {
    throw new Error(`Backend field ${path}.kind is not an operation kind.`);
  }
  return {
    jobId: text(operation.job_id, `${path}.job_id`),
    kind,
    completesAtMinute: simMinute(operation.completes_at, `${path}.completes_at`),
    outputProduct: text(operation.output_product, `${path}.output_product`),
    outputQuantity: decimalText(
      operation.output_quantity,
      `${path}.output_quantity`,
    ),
  };
}

function parseObservationDelta(
  payload: unknown,
  path: string,
): TurnTimelineItemView["observationDelta"] {
  const delta = record(payload, path);
  return {
    cashBefore: nullableDecimalText(delta.cash_before, `${path}.cash_before`),
    cashAfter: decimalText(delta.cash_after, `${path}.cash_after`),
    cashChange: nullableDecimalText(delta.cash_change, `${path}.cash_change`),
    inventory: array(delta.inventory, `${path}.inventory`).map(
      (value, index) => {
        const itemPath = `${path}.inventory[${index}]`;
        const item = record(value, itemPath);
        return {
          product: text(item.product, `${itemPath}.product`),
          before: nullableDecimalText(item.before, `${itemPath}.before`),
          after: decimalText(item.after, `${itemPath}.after`),
          change: nullableDecimalText(item.change, `${itemPath}.change`),
        };
      },
    ),
    retailPriceBefore: nullableDecimalText(
      delta.retail_price_before,
      `${path}.retail_price_before`,
    ),
    retailPriceAfter: nullableDecimalText(
      delta.retail_price_after,
      `${path}.retail_price_after`,
    ),
    openOrderCountBefore: nullableNumber(
      delta.open_order_count_before,
      `${path}.open_order_count_before`,
    ),
    openOrderCountAfter: number(
      delta.open_order_count_after,
      `${path}.open_order_count_after`,
    ),
  };
}

function parseTimelineEffects(
  payload: unknown,
  path: string,
): readonly EconomicEffectView[] {
  return array(payload, path).map((value, index) => {
    const effectPath = `${path}[${index}]`;
    const effect = record(value, effectPath);
    const kind = text(effect.event_type, `${effectPath}.event_type`);
    if (kind === "milk_produced") {
      return {
        kind,
        companyId: text(effect.company_id, `${effectPath}.company_id`),
        requestedQuantity: decimalText(
          effect.requested_quantity,
          `${effectPath}.requested_quantity`,
        ),
        actualQuantity: decimalText(
          effect.actual_quantity,
          `${effectPath}.actual_quantity`,
        ),
        unitCost: decimalText(effect.unit_cost, `${effectPath}.unit_cost`),
        cashCost: decimalText(effect.cash_cost, `${effectPath}.cash_cost`),
      };
    }
    if (kind === "trade_executed") {
      return {
        kind,
        tradeId: text(effect.trade_id, `${effectPath}.trade_id`),
        sellerId: text(effect.seller_id, `${effectPath}.seller_id`),
        buyerId: text(effect.buyer_id, `${effectPath}.buyer_id`),
        product: text(effect.product, `${effectPath}.product`),
        quantity: decimalText(effect.quantity, `${effectPath}.quantity`),
        unitPrice: decimalText(effect.unit_price, `${effectPath}.unit_price`),
        totalValue: decimalText(
          effect.total_value,
          `${effectPath}.total_value`,
        ),
      };
    }
    if (kind === "delivery_completed") {
      return {
        kind,
        tradeId: text(effect.trade_id, `${effectPath}.trade_id`),
        companyId: text(effect.company_id, `${effectPath}.company_id`),
        product: text(effect.product, `${effectPath}.product`),
        quantity: decimalText(effect.quantity, `${effectPath}.quantity`),
      };
    }
    if (kind === "milk_processed") {
      return {
        kind,
        companyId: text(effect.company_id, `${effectPath}.company_id`),
        requestedInput: decimalText(
          effect.requested_input,
          `${effectPath}.requested_input`,
        ),
        actualInput: decimalText(
          effect.actual_input,
          `${effectPath}.actual_input`,
        ),
        outputQuantity: decimalText(
          effect.output_quantity,
          `${effectPath}.output_quantity`,
        ),
        cashCost: decimalText(effect.cash_cost, `${effectPath}.cash_cost`),
      };
    }
    if (kind === "consumer_sale") {
      return {
        kind,
        companyId: text(effect.company_id, `${effectPath}.company_id`),
        potentialDemand: decimalText(
          effect.potential_demand_quantity,
          `${effectPath}.potential_demand_quantity`,
        ),
        demandQuantity: decimalText(
          effect.demand_quantity,
          `${effectPath}.demand_quantity`,
        ),
        soldQuantity: decimalText(
          effect.sold_quantity,
          `${effectPath}.sold_quantity`,
        ),
        retailPrice: nullableDecimalText(
          effect.retail_price,
          `${effectPath}.retail_price`,
        ),
        revenue: decimalText(effect.revenue, `${effectPath}.revenue`),
      };
    }
    if (kind === "inventory_expired") {
      return {
        kind,
        companyId: text(effect.company_id, `${effectPath}.company_id`),
        product: text(effect.product, `${effectPath}.product`),
        quantity: decimalText(effect.quantity, `${effectPath}.quantity`),
        valueLoss: decimalText(
          effect.reference_value_loss,
          `${effectPath}.reference_value_loss`,
        ),
      };
    }
    if (kind === "decision_rejected" || kind === "policy_failed") {
      return {
        kind,
        companyId: text(effect.company_id, `${effectPath}.company_id`),
        reason: text(effect.reason, `${effectPath}.reason`),
      };
    }
    throw new Error(`Backend field ${effectPath}.event_type is unknown.`);
  });
}

function parseTimelineCommand(
  payload: unknown,
  path: string,
): TimelineCommandView {
  const command = record(payload, path);
  const kind = text(command.kind, `${path}.kind`);
  if (kind === "produce") {
    return {
      kind,
      product: text(command.product, `${path}.product`),
      quantity: decimalText(command.quantity, `${path}.quantity`),
    };
  }
  if (kind === "transform") {
    return {
      kind,
      inputProduct: text(command.input_product, `${path}.input_product`),
      outputProduct: text(command.output_product, `${path}.output_product`),
      inputQuantity: decimalText(
        command.input_quantity,
        `${path}.input_quantity`,
      ),
    };
  }
  if (kind === "set_quote_ladder") {
    const side = text(command.side, `${path}.side`);
    if (side !== "buy" && side !== "sell") {
      throw new Error(`Backend field ${path}.side is not a market side.`);
    }
    return {
      kind,
      side,
      product: text(command.product, `${path}.product`),
      levels: array(command.levels, `${path}.levels`).map((value, index) =>
        parseQuoteLevel(value, `${path}.levels[${index}]`),
      ),
    };
  }
  if (kind === "set_retail_price") {
    return {
      kind,
      product: text(command.product, `${path}.product`),
      unitPrice: decimalText(command.unit_price, `${path}.unit_price`),
    };
  }
  if (kind === "wait") {
    return {
      kind,
      untilMinute:
        command.until === null
          ? null
          : simMinute(command.until, `${path}.until`),
      alerts: array(command.alerts, `${path}.alerts`).map((value, index) =>
        parseQuoteAlert(value, `${path}.alerts[${index}]`),
      ),
    };
  }
  throw new Error(`Backend field ${path}.kind is not an atomic command.`);
}

function parseQuoteLevel(
  payload: unknown,
  path: string,
): Extract<
  TimelineCommandView,
  { readonly kind: "set_quote_ladder" }
>["levels"][number] {
  const level = record(payload, path);
  return {
    quantity: decimalText(level.quantity, `${path}.quantity`),
    limitPrice: decimalText(level.limit_price, `${path}.limit_price`),
  };
}

function parseQuoteLadderResult(
  payload: unknown,
  path: string,
): NonNullable<TurnTimelineItemView["quoteLadderResult"]> {
  const result = record(payload, path);
  return {
    levels: array(result.levels, `${path}.levels`).map((value, index) => {
      const levelPath = `${path}.levels[${index}]`;
      const level = record(value, levelPath);
      const action = text(level.action, `${levelPath}.action`);
      if (action !== "keep" && action !== "place" && action !== "replace") {
        throw new Error(
          `Backend field ${levelPath}.action is not a quote action.`,
        );
      }
      return {
        level: parseQuoteLevel(level.level, `${levelPath}.level`),
        action,
        orderId: text(level.order_id, `${levelPath}.order_id`),
        replacedOrderId: nullableText(
          level.replaced_order_id,
          `${levelPath}.replaced_order_id`,
        ),
        prioritySequence: number(
          level.priority_sequence,
          `${levelPath}.priority_sequence`,
        ),
        remainingQuantity: decimalText(
          level.remaining_quantity,
          `${levelPath}.remaining_quantity`,
        ),
      };
    }),
    cancelledOrderIds: array(
      result.cancelled_order_ids,
      `${path}.cancelled_order_ids`,
    ).map((value, index) =>
      text(value, `${path}.cancelled_order_ids[${index}]`),
    ),
  };
}

function parseQuoteAlert(payload: unknown, path: string): QuoteAlertView {
  const alert = record(payload, path);
  const quote = text(alert.quote, `${path}.quote`);
  const operator = text(alert.operator, `${path}.operator`);
  if (quote !== "best_ask" && quote !== "best_bid") {
    throw new Error(`Backend field ${path}.quote is not a supported quote.`);
  }
  if (operator !== "at_least" && operator !== "at_most") {
    throw new Error(`Backend field ${path}.operator is not a price comparison.`);
  }
  return {
    product: text(alert.product, `${path}.product`),
    quote,
    operator,
    price: decimalText(alert.price, `${path}.price`),
  };
}

function parseTracePreview(
  payload: unknown,
  path: string,
): TracePreviewView {
  const preview = record(payload, path);
  return {
    traceRunId: text(preview.trace_run_id, `${path}.trace_run_id`),
    invocationId: text(preview.invocation_id, `${path}.invocation_id`),
    isSourceTrace: boolean(preview.source_trace, `${path}.source_trace`),
    provider: text(preview.provider, `${path}.provider`),
    model: text(preview.model, `${path}.model`),
    outcome: invocationOutcome(preview.outcome, `${path}.outcome`),
    usage: parseTokenUsage(preview.usage, `${path}.usage`),
    latencyMs: number(preview.latency_ms, `${path}.latency_ms`),
    attempts: number(preview.attempts, `${path}.attempts`),
    appliedToCommittedTurn: boolean(
      preview.applied_to_committed_turn,
      `${path}.applied_to_committed_turn`,
    ),
  };
}

function parseCommandStateChange(
  payload: unknown,
  path: string,
): CommandStateChangeView {
  const change = record(payload, path);
  const changeType = text(change.change_type, `${path}.change_type`);
  if (changeType === "order_placed") {
    const side = text(change.side, `${path}.side`);
    if (side !== "buy" && side !== "sell") {
      throw new Error(`Backend field ${path}.side is not a market side.`);
    }
    return {
      changeType,
      orderId: text(change.order_id, `${path}.order_id`),
      side,
      product: text(change.product, `${path}.product`),
      quantity: decimalText(change.quantity, `${path}.quantity`),
      limitPrice: decimalText(change.limit_price, `${path}.limit_price`),
    };
  }
  if (changeType === "order_cancelled") {
    return {
      changeType,
      orderId: text(change.order_id, `${path}.order_id`),
    };
  }
  if (changeType === "order_replaced") {
    return {
      changeType,
      replacedOrderId: text(
        change.replaced_order_id,
        `${path}.replaced_order_id`,
      ),
      orderId: text(change.order_id, `${path}.order_id`),
      quantity: decimalText(change.quantity, `${path}.quantity`),
      limitPrice: decimalText(change.limit_price, `${path}.limit_price`),
    };
  }
  if (changeType === "retail_price_changed") {
    return {
      changeType,
      product: text(change.product, `${path}.product`),
      before:
        change.before === null
          ? null
          : decimalText(change.before, `${path}.before`),
      after: decimalText(change.after, `${path}.after`),
    };
  }
  throw new Error(`Backend field ${path}.change_type is not a state change.`);
}

function parseAgentTrace(payload: unknown, path: string): AgentTraceView {
  const trace = record(payload, path);
  const preview = parseTracePreview(trace.preview, `${path}.preview`);
  const artifactStatus = text(
    trace.artifact_status,
    `${path}.artifact_status`,
  );
  const artifactUnavailableReason = parseArtifactUnavailableReason(
    trace.artifact_unavailable_reason,
    `${path}.artifact_unavailable_reason`,
  );
  if (artifactStatus !== "available" && artifactStatus !== "unavailable") {
    throw new Error(`Backend field ${path}.artifact_status is unknown.`);
  }
  const reasoningMarkdown = nullableText(
    trace.reasoning_markdown,
    `${path}.reasoning_markdown`,
  );
  const finalOutput = nullableText(
    trace.final_output,
    `${path}.final_output`,
  );
  if (artifactStatus === "available") {
    if (
      artifactUnavailableReason !== null ||
      reasoningMarkdown === null ||
      finalOutput === null
    ) {
      throw new Error(
        `Backend trace artifact fields at ${path} are inconsistent.`,
      );
    }
    return {
      preview,
      artifactStatus,
      artifactUnavailableReason: null,
      reasoningMarkdown,
      finalOutput,
    };
  }
  if (
    artifactUnavailableReason === null ||
    reasoningMarkdown !== null ||
    finalOutput !== null
  ) {
    throw new Error(
      `Backend trace artifact fields at ${path} are inconsistent.`,
    );
  }
  return {
    preview,
    artifactStatus,
    artifactUnavailableReason,
    reasoningMarkdown: null,
    finalOutput: null,
  };
}

function parseArtifactUnavailableReason(
  value: unknown,
  path: string,
): AgentTraceView["artifactUnavailableReason"] {
  const reason = nullableText(value, path);
  switch (reason) {
    case null:
    case "store_not_configured":
    case "provider_not_supported":
    case "identity_unavailable":
    case "not_found":
    case "read_error":
      return reason;
    default:
      throw new Error(`Backend field ${path} is unknown.`);
  }
}

function simMinute(payload: unknown, path: string): number {
  const time = record(payload, path);
  return number(time.absolute_minute, `${path}.absolute_minute`);
}

function jsonValue(value: unknown, path: string): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "number") {
    return value;
  }
  if (typeof value === "string") {
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item, index) => jsonValue(item, `${path}[${index}]`));
  }
  if (isRecord(value)) {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        jsonValue(item, `${path}.${key}`),
      ]),
    );
  }
  throw new Error(`Backend field ${path} is not valid JSON.`);
}

function record(value: unknown, path: string): JsonRecord {
  if (!isRecord(value)) {
    throw new Error(`Backend field ${path} must be an object.`);
  }
  return value;
}

function array(value: unknown, path: string): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`Backend field ${path} must be an array.`);
  }
  return value;
}

function text(value: unknown, path: string): string {
  const parsed = plainText(value, path);
  if (parsed.length === 0) {
    throw new Error(`Backend field ${path} must be non-empty text.`);
  }
  return parsed;
}

function optionalText(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function number(value: unknown, path: string): number {
  const parsed =
    typeof value === "number"
      ? value
      : typeof value === "string" && value.trim() !== ""
        ? Number(value)
        : Number.NaN;
  if (!Number.isFinite(parsed)) {
    throw new Error(`Backend field ${path} must be a finite number.`);
  }
  return parsed;
}

function decimalText(value: unknown, path: string): DecimalText {
  const parsed = plainText(value, path);
  if (parsed.trim() !== parsed || !DECIMAL_TEXT_PATTERN.test(parsed)) {
    throw new Error(`Backend field ${path} must be finite decimal text.`);
  }
  return parsed as DecimalText;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`Backend field ${path} must be a boolean.`);
  }
  return value;
}

function role(value: unknown, path: string): CompanyRole {
  if (value === "farm" || value === "processor" || value === "retailer") {
    return value;
  }
  throw new Error(`Backend field ${path} is not a known company tier.`);
}

function commandDispositionSource(
  value: unknown,
  path: string,
): CommandDispositionSource {
  if (
    value === "economic_engine" ||
    value === "runtime_attention" ||
    value === "runtime_protocol"
  ) {
    return value;
  }
  throw new Error(`Backend field ${path} is not a known disposition source.`);
}

function policyMode(value: unknown, path: string): PolicyMode {
  if (
    value === "baseline" ||
    value === "codex" ||
    value === "openai" ||
    value === "claude" ||
    value === "replay"
  ) {
    return value;
  }
  throw new Error(`Backend field ${path} is not a known run mode.`);
}

function runStatus(value: unknown, path: string): RunStatus {
  if (
    value === "queued" ||
    value === "running" ||
    value === "interrupted" ||
    value === "completed" ||
    value === "failed" ||
    value === "stopped"
  ) {
    return value;
  }
  throw new Error(`Backend field ${path} is not a known run status.`);
}

function invocationOutcome(value: unknown, path: string): InvocationOutcome {
  if (
    value === "success" ||
    value === "agent_error" ||
    value === "infrastructure_error"
  ) {
    return value;
  }
  throw new Error(`Backend field ${path} is not a known invocation outcome.`);
}

function nullableText(value: unknown, path: string): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "string") {
    throw new Error(`Backend field ${path} must be text or null.`);
  }
  return value.length > 0 ? value : null;
}

function nullableNumber(value: unknown, path: string): number | null {
  return value === null || value === undefined ? null : number(value, path);
}

function nullableDecimalText(
  value: unknown,
  path: string,
): DecimalText | null {
  return value === null || value === undefined
    ? null
    : decimalText(value, path);
}

function dateTime(value: unknown, path: string): string {
  const parsed = text(value, path);
  if (Number.isNaN(Date.parse(parsed))) {
    throw new Error(`Backend field ${path} is not a valid date-time.`);
  }
  return parsed;
}

function nullableDateTime(value: unknown, path: string): string | null {
  return value === null || value === undefined ? null : dateTime(value, path);
}

function plainText(value: unknown, path: string): string {
  if (typeof value !== "string") {
    throw new Error(`Backend field ${path} must be text.`);
  }
  return value;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
