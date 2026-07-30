import { formatValue } from "../format";
import type { DailySnapshotView } from "../types";

interface MetricChartProps {
  readonly snapshots: readonly DailySnapshotView[];
}

interface DayMetrics {
  readonly day: number;
  readonly efficiency: number;
  readonly inventoryValue: number;
  readonly consumerSales: number;
}

interface Series {
  readonly key: keyof Omit<DayMetrics, "day">;
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
  { key: "consumerSales", label: "Daily consumer sales", color: "#e08c30" },
];

function aggregateSnapshots(
  snapshots: readonly DailySnapshotView[],
): readonly DayMetrics[] {
  const byDay = new Map<number, DayMetrics>();

  for (const snapshot of snapshots) {
    const current = byDay.get(snapshot.day) ?? {
      day: snapshot.day,
      efficiency: 0,
      inventoryValue: 0,
      consumerSales: 0,
    };
    byDay.set(snapshot.day, {
      day: snapshot.day,
      efficiency: current.efficiency + snapshot.cumulativeSurplus,
      inventoryValue: current.inventoryValue + snapshot.inventoryValue,
      consumerSales:
        current.consumerSales + snapshot.consumerSalesQuantity,
    });
  }

  return [...byDay.values()].sort((left, right) => left.day - right.day);
}

function linePath(
  metrics: readonly DayMetrics[],
  series: Series,
  row: number,
): string {
  const values = metrics.map((metric) => metric[series.key]);
  const minimum = Math.min(0, ...values);
  const maximum = Math.max(0, ...values);
  const span = maximum - minimum || 1;
  const firstDay = metrics[0]?.day ?? 1;
  const lastDay = metrics.at(-1)?.day ?? firstDay + 1;
  const daySpan = lastDay - firstDay || 1;
  const rowTop = TOP + row * (ROW_HEIGHT + ROW_GAP);

  return metrics
    .map((metric, index) => {
      const x =
        LEFT + ((metric.day - firstDay) / daySpan) * (WIDTH - LEFT - RIGHT);
      const y =
        rowTop + ROW_HEIGHT - ((metric[series.key] - minimum) / span) * ROW_HEIGHT;
      return `${index === 0 ? "M" : "L"} ${x.toFixed(2)} ${y.toFixed(2)}`;
    })
    .join(" ");
}

export function MetricChart({ snapshots }: MetricChartProps) {
  const metrics = aggregateSnapshots(snapshots);
  const firstDay = metrics[0]?.day ?? 1;
  const lastDay = metrics.at(-1)?.day ?? 30;
  const tickDays = [1, 5, 10, 15, 20, 25, 30].filter(
    (day) => day >= firstDay && day <= lastDay,
  );
  const daySpan = lastDay - firstDay || 1;

  return (
    <section className="panel chart-panel">
      <div className="section-heading">
        <div>
          <span className="eyebrow">30-DAY TREND</span>
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
          <title id="trend-title">30-day supply-chain trends</title>
          <desc id="trend-description">
            Cumulative system surplus, inventory value, and daily consumer
            sales.
          </desc>
          {tickDays.map((day) => {
            const x = LEFT + ((day - firstDay) / daySpan) * (WIDTH - LEFT - RIGHT);
            return (
              <g key={day}>
                <line
                  className="chart-grid"
                  x1={x}
                  x2={x}
                  y1={TOP}
                  y2={HEIGHT - 24}
                />
                <text className="chart-tick" textAnchor="middle" x={x} y={HEIGHT - 4}>
                  Day {day}
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
                    ((metric.day - firstDay) / daySpan) *
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
                      key={metric.day}
                      r={3}
                    >
                      <title>
                        Day {metric.day}: {formatValue(metric[series.key])}
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
