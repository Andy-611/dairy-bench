import type {
  AgentUsageSummaryView,
  CompanyResultView,
  CompanyRole,
  DailySnapshotView,
  EpisodeView,
  EventView,
  InvocationArtifactsView,
  InvocationOutcome,
  JsonValue,
  PolicyInvocationView,
  PolicyMode,
  PolicyProfileView,
  RunProgressView,
  RunRequest,
  RunStatus,
  TokenUsageView,
} from "./types";

type JsonRecord = Readonly<Record<string, unknown>>;
type ProgressListener = (progress: RunProgressView) => void;

const POLL_INTERVAL_MS = 500;
const ACTIVE_STATUSES: readonly RunStatus[] = [
  "queued",
  "running",
  "interrupted",
];

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
  ): Promise<EpisodeView> {
    const response = await fetch(`${this.baseUrl}/api/runs`, {
      body: JSON.stringify(runRequestPayload(request)),
      headers: { "Content-Type": "application/json" },
      method: "POST",
      signal,
    });
    const payload: unknown = await response.json().catch(() => null);

    if (!response.ok) {
      throw new ApiError(errorMessage(payload, response.status), response.status);
    }

    let progress = parseRunProgress(payload);
    onProgress(progress);
    while (ACTIVE_STATUSES.includes(progress.status)) {
      await delay(POLL_INTERVAL_MS, signal);
      progress = parseRunProgress(
        await this.getJson(`/api/run-jobs/${progress.runId}`, signal),
      );
      onProgress(progress);
    }

    if (progress.status !== "completed") {
      throw new ApiError(
        progress.errorMessage ?? "运行未完成，请查看后端日志。",
        500,
      );
    }

    const [episode, invocations] = await Promise.all([
      this.getJson(`/api/runs/${progress.runId}`, signal),
      this.getJson(`/api/runs/${progress.runId}/invocations`, signal),
    ]);
    return parseEpisode(episode, parseInvocations(invocations));
  }

  public async invocationArtifacts(
    runId: string,
    invocationId: string,
    signal?: AbortSignal,
  ): Promise<InvocationArtifactsView> {
    const payload = await this.getJson(
      `/api/runs/${encodeURIComponent(runId)}/invocations/${encodeURIComponent(invocationId)}/artifacts`,
      signal,
    );
    return parseInvocationArtifacts(payload);
  }

  private async getJson(path: string, signal?: AbortSignal): Promise<unknown> {
    const response = await fetch(`${this.baseUrl}${path}`, { signal });
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(errorMessage(payload, response.status), response.status);
    }
    return payload;
  }
}

function runRequestPayload(request: RunRequest): Readonly<Record<string, unknown>> {
  return request.policyMode === "replay"
    ? {
        mode: request.policyMode,
        source_run_id: request.sourceRunId,
      }
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
    reasoningEffort: optionalText(profile.reasoning_effort),
    description: text(profile.description, `${path}.description`),
    unavailableReason: optionalText(profile.unavailable_reason),
  };
}

function parseRunProgress(payload: unknown): RunProgressView {
  const job = record(payload, "RunJob");
  return {
    runId: text(job.run_id, "run_id"),
    status: runStatus(job.status, "status"),
    currentDay: number(job.current_day ?? 0, "current_day"),
    totalDays: number(job.total_days ?? 30, "total_days"),
    errorMessage: optionalText(job.error_message) ?? optionalText(job.error),
  };
}

function delay(milliseconds: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    function abort(): void {
      window.clearTimeout(timer);
      reject(new DOMException("请求已取消", "AbortError"));
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
      return `请求参数未通过校验（${detail.length} 项）`;
    }
  }
  return `后端请求失败（HTTP ${status}）`;
}

function parseEpisode(
  payload: unknown,
  invocations: readonly PolicyInvocationView[],
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
    invocations,
    score: {
      eligible: boolean(score.eligible, "score.eligible"),
      efficiency: number(score.efficiency, "score.efficiency"),
      fairness: number(score.fairness, "score.fairness"),
      fulfillmentRate: number(
        score.consumer_fill_rate,
        "score.consumer_fill_rate",
      ),
      expiredQuantity: number(
        score.expired_quantity,
        "score.expired_quantity",
      ),
    },
    companies: companyScores.map((companyScore, index) =>
      parseCompany(
        companyScore,
        companySpecs,
        policies,
        `score.companies[${index}]`,
      ),
    ),
    snapshots: parseSnapshots(episode.snapshots),
    events: parseEvents(episode.events),
  };
}

function parseInvocations(payload: unknown): readonly PolicyInvocationView[] {
  return array(payload, "PolicyInvocation[]").map((item, index) => {
    const path = `invocations[${index}]`;
    const invocation = record(item, path);
    const rawDecision = invocation.decision;
    return {
      invocationId: text(invocation.invocation_id, `${path}.invocation_id`),
      companyId: text(invocation.company_id, `${path}.company_id`),
      day: number(invocation.day, `${path}.day`),
      outcome: invocationOutcome(invocation.outcome, `${path}.outcome`),
      provider: text(invocation.provider, `${path}.provider`),
      model: text(invocation.model, `${path}.model`),
      decision:
        rawDecision === null
          ? null
          : jsonValue(rawDecision, `${path}.decision`),
      errorMessage: nullableText(
        invocation.error_message,
        `${path}.error_message`,
      ),
      threadId: nullableText(invocation.request_id, `${path}.request_id`),
      turnId: nullableText(invocation.response_id, `${path}.response_id`),
      usage: parseTokenUsage(invocation.usage, `${path}.usage`),
      attempts: number(invocation.attempts, `${path}.attempts`),
      latencyMs: number(invocation.latency_ms, `${path}.latency_ms`),
    };
  });
}

