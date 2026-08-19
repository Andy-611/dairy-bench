import type { ReactNode } from "react";

import type { ScoreView } from "../../shared/api/types";
import {
  formatExactDecimal,
  formatSignedExactDecimal,
  formatValue,
} from "../../shared/format";

interface EvaluationStandardProps {
  readonly benchmarkEligible: boolean;
  readonly completedWeeks: number;
  readonly provisional: boolean;
  readonly score: ScoreView;
  readonly totalWeeks: number;
}

interface FormulaDefinition {
  readonly description: string;
  readonly expression: ReactNode;
  readonly label: string;
}

interface SymbolDefinition {
  readonly base: string;
  readonly subscript?: string;
}

interface MetricDefinition {
  readonly detail: string;
  readonly symbol: SymbolDefinition;
  readonly tone?: "bankruptcy" | "loss";
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
    expression: (
      <>
        <Formula spoken="E raw equals the sum from i equals 1 to 9 of V i at T minus V i at zero">
          <mrow>
            <MathSubscript base="E" subscript="raw" />
            <mo>=</mo>
            <BoundedSum index="i" upper="9" />
            <mo>[</mo>
            <MathSubscript base="V" subscript="i" />
            <mo>(</mo><mi>T</mi><mo>)</mo>
            <mo>−</mo>
            <MathSubscript base="V" subscript="i" />
            <mo>(</mo><mn>0</mn><mo>)</mo>
            <mo>]</mo>
          </mrow>
        </Formula>
        <Formula spoken="E oracle is the maximum feasible total enterprise surplus">
          <mrow>
            <MathSubscript base="E" subscript="oracle" />
            <mo>=</mo>
            <munder><mo>max</mo><mtext>feasible economy</mtext></munder>
            <MathSubscript base="E" subscript="raw" />
          </mrow>
        </Formula>
        <Formula spoken="E equals E raw divided by E oracle, clipped between zero and one">
          <mrow>
            <mi>E</mi><mo>=</mo><mi>clip</mi>
            <mo>(</mo>
            <mfrac>
              <MathSubscript base="E" subscript="raw" />
              <MathSubscript base="E" subscript="oracle" />
            </mfrac>
            <mo>,</mo><mn>0</mn><mo>,</mo><mn>1</mn>
            <mo>)</mo>
          </mrow>
        </Formula>
      </>
    ),
    description:
      "Total enterprise surplus, normalized by a same-horizon full-information feasible supply-chain Oracle.",
  },
  {
    label: "Fairness",
    expression: (
      <>
        <Formula spoken="G all is the Gini coefficient of all nine companies' evaluated assets">
          <mrow>
            <MathSubscript base="G" subscript="all" /><mo>=</mo>
            <mfrac>
              <mrow>
                <BoundedSum index="i" upper="9" />
                <BoundedSum index="j" upper="9" />
                <mo>|</mo><MathSubscript base="V" subscript="i" />
                <mo>−</mo><MathSubscript base="V" subscript="j" /><mo>|</mo>
              </mrow>
              <mrow>
                <mn>18</mn><BoundedSum index="i" upper="9" />
                <MathSubscript base="V" subscript="i" />
              </mrow>
            </mfrac>
          </mrow>
        </Formula>
        <Formula spoken="F all equals one minus nine eighths times G all, clipped between zero and one">
          <mrow>
            <MathSubscript base="F" subscript="all" /><mo>=</mo><mi>clip</mi>
            <mo>(</mo><mn>1</mn><mo>−</mo>
            <mfrac><mn>9</mn><mn>8</mn></mfrac><mo>·</mo>
            <MathSubscript base="G" subscript="all" />
            <mo>,</mo><mn>0</mn><mo>,</mo><mn>1</mn><mo>)</mo>
          </mrow>
        </Formula>
      </>
    ),
    description: "Equality across all 9 companies at the evaluated horizon.",
  },
  {
    label: "Enterprise outcomes",
    expression: (
      <Formula spoken="P equals one minus loss-making company count L divided by nine">
        <mrow>
          <mi>P</mi><mo>=</mo><mn>1</mn><mo>−</mo><mfrac><mi>L</mi><mn>9</mn></mfrac>
        </mrow>
      </Formula>
    ),
    description: "P is the share of companies that finish without a loss.",
  },
  {
    label: "Final composition",
    expression: (
      <Formula spoken="Score equals 100 times E times the square root of F all times P">
        <mrow>
          <mtext>Score</mtext><mo>=</mo><mn>100</mn><mo>·</mo><mi>E</mi><mo>·</mo>
          <msqrt><MathSubscript base="F" subscript="all" /><mi>P</mi></msqrt>
        </mrow>
      </Formula>
    ),
    description: "Efficiency has full weight; fairness and non-loss participation each have half weight.",
  },
];

