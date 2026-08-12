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
      <Formula
        spoken="F equals one minus the sum of farm, processor, and retailer Gini coefficients divided by two"
      >
        <mrow>
          <mi>F</mi><mo>=</mo><mn>1</mn><mo>−</mo>
          <mfrac>
            <mrow>
              <MathSubscript base="G" subscript="farm" />
              <mo>+</mo>
              <MathSubscript base="G" subscript="processor" />
              <mo>+</mo>
              <MathSubscript base="G" subscript="retailer" />
            </mrow>
            <mn>2</mn>
          </mfrac>
        </mrow>
      </Formula>
    ),
    description: "Normalized equality across the three peer-company tiers.",
  },
  {
    label: "Bankruptcy",
    expression: (
      <Formula spoken="B equals D divided by 9">
        <mrow><mi>B</mi><mo>=</mo><mfrac><mi>D</mi><mn>9</mn></mfrac></mrow>
      </Formula>
    ),
    description: "The share of companies that became economically bankrupt.",
  },
  {
    label: "Final composition",
    expression: (
      <Formula spoken="Score equals 100 times E times the square root of F times one minus B">
        <mrow>
          <mtext>Score</mtext><mo>=</mo><mn>100</mn><mo>·</mo><mi>E</mi><mo>·</mo>
          <msqrt>
            <mi>F</mi><mo>(</mo><mn>1</mn><mo>−</mo><mi>B</mi><mo>)</mo>
          </msqrt>
        </mrow>
      </Formula>
    ),
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
          symbol: { base: "E", subscript: "raw" },
          value: formatSignedExactDecimal(score.efficiencyRaw),
          detail: "Realized surplus",
        },
        {
          symbol: { base: "E", subscript: "ref" },
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
          symbol: { base: "G", subscript: "farm" },
          value: formatExactDecimal(score.farmGini),
          detail: "Farm",
          tone: "farm",
        },
        {
          symbol: { base: "G", subscript: "processor" },
          value: formatExactDecimal(score.processorGini),
          detail: "Processor",
          tone: "processor",
        },
        {
          symbol: { base: "G", subscript: "retailer" },
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
          symbol: { base: "D" },
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
          demand. Gini is measured among the three companies in each tier. D counts any
          company whose net worth is at or below 1.0000 at a week end.
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
