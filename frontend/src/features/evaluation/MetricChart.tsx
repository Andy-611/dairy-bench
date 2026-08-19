import type { WeeklySnapshotView } from "../../shared/api/types";
import { formatValue } from "../../shared/format";

interface MetricChartProps {
  readonly snapshots: readonly WeeklySnapshotView[];
  readonly totalWeeks: number;
}

interface WeekMetrics {
  readonly week: number;
  readonly efficiency: number;
  readonly inventoryValue: number;
  readonly consumerSales: number;
}

interface Series {
  readonly key: keyof Omit<WeekMetrics, "week">;
  readonly label: string;
  readonly color: string;
}

const WIDTH = 960;
const HEIGHT = 330;
const LEFT = 150;
const RIGHT = 32;
const TOP = 20;
const ROW_HEIGHT = 72;
const ROW_GAP = 25;

const SERIES: readonly Series[] = [
  { key: "efficiency", label: "Cumulative surplus", color: "#147d64" },
  { key: "inventoryValue", label: "Inventory value", color: "#2d6cdf" },
  { key: "consumerSales", label: "Weekly consumer sales", color: "#e08c30" },
];

function aggregateSnapshots(
  snapshots: readonly WeeklySnapshotView[],
): readonly WeekMetrics[] {
  const byWeek = new Map<number, WeekMetrics>();

  for (const snapshot of snapshots) {
    const current = byWeek.get(snapshot.week) ?? {
      week: snapshot.week,
      efficiency: 0,
      inventoryValue: 0,
      consumerSales: 0,
    };
    byWeek.set(snapshot.week, {
      week: snapshot.week,
      efficiency: current.efficiency + Number(snapshot.cumulativeSurplus),
      inventoryValue: current.inventoryValue + Number(snapshot.inventoryValue),
      consumerSales:
        current.consumerSales + Number(snapshot.consumerSalesQuantity),
    });
  }

  return [...byWeek.values()].sort((left, right) => left.week - right.week);
}

function linePath(
  metrics: readonly WeekMetrics[],
  series: Series,
  row: number,
): string {
  const values = metrics.map((metric) => metric[series.key]);
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const span = maximum - minimum || 1;
  const firstWeek = metrics[0]?.week ?? 1;
  const lastWeek = metrics.at(-1)?.week ?? firstWeek + 1;
  const weekSpan = lastWeek - firstWeek || 1;
  const rowTop = TOP + row * (ROW_HEIGHT + ROW_GAP);

  return metrics
    .map((metric, index) => {
      const x =
        LEFT + ((metric.week - firstWeek) / weekSpan) * (WIDTH - LEFT - RIGHT);
      const y =
        rowTop + ROW_HEIGHT - ((metric[series.key] - minimum) / span) * ROW_HEIGHT;
      return `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
}

export function MetricChart({ snapshots, totalWeeks }: MetricChartProps) {
  const metrics = aggregateSnapshots(snapshots);
  const firstWeek = metrics[0]?.week ?? 1;
  const lastWeek = metrics.at(-1)?.week ?? totalWeeks;
  const tickWeeks = chartTicks(firstWeek, lastWeek);
  const weekSpan = lastWeek - firstWeek || 1;

  return (
    <section className="panel chart-panel">
      <div className="section-heading">
        <div>
          <span className="eyebrow">
            {lastWeek < totalWeeks
              ? `TREND THROUGH WEEK ${lastWeek}`
              : `${totalWeeks}-WEEK TREND`}
          </span>
          <h2>Supply-chain trends</h2>
        </div>
        <p>Each series uses its own vertical scale.</p>
      </div>
      <div className="chart-scroll">
        <svg
          aria-labelledby="trend-title trend-description"
          className="trend-chart"
          role="img"
          viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
        >
          <title id="trend-title">
            Supply-chain trends through week {lastWeek}
          </title>
          <desc id="trend-description">
            Cumulative system surplus, inventory value, and weekly consumer
            sales.
          </desc>
          {tickWeeks.map((week) => {
            const x = LEFT + ((week - firstWeek) / weekSpan) * (WIDTH - LEFT - RIGHT);
            return (
              <g key={week}>
                <line
                  className="chart-grid"
                  x1={x}
                  x2={x}
                  y1={TOP}
                  y2={HEIGHT - 24}
                />
                <text className="chart-tick" textAnchor="middle" x={x} y={HEIGHT - 4}>
                  Week {week}
                </text>
              </g>
            );
          })}
          {SERIES.map((series, row) => {
            const values = metrics.map((metric) => metric[series.key]);
            const rowTop = TOP + row * (ROW_HEIGHT + ROW_GAP);
            const maximum = Math.max(0, ...values);
            const minimum = Math.min(0, ...values);

            return (
              <g key={series.key}>
                <rect
                  className="chart-row"
                  height={ROW_HEIGHT}
                  rx={8}
                  width={WIDTH - LEFT - RIGHT}
                  x={LEFT}
                  y={rowTop}
                />
                <text className="chart-label" x={16} y={rowTop + 30}>
                  {series.label}
                </text>
                <text className="chart-range" x={16} y={rowTop + 52}>
                  {formatValue(minimum)} to {formatValue(maximum)}
                </text>
                <path
                  className="chart-line"
                  d={linePath(metrics, series, row)}
                  stroke={series.color}
                />
                {metrics.map((metric) => {
                  const span = maximum - minimum || 1;
                  const x =
                    LEFT +
                    ((metric.week - firstWeek) / weekSpan) *
                      (WIDTH - LEFT - RIGHT);
                  const y =
                    rowTop +
                    ROW_HEIGHT -
                    ((metric[series.key] - minimum) / span) * ROW_HEIGHT;
                  return (
                    <circle
                      className="chart-point"
                      cx={x}
                      cy={y}
                      fill={series.color}
                      key={metric.week}
                      r={3}
                    >
                      <title>
                        Week {metric.week}: {formatValue(metric[series.key])}
                      </title>
                    </circle>
                  );
                })}
              </g>
            );
          })}
        </svg>
      </div>
    </section>
  );
}

function chartTicks(firstWeek: number, lastWeek: number): readonly number[] {
  const span = Math.max(0, lastWeek - firstWeek);
  const positions = [0, 0.25, 0.5, 0.75, 1];
  return [
    ...new Set(
      positions.map(
        (position) => firstWeek + Math.round(span * position),
      ),
    ),
  ];
}
