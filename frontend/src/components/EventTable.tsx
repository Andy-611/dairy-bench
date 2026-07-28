import { useEffect, useMemo, useState } from "react";

import { formatEventValue, formatValue } from "../format";
import type {
  EventView,
  InvocationArtifactsView,
  PolicyInvocationView,
} from "../types";

type ArtifactLoader = (
  runId: string,
  invocationId: string,
) => Promise<InvocationArtifactsView | null>;

interface EventTableProps {
  readonly events: readonly EventView[];
  readonly invocations: readonly PolicyInvocationView[];
  readonly loadArtifacts: ArtifactLoader;
  readonly runId: string;
}

interface EventCardProps {
  readonly event: EventView;
  readonly hasAnyInvocations: boolean;
  readonly invocationIndex: ReadonlyMap<string, PolicyInvocationView>;
  readonly loadArtifacts: ArtifactLoader;
  readonly runId: string;
}

interface InvocationAuditProps {
  readonly invocation: PolicyInvocationView;
  readonly loadArtifacts: ArtifactLoader;
  readonly runId: string;
}

type ArtifactState =
  | { readonly status: "loading" }
  | { readonly status: "loaded"; readonly value: InvocationArtifactsView }
  | { readonly status: "unavailable"; readonly message: string }
  | { readonly status: "failed"; readonly message: string };

const EVENT_LABELS: Readonly<Record<string, string>> = {
  milk_produced: "生产原奶",
  trade_executed: "现货成交",
  milk_processed: "加工盒装奶",
  consumer_sale: "零售成交",
  inventory_expired: "库存过期",
  decision_rejected: "决策被拒绝",
  policy_failed: "策略执行失败",
};

const FIELD_LABELS: Readonly<Record<string, string>> = {
  actual_input: "实际投入",
  actual_quantity: "实际数量",
  cash_cost: "现金成本",
  demand_quantity: "消费者需求",
  lot_id: "库存批次",
  output_lot_id: "产出批次",
  output_quantity: "盒装奶产量",
  potential_demand_quantity: "潜在需求",
  product: "商品",
  quantity: "成交数量",
  reason: "原因",
  reference_value_loss: "价值损失",
  requested_input: "计划投入",
  requested_quantity: "计划数量",
  retail_price: "零售价",
  revenue: "零售收入",
  sold_quantity: "实际销量",
  total_value: "成交总额",
  unit_cost: "单位成本",
  unit_price: "成交单价",
};

const OUTCOME_LABELS: Readonly<Record<PolicyInvocationView["outcome"], string>> = {
  success: "调用成功",
  agent_error: "输出失败",
  infrastructure_error: "运行失败",
};

const VALUE_LABELS: Readonly<Record<string, string>> = {
  bottled_milk: "盒装奶",
  raw_milk: "原奶",
};

function eventLabel(type: string): string {
  return EVENT_LABELS[type] ?? type.replaceAll("_", " ");
}

function eventDetails(event: EventView): string {
  const details = Object.entries(event.payload)
    .filter(([, value]) => typeof value !== "object")
    .slice(0, 5)
    .map(([key, value]) => {
      const formatted =
        typeof value === "string" ? VALUE_LABELS[value] : undefined;
      return `${FIELD_LABELS[key] ?? key}: ${formatted ?? formatEventValue(value)}`;
    });
  return details.length > 0 ? details.join(" · ") : "无附加数据";
}

export function EventTable({
  events,
  invocations,
  loadArtifacts,
  runId,
}: EventTableProps) {
  const orderedEvents = [...events].sort(
    (left, right) => left.day - right.day || left.sequence - right.sequence,
  );
  const invocationIndex = useMemo(
    () =>
      new Map(
        invocations.map((invocation) => [
          invocationKey(invocation.day, invocation.companyId),
          invocation,
        ]),
      ),
    [invocations],
  );

  return (
    <section className="panel">
      <div className="section-heading">
        <div>
          <span className="eyebrow">AUDIT TRAIL</span>
          <h2>经济事件时间线</h2>
        </div>
        <p>{orderedEvents.length} 条由环境确认的事实 · 点击事件查看 Agent 轨迹</p>
      </div>
      {orderedEvents.length === 0 ? (
        <p className="inline-empty">本次运行没有产生经济事件。</p>
      ) : (
        <ol className="event-list">
          {orderedEvents.map((event) => (
            <EventCard
              event={event}
              hasAnyInvocations={invocations.length > 0}
              invocationIndex={invocationIndex}
              key={`${event.day}-${event.sequence}`}
              loadArtifacts={loadArtifacts}
              runId={runId}
            />
          ))}
        </ol>
      )}
    </section>
  );
}