function parseInvocationArtifacts(payload: unknown): InvocationArtifactsView {
  const artifacts = record(payload, "InvocationArtifacts");
  return {
    invocationId: text(artifacts.invocation_id, "invocation_id"),
    threadId: text(artifacts.thread_id, "thread_id"),
    turnId: text(artifacts.turn_id, "turn_id"),
    model: text(artifacts.model, "model"),
    reasoningMarkdown: plainText(
      artifacts.reasoning_markdown,
      "reasoning_markdown",
    ),
    finalOutput: plainText(artifacts.final_output, "final_output"),
  };
}

function summarizeAgentUsage(
  invocations: readonly PolicyInvocationView[],
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
    throw new Error(`后端响应缺少公司配置：${companyId}`);
  }

  return {
    companyId,
    companyName: text(companySpec.name, `company ${companyId}.name`),
    role: role(companyScore.tier, `${path}.tier`),
    policyName: policy ? text(policy.name, `policy ${companyId}.name`) : "未知策略",
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

function parseEvents(payload: unknown): readonly EventView[] {
  return array(payload, "events").map((item, index) => {
    const eventRecord = record(item, `events[${index}]`);
    const event = record(eventRecord.event, `events[${index}].event`);
    const type = text(event.event_type, `events[${index}].event.event_type`);
    const actor =
      optionalText(event.company_id) ?? optionalText(event.seller_id) ?? null;
    const counterparty = optionalText(event.buyer_id) ?? null;
    const details = Object.fromEntries(
      Object.entries(event)
        .filter(
          ([key]) =>
            ![
              "event_type",
              "day",
              "company_id",
              "seller_id",
              "buyer_id",
            ].includes(key),
        )
        .map(([key, value]) => [key, jsonValue(value, `${type}.${key}`)]),
    );

    return {
      sequence: number(eventRecord.sequence, `events[${index}].sequence`),
      day: number(event.day, `events[${index}].event.day`),
      type,
      actorCompanyId: actor,
      counterpartyCompanyId: counterparty,
      payload: details,
    };
  });
}

function jsonValue(value: unknown, path: string): JsonValue {
  if (value === null || typeof value === "boolean" || typeof value === "number") {
    return value;
  }
  if (typeof value === "string") {
    const numeric = Number(value);
    return value.trim() !== "" && Number.isFinite(numeric) ? numeric : value;
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
  throw new Error(`后端响应字段 ${path} 不是有效 JSON`);
}

function record(value: unknown, path: string): JsonRecord {
  if (!isRecord(value)) {
    throw new Error(`后端响应字段 ${path} 应为对象`);
  }
  return value;
}

function array(value: unknown, path: string): readonly unknown[] {
  if (!Array.isArray(value)) {
    throw new Error(`后端响应字段 ${path} 应为数组`);
  }
  return value;
}

function text(value: unknown, path: string): string {
  const parsed = plainText(value, path);
  if (parsed.length === 0) {
    throw new Error(`后端响应字段 ${path} 应为非空文本`);
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
    throw new Error(`后端响应字段 ${path} 应为有限数值`);
  }
  return parsed;
}

function boolean(value: unknown, path: string): boolean {
  if (typeof value !== "boolean") {
    throw new Error(`后端响应字段 ${path} 应为布尔值`);
  }
  return value;
}

function role(value: unknown, path: string): CompanyRole {
  if (value === "farm" || value === "processor" || value === "retailer") {
    return value;
  }
  throw new Error(`后端响应字段 ${path} 不是已知产业层级`);
}

function policyMode(value: unknown, path: string): PolicyMode {
  if (
    value === "baseline" ||
    value === "codex" ||
    value === "openai" ||
    value === "replay"
  ) {
    return value;
  }
  throw new Error(`后端响应字段 ${path} 不是已知运行模式`);
}

function runStatus(value: unknown, path: string): RunStatus {
  if (
    value === "queued" ||
    value === "running" ||
    value === "interrupted" ||
    value === "completed" ||
    value === "failed"
  ) {
    return value;
  }
  throw new Error(`后端响应字段 ${path} 不是已知运行状态`);
}

function invocationOutcome(value: unknown, path: string): InvocationOutcome {
  if (
    value === "success" ||
    value === "agent_error" ||
    value === "infrastructure_error"
  ) {
    return value;
  }
  throw new Error(`后端响应字段 ${path} 不是已知调用结果`);
}

function nullableText(value: unknown, path: string): string | null {
  if (value === null || value === undefined) {
    return null;
  }
  if (typeof value !== "string") {
    throw new Error(`后端响应字段 ${path} 应为文本或 null`);
  }
  return value.length > 0 ? value : null;
}

function plainText(value: unknown, path: string): string {
  if (typeof value !== "string") {
    throw new Error(`后端响应字段 ${path} 应为文本`);
  }
  return value;
}

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
