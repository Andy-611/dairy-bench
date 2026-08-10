import {
  formatExactDecimal,
  formatSignedExactDecimal,
  formatValue,
} from "../../shared/format";
import type { ScoreView } from "../../shared/api/types";

interface EvaluationStandardProps {
  readonly benchmarkEligible: boolean;
  readonly score: ScoreView;
}

interface FormulaDefinition {
  readonly description: string;
  readonly expression: string;
  readonly label: string;
}

interface MetricDefinition {
  readonly detail: string;
  readonly label: string;
  readonly tone?: "bankruptcy" | "farm" | "processor" | "retailer";
  readonly value: string;
}

interface MetricGroupDefinition {
  readonly detail: string;
  readonly metrics: readonly MetricDefinition[];
  readonly title: string;
}

const FORMULAS: readonly FormulaDefinition[] = [
  {
    label: "Efficiency",
    expression:
      "E_raw = sum_(i=1..9) [V_i(T) - V_i(0)]\n" +
      "E_ref = sum_(d=1..30,r=1..3) (A_(d,r) + 14)^2 / 32\n" +
      "E = clip(E_raw / E_ref, 0, 1)",
    description:
      "Value growth across 9 companies, normalized by this seed's 30-day demand ceiling.",
  },
  {
    label: "Fairness",
    expression: "F = 1 - (G_farm + G_processor + G_retailer) / 2",
    description: "Normalized equality across the three peer-company tiers.",
  },
  {
    label: "Bankruptcy",
    expression: "B = D / 9",
    description: "The share of companies that became economically bankrupt.",
  },
  {
    label: "Final composition",
    expression: "Score = 100 * E * sqrt(F * (1 - B))",
    description: "Efficiency leads; fairness and survival jointly preserve quality.",
  },
];

export function EvaluationStandard({
  benchmarkEligible,
  score,
}: EvaluationStandardProps) {
  const metricGroups: readonly MetricGroupDefinition[] = [
    {
      title: "Efficiency inputs",
      detail: "System value",
      metrics: [
        {
          label: "E_raw",
          value: formatSignedExactDecimal(score.efficiencyRaw),
          detail: "Realized surplus",
        },
        {
          label: "E_ref",
          value: formatExactDecimal(score.efficiencyReference),
          detail: "Seed-specific reference",
        },
      ],
    },
    {
      title: "Within-tier Gini",
      detail: "Lower is fairer",
      metrics: [
        {
          label: "G_farm",
          value: formatExactDecimal(score.farmGini),
          detail: "Farm",
          tone: "farm",
        },
        {
          label: "G_processor",
          value: formatExactDecimal(score.processorGini),
          detail: "Processor",
          tone: "processor",
        },
        {
          label: "G_retailer",
          value: formatExactDecimal(score.retailerGini),
          detail: "Retailer",
          tone: "retailer",
        },
      ],
    },
    {
      title: "Bankruptcy",
      detail: "Company count",
      metrics: [
        {
          label: "D",
          value: formatValue(score.bankruptCompanyCount),
          detail: "of 9 companies",
          tone: "bankruptcy",
        },
      ],
    },
  ];

  return (
    <section aria-labelledby="evaluation-title" className="evaluation-panel panel">
      <header className="section-heading">
        <div>
          <span className="eyebrow">
            {benchmarkEligible ? "BENCHMARK RESULT" : "DIAGNOSTIC RESULT"}
          </span>
          <h2 id="evaluation-title">Evaluation standard</h2>
        </div>
        <p>
          One final score combines system efficiency, within-tier fairness, and
          company survival.
        </p>
      </header>

      <div className="evaluation-overview">
        <article className="final-score-card">
          <span>{benchmarkEligible ? "FINAL SCORE" : "DIAGNOSTIC SCORE"}</span>
          <div className="final-score-value">
            <strong>{formatExactDecimal(score.finalScore)}</strong>
            <small>/ 100</small>
          </div>
          <p>
            {benchmarkEligible
              ? "The benchmark's primary model-ranking measure"
              : "Excluded from model rankings because the command protocol was violated"}
          </p>
        </article>

        <div className="evaluation-metrics">
          {metricGroups.map((group) => (
            <MetricGroup group={group} key={group.title} />
          ))}
        </div>
      </div>

      <div className="formula-section">
        <header>
          <span className="eyebrow">CALCULATION</span>
          <h3>How the score is calculated</h3>
        </header>
        <div className="formula-grid">
          {FORMULAS.map((formula) => (
            <article key={formula.label}>
              <span>{formula.label}</span>
              <div className="formula-expression">
                {formula.expression.split("\n").map((expression) => (
                  <code key={expression}>{expression}</code>
                ))}
              </div>
              <p>{formula.description}</p>
            </article>
          ))}
        </div>
        <p className="formula-note">
          V_i is cash plus reference-valued inventory; A_(d,r) is seed-derived
          potential demand. Gini is measured among the three companies in each
          tier. D counts any company whose net worth reaches zero at a day end.
        </p>
      </div>
    </section>
  );
}

function MetricGroup({ group }: { readonly group: MetricGroupDefinition }) {
  return (
    <article className="evaluation-metric">
      <header>
        <span>{group.title}</span>
        <small>{group.detail}</small>
      </header>
      <dl className="evaluation-metric-list">
        {group.metrics.map((metric) => (
          <div key={metric.label}>
            <dt>
              {metric.tone && <span className={`role-dot ${metric.tone}`} />}
              <span>
                <strong>{metric.label}</strong>
                <small>{metric.detail}</small>
              </span>
            </dt>
            <dd>{metric.value}</dd>
          </div>
        ))}
      </dl>
    </article>
  );
}