function EventCard({
  event,
  hasAnyInvocations,
  invocationIndex,
  loadArtifacts,
  runId,
}: EventCardProps) {
  const [isOpen, setIsOpen] = useState(false);
  const companyIds = relatedCompanyIds(event);
  const relatedInvocations = companyIds.flatMap((companyId) => {
    const invocation = invocationIndex.get(invocationKey(event.day, companyId));
    return invocation ? [invocation] : [];
  });
  const missingCompanyIds = companyIds.filter(
    (companyId) =>
      !invocationIndex.has(invocationKey(event.day, companyId)),
  );
  const details = eventDetails(event);

  return (
    <li>
      <div className="event-marker">
        <span>DAY</span>
        <strong>{event.day}</strong>
      </div>
      <details
        className="event-card"
        onToggle={(change) => setIsOpen(change.currentTarget.open)}
      >
        <summary>
          <div className="event-summary">
            <div className="event-title">
              <strong>{eventLabel(event.type)}</strong>
              <span className="event-tag">#{event.sequence}</span>
            </div>
            <p title={details}>{details}</p>
            {companyIds.length > 0 && (
              <small>{companyIds.join(" → ")}</small>
            )}
          </div>
          <span aria-hidden="true" className="event-chevron" />
        </summary>
        {isOpen && (
          <div className="event-audit">
            {relatedInvocations.length > 0 && (
              <p className="audit-context">
                以下是本事件相关公司的当日 Agent 调用记录，不代表事件的完整因果链。
              </p>
            )}
            {relatedInvocations.map((invocation) => (
              <InvocationAudit
                invocation={invocation}
                key={invocation.invocationId}
                loadArtifacts={loadArtifacts}
                runId={runId}
              />
            ))}
            {missingCompanyIds.length > 0 && (
              <MissingInvocation
                companyIds={missingCompanyIds}
                hasAnyInvocations={hasAnyInvocations}
              />
            )}
            {companyIds.length === 0 && (
              <p className="trace-notice">
                该环境事件没有可直接关联的公司 Agent。
              </p>
            )}
          </div>
        )}
      </details>
    </li>
  );
}

function InvocationAudit({
  invocation,
  loadArtifacts,
  runId,
}: InvocationAuditProps) {
  const isCodex = invocation.provider.toLowerCase() === "codex";
  const canLoadArtifacts =
    isCodex && invocation.threadId !== null && invocation.turnId !== null;
  const [artifacts, setArtifacts] = useState<ArtifactState>(() =>
    initialArtifactState(invocation),
  );

  useEffect(() => {
    if (!canLoadArtifacts) {
      return;
    }
    let active = true;
    void loadArtifacts(runId, invocation.invocationId).then(
      (value) => {
        if (active) {
          setArtifacts(
            value
              ? { status: "loaded", value }
              : {
                  status: "unavailable",
                  message:
                    "No saved Codex trace is available for this invocation. The final decision is loaded from the database.",
                },
          );
        }
      },
      (reason: unknown) => {
        if (active) {
          setArtifacts({ status: "failed", message: errorMessage(reason) });
        }
      },
    );
    return () => {
      active = false;
    };
  }, [
    canLoadArtifacts,
    invocation.invocationId,
    loadArtifacts,
    runId,
  ]);

  const loaded = artifacts.status === "loaded" ? artifacts.value : null;
  const threadId = loaded?.threadId ?? invocation.threadId;
  const turnId = loaded?.turnId ?? invocation.turnId;
  const artifactOutput = loaded?.finalOutput;
  const finalOutput =
    artifactOutput?.trim() ? artifactOutput : formattedDecision(invocation);

  return (
    <article className="invocation-audit">
      <header>
        <div>
          <strong>{invocation.companyId}</strong>
          <span className={`invocation-outcome ${invocation.outcome}`}>
            {OUTCOME_LABELS[invocation.outcome]}
          </span>
        </div>
        <span>{loaded?.model ?? invocation.model}</span>
      </header>

      <dl className="invocation-meta">
        <MetaItem label="Provider" value={invocation.provider} />
        <MetaItem
          label={isCodex ? "Thread" : "Request"}
          value={threadId ?? "—"}
        />
        <MetaItem
          label={isCodex ? "Turn" : "Response"}
          value={turnId ?? "—"}
        />
        <MetaItem
          label="Token"
          value={formatValue(invocation.usage.totalTokens)}
        />
        <MetaItem label="Attempts" value={formatValue(invocation.attempts)} />
        <MetaItem
          label="Latency"
          value={`${formatValue(invocation.latencyMs)} ms`}
        />
      </dl>

      <TraceSection
        artifacts={artifacts}
        errorMessage={invocation.errorMessage}
        finalOutput={finalOutput}
      />
    </article>
  );
}

