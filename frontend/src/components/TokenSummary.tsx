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
      detail: "提示词、企业状态与显式历史",
      label: "输入 Token",
      tone: "input",
      value: usage.inputTokens,
    },
    {
      code: "CACHE",
      detail: `占输入 ${ratio(usage.cachedTokens, usage.inputTokens)}`,
      label: "缓存 Token",
      tone: "cached",
      value: usage.cachedTokens,
    },
    {
      code: "OUT",
      detail: "模型生成的决策与结构化响应",
      label: "输出 Token",
      tone: "output",
      value: usage.outputTokens,
    },
    {
      code: "THINK",
      detail: `占输出 ${ratio(usage.reasoningTokens, usage.outputTokens)}`,
      label: "推理 Token",
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
          <h2 id="token-summary-title">Agent Token 汇总</h2>
        </div>
        <div className="token-run-meta">
          <span className={`token-status ${allSucceeded ? "success" : "warning"}`}>
            <i aria-hidden="true" />
            {summary.successfulInvocations} / {summary.invocationCount} 次成功
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
          <p>由模型运行时逐次返回并汇总</p>
          <div>
            <span>{summary.invocationCount} 次公司日决策</span>
            <span>完整审计记录</span>
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
        缓存 Token 已包含在输入 Token 中，推理 Token 已包含在输出 Token
        中；总量无需再次相加。
      </p>
    </section>
  );
}

function ratio(part: number, whole: number): string {
  return formatPercent(whole > 0 ? part / whole : 0);
}
