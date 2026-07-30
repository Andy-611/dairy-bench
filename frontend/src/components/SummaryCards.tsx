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
      label: "Eligibility",
      value: score.eligible ? "Passed" : "Not passed",
      detail: score.eligible
        ? "Eligible for efficiency ranking"
        : "At least one hard constraint failed",
      tone: score.eligible ? "positive" : "negative",
    },
    {
      label: "System efficiency",
      value: formatSignedValue(score.efficiency),
      detail: "Cumulative surplus created by all six companies",
      tone: score.efficiency >= 0 ? "positive" : "negative",
    },
    {
      label: "Fairness",
      value: formatPercent(score.fairness),
      detail: "1 minus the mean within-tier Gini",
      tone: "neutral",
    },
    {
      label: "Demand fulfillment",
      value: formatPercent(score.fulfillmentRate),
      detail: "Fulfilled retail demand divided by total demand",
      tone: "neutral",
    },
    {
      label: "Expired volume",
      value: formatValue(score.expiredQuantity),
      detail: "Total milk discarded during the 30-day run",
      tone: score.expiredQuantity > 0 ? "negative" : "positive",
    },
  ];

  return (
    <section aria-label="Run summary" className="summary-grid">
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
