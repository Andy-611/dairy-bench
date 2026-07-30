import { formatPercent, formatValue } from "../format";
import type { AgentUsageSummaryView } from "../types";

interface TokenSummaryProps {
  readonly summary: AgentUsageSummaryView;
}

interface TokenMetric {
  readonly code: string;
  readonly detail: string;
  readonly label: string;
  readonly tone: "cached" | "input" | "output" | "reasoning";
  readonly value: number;
}

export function TokenSummary({ summary }: TokenSummaryProps) {
  const { usage } = summary;
  const metrics: readonly TokenMetric[] = [
    {
      code: "IN",
      detail: "Prompts, company state, and explicit history",
      label: "Input tokens",
      tone: "input",
      value: usage.inputTokens,
    },
    {
      code: "CACHE",
      detail: `${ratio(usage.cachedTokens, usage.inputTokens)} of input`,
      label: "Cached tokens",
      tone: "cached",
      value: usage.cachedTokens,
    },
    {
      code: "OUT",
      detail: "Model decisions and structured responses",
      label: "Output tokens",
      tone: "output",
      value: usage.outputTokens,
    },
    {
      code: "THINK",
      detail: `${ratio(usage.reasoningTokens, usage.outputTokens)} of output`,
      label: "Reasoning tokens",
      tone: "reasoning",
      value: usage.reasoningTokens,
    },
  ];
  const allSucceeded =
    summary.successfulInvocations === summary.invocationCount;

  return (
    <section aria-labelledby="token-summary-title" className="panel token-panel">
      <div className="section-heading token-heading">
        <div>
          <span className="eyebrow">AGENT USAGE</span>
          <h2 id="token-summary-title">Agent token usage</h2>
        </div>
        <div className="token-run-meta">
          <span className={`token-status ${allSucceeded ? "success" : "warning"}`}>
            <i aria-hidden="true" />
            {summary.successfulInvocations} / {summary.invocationCount} successful
          </span>
          <span className="token-model">
            {[...summary.providers, ...summary.models].join(" · ")}
          </span>
        </div>
      </div>

      <div className="token-overview">
        <article className="token-total-card">
          <span>TOTAL TOKENS</span>
          <strong>{formatValue(usage.totalTokens)}</strong>
          <p>Reported and aggregated by the model runtime</p>
          <div>
            <span>{summary.invocationCount} Agent turns</span>
            <span>Fully auditable</span>
          </div>
        </article>

        <div className="token-stat-grid">
          {metrics.map((metric) => (
            <article className={`token-stat-card ${metric.tone}`} key={metric.code}>
              <div>
                <span>{metric.label}</span>
                <small>{metric.code}</small>
              </div>
              <strong>{formatValue(metric.value)}</strong>
              <p>{metric.detail}</p>
            </article>
          ))}
        </div>
      </div>

      <p className="token-footnote">
        Cached tokens are included in input tokens; reasoning tokens are included
        in output tokens. Do not add them to the total again.
      </p>
    </section>
  );
}

function ratio(part: number, whole: number): string {
  return formatPercent(whole > 0 ? part / whole : 0);
}