export function EvaluationStandard({
  benchmarkEligible,
  completedWeeks,
  provisional,
  score,
  totalWeeks,
}: EvaluationStandardProps) {
  const metricGroups: readonly MetricGroupDefinition[] = [
    {
      title: "Efficiency inputs",
      detail: "System value",
      metrics: [
        {
          symbol: { base: "E", subscript: "raw" },
          value: formatSignedExactDecimal(score.efficiencyRaw),
          detail: "Realized surplus",
        },
        {
          symbol: { base: "E", subscript: "oracle" },
          value: formatExactDecimal(score.efficiencyOracle),
          detail: "Seed-specific Oracle",
        },
        {
          symbol: { base: "E" },
          value: formatExactDecimal(score.efficiencyScore),
          detail: "Normalized efficiency",
        },
      ],
    },
    {
      title: "Global fairness",
      detail: "Lower Gini is fairer",
      metrics: [
        {
          symbol: { base: "G", subscript: "all" },
          value: formatExactDecimal(score.globalGini),
          detail: "Evaluated-asset Gini",
        },
        {
          symbol: { base: "F", subscript: "all" },
          value: formatExactDecimal(score.fairnessScore),
          detail: "Normalized fairness",
        },
      ],
    },
    {
      title: "Enterprise outcomes",
      detail: "Company counts",
      metrics: [
        {
          symbol: { base: "D" },
          value: formatValue(score.bankruptCompanyCount),
          detail: "Bankrupt companies",
          tone: "bankruptcy",
        },
        {
          symbol: { base: "L" },
          value: formatValue(score.lossMakingCompanyCount),
          detail: "Loss-making companies",
          tone: "loss",
        },
      ],
    },
  ];

  return (
    <section aria-labelledby="evaluation-title" className="evaluation-panel panel">
      <header className="section-heading">
        <div>
          <span className="eyebrow">
            {provisional
              ? "PROVISIONAL RESULT"
              : benchmarkEligible
                ? "BENCHMARK RESULT"
                : "DIAGNOSTIC RESULT"}
          </span>
          <h2 id="evaluation-title">Evaluation standard</h2>
        </div>
        <p>
          {provisional ? "The current" : "One final"} score combines system
          efficiency, global fairness, and non-loss participation.
        </p>
      </header>

      <div className="evaluation-overview">
        <article className="final-score-card">
          <span>
            {provisional
              ? "PROVISIONAL SCORE"
              : benchmarkEligible
                ? "FINAL SCORE"
                : "DIAGNOSTIC SCORE"}
          </span>
          <div className="final-score-value">
            <strong>{formatExactDecimal(score.finalScore)}</strong>
            <small>/ 100</small>
          </div>
          <p>
            {provisional
              ? `Through settled week ${completedWeeks} of ${totalWeeks}; excluded from rankings until completion`
              : benchmarkEligible
              ? "The benchmark's primary model-ranking measure"
              : "Excluded from model rankings because the decision protocol was violated"}
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
              <div className="formula-expression">{formula.expression}</div>
              <p>{formula.description}</p>
            </article>
          ))}
        </div>
        <p className="formula-note">
          <MathSymbol base="V" subscript="i" /> is cash plus reference-valued
          inventory minus operating-cost payables. The Oracle uses the same realized
          capacities, convex costs, processing yield, perishability, hidden WTP, shared
          consumers, store costs, and terminal reference values; consumer surplus is
          excluded. Gini uses all 9 companies' values at the evaluated horizon. L
          counts strictly negative surplus; zero surplus is non-loss.
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
          <div key={`${metric.symbol.base}-${metric.symbol.subscript ?? "plain"}`}>
            <dt>
              {metric.tone && <span className={`role-dot ${metric.tone}`} />}
              <span>
                <MathSymbol {...metric.symbol} />
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

function MathSymbol({ base, subscript }: SymbolDefinition) {
  return (
    <span className="math-symbol">
      <var>{base}</var>
      {subscript && <sub>{subscript}</sub>}
    </span>
  );
}

function Formula({ children, spoken }: { readonly children: ReactNode; readonly spoken: string }) {
  return (
    <math aria-label={spoken} className="math-formula">
      {children}
    </math>
  );
}

function MathSubscript({ base, subscript }: Required<SymbolDefinition>) {
  return <msub><mi>{base}</mi><mi>{subscript}</mi></msub>;
}

function BoundedSum({ index, upper }: { readonly index: string; readonly upper: string }) {
  return (
    <munderover>
      <mo>∑</mo>
      <mrow><mi>{index}</mi><mo>=</mo><mn>1</mn></mrow>
      <mn>{upper}</mn>
    </munderover>
  );
}
