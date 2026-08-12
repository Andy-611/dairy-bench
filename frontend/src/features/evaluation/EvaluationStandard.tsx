import type { ReactNode } from "react";

import type { ScoreView } from "../../shared/api/types";
import {
  formatExactDecimal,
  formatSignedExactDecimal,
  formatValue,
} from "../../shared/format";

interface EvaluationStandardProps {
  readonly benchmarkEligible: boolean;
  readonly score: ScoreView;
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
        <Formula
          spoken="E reference equals the sum over 52 weeks and 3 retailers of A w r plus 14 squared, divided by 32"
        >
          <mrow>
            <MathSubscript base="E" subscript="ref" />
            <mo>=</mo>
            <BoundedSum index="w" upper="52" />
            <BoundedSum index="r" upper="3" />
            <mfrac>
              <msup>
                <mrow>
                  <mo>(</mo>
                  <MathSubscript base="A" subscript="w,r" />
                  <mo>+</mo><mn>14</mn>
                  <mo>)</mo>
                </mrow>
                <mn>2</mn>
              </msup>
              <mn>32</mn>
            </mfrac>
          </mrow>
        </Formula>
        <Formula spoken="E equals E raw divided by E reference, clipped between zero and one">
          <mrow>
            <mi>E</mi><mo>=</mo><mi>clip</mi>
            <mo>(</mo>
            <mfrac>
              <MathSubscript base="E" subscript="raw" />
              <MathSubscript base="E" subscript="ref" />
            </mfrac>
            <mo>,</mo><mn>0</mn><mo>,</mo><mn>1</mn>
            <mo>)</mo>
          </mrow>
        </Formula>
      </>
    ),
    description:
      "Value growth across 9 companies, normalized by this seed's 52-week demand ceiling.",
  },
  {
    label: "Fairness",
    expression: (
      <>
        <Formula spoken="G all is the Gini coefficient of all nine companies' final assets">
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
    description: "Equality across all 9 companies, measured from final assets.",
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
    description: "Efficiency has full weight; fairness and profitable participation each have half weight.",
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
          symbol: { base: "E", subscript: "raw" },
          value: formatSignedExactDecimal(score.efficiencyRaw),
          detail: "Realized surplus",
        },
        {
          symbol: { base: "E", subscript: "ref" },
          value: formatExactDecimal(score.efficiencyReference),
          detail: "Seed-specific reference",
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
          detail: "Final-asset Gini",
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
            {benchmarkEligible ? "BENCHMARK RESULT" : "DIAGNOSTIC RESULT"}
          </span>
          <h2 id="evaluation-title">Evaluation standard</h2>
        </div>
        <p>
          One final score combines system efficiency, global fairness, and
          profitable participation.
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
          inventory; <MathSymbol base="A" subscript="w,r" /> is seed-derived potential
          demand. Gini uses all 9 companies' final assets. L counts negative final
          surplus.
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
