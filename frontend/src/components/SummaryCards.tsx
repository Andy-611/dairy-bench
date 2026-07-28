import { formatPercent, formatSignedValue, formatValue } from "../format";
import type { ScoreView } from "../types";

interface SummaryCardsProps {
  readonly score: ScoreView;
}

interface MetricCard {
  readonly label: string;
  readonly value: string;
  readonly detail: string;
  readonly tone: "positive" | "negative" | "neutral";
}

export function SummaryCards({ score }: SummaryCardsProps) {
  const cards: readonly MetricCard[] = [
    {
      label: "公平门槛",
      value: score.eligible ? "已通过" : "未通过",
      detail: score.eligible ? "具备效率排名资格" : "至少一项硬约束未满足",
      tone: score.eligible ? "positive" : "negative",
    },
    {
      label: "系统效率",
      value: formatSignedValue(score.efficiency),
      detail: "六家公司累计创造的剩余",
      tone: score.efficiency >= 0 ? "positive" : "negative",
    },
    {
      label: "公平度",
      value: formatPercent(score.fairness),
      detail: "1 − 层级内平均 Gini",
      tone: "neutral",
    },
    {
      label: "需求满足率",
      value: formatPercent(score.fulfillmentRate),
      detail: "实际零售量 ÷ 消费需求",
      tone: "neutral",
    },
    {
      label: "过期量",
      value: formatValue(score.expiredQuantity),
      detail: "30 天内报废的鲜奶总量",
      tone: score.expiredQuantity > 0 ? "negative" : "positive",
    },
  ];

  return (
    <section aria-label="运行摘要" className="summary-grid">
      {cards.map((card) => (
        <article className={`metric-card ${card.tone}`} key={card.label}>
          <span className="metric-label">{card.label}</span>
          <strong>{card.value}</strong>
          <small>{card.detail}</small>
        </article>
      ))}
    </section>
  );
}