function TraceSection({
  artifacts,
  errorMessage: invocationError,
  finalOutput,
}: {
  readonly artifacts: ArtifactState;
  readonly errorMessage: string | null;
  readonly finalOutput: string | null;
}) {
  const reasoning =
    artifacts.status === "loaded" ? artifacts.value.reasoningMarkdown : null;

  return (
    <div aria-live="polite" className="trace-grid">
      <section className="trace-block">
        <h3>Public Reasoning Trace</h3>
        {artifacts.status === "loading" ? (
          <p className="trace-notice">
            <span aria-hidden="true" className="spinner dark" /> Loading the
            local Session trace…
          </p>
        ) : artifacts.status === "unavailable" ? (
          <p className="trace-notice">{artifacts.message}</p>
        ) : artifacts.status === "failed" ? (
          <p className="trace-notice warning">{artifacts.message}</p>
        ) : reasoning?.trim() ? (
          <pre>{reasoning}</pre>
        ) : (
          <p className="trace-notice">
            No public reasoning summary is available for this invocation.
          </p>
        )}
      </section>

      <section className="trace-block">
        <h3>Final Output</h3>
        {finalOutput ? (
          <pre>{finalOutput}</pre>
        ) : (
          <p className="trace-notice warning">
            {invocationError ?? "This invocation did not produce a final output."}
          </p>
        )}
      </section>
    </div>
  );
}

function MetaItem({
  label,
  value,
}: {
  readonly label: string;
  readonly value: string;
}) {
  return (
    <div>
      <dt>{label}</dt>
      <dd title={value}>{value}</dd>
    </div>
  );
}

function MissingInvocation({
  companyIds,
  hasAnyInvocations,
}: {
  readonly companyIds: readonly string[];
  readonly hasAnyInvocations: boolean;
}) {
  return (
    <p className="trace-notice missing">
      {hasAnyInvocations
        ? `未找到 ${companyIds.join("、")} 在本日的 Agent 调用记录。`
        : "本次为规则策略或复放运行，没有调用 Agent，因此没有 Session 轨迹。"}
    </p>
  );
}

function initialArtifactState(
  invocation: PolicyInvocationView,
): ArtifactState {
  if (invocation.provider.toLowerCase() !== "codex") {
    return {
      status: "unavailable",
      message: `This invocation used ${invocation.provider}, so no Codex Session trace exists.`,
    };
  }
  if (invocation.threadId === null || invocation.turnId === null) {
    return {
      status: "unavailable",
      message:
        invocation.errorMessage ??
        "This invocation did not produce an exportable Codex Session trace.",
    };
  }
  return { status: "loading" };
}

function relatedCompanyIds(event: EventView): readonly string[] {
  return [...new Set(
    [event.actorCompanyId, event.counterpartyCompanyId].filter(
      (companyId): companyId is string => companyId !== null,
    ),
  )];
}

function invocationKey(day: number, companyId: string): string {
  return `${day}\u0000${companyId}`;
}

function formattedDecision(invocation: PolicyInvocationView): string | null {
  return invocation.decision === null
    ? null
    : JSON.stringify(invocation.decision, null, 2);
}

function errorMessage(reason: unknown): string {
  return reason instanceof Error
    ? `The trace file could not be read: ${reason.message}`
    : "The trace file could not be read.";
}
