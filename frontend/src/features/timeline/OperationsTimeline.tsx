import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { CSSProperties } from "react";

import type {
  SystemTimelineItemView,
  TimelineContextView,
  TimelineDayFrameView,
  TimelineDetailView,
  TimelineWeekView,
  TurnTimelineItemView,
} from "../../shared/api/types";
import {
  formatExactDecimal,
  formatPercent,
  formatValue,
} from "../../shared/format";
import { companyLabel } from "../../shared/labels";
import { isAbortError, requestErrorMessage } from "../../shared/requestErrors";
import {
  simulationDayLabel,
  weekdayName,
} from "../../shared/simulationCalendar";
import {
  ACCEPTED_BY_ENGINE_LABEL,
  decisionContextSummary,
  decisionDispositionLabel,
  decisionKind,
  decisionLabel,
  decisionProcessingResult,
  decisionSummary,
  nextDecisionTiming,
  plural,
  systemLabel,
  wakeLabel,
  type DecisionKind,
} from "../../shared/timelineFormatters";
import { MarketDisplay } from "../market/MarketDisplay";
import { DecisionDrawer } from "./DecisionDrawer";
import { TimelineError, TimelineNotice } from "./TimelineFeedback";

type TimelineLoader = (
  runId: string,
  week: number,
  signal?: AbortSignal,
) => Promise<TimelineWeekView>;
type DetailLoader = (
  runId: string,
  entryId: string,
  signal?: AbortSignal,
) => Promise<TimelineDetailView>;
type StatusFilter = "accepted" | "all" | "rejected";

interface OperationsTimelineProps {
  readonly loadDetail: DetailLoader;
  readonly loadTimeline: TimelineLoader;
  readonly onWeekChange: (week: number) => void;
  readonly revision: string;
  readonly runId: string;
  readonly timelinePending?: boolean;
  readonly week: number;
  readonly weeks: number;
}

interface CompanyOption {
  readonly companyId: string;
  readonly companyName: string;
}

interface TimelineFilters {
  readonly company: string;
  readonly decision: DecisionKind | typeof ALL;
  readonly status: StatusFilter;
}

interface DisplayDay extends TimelineDayFrameView {
  readonly totalTurnCount: number;
}

const ALL = "all";
const INITIAL_FILTERS: TimelineFilters = {
  company: ALL,
  decision: ALL,
  status: "all",
};

export function OperationsTimeline({
  loadDetail,
  loadTimeline,
  onWeekChange,
  revision,
  runId,
  timelinePending = false,
  week,
  weeks,
}: OperationsTimelineProps) {
  const [timeline, setTimeline] = useState<TimelineWeekView | null>(null);
  const [filters, setFilters] = useState<TimelineFilters>(INITIAL_FILTERS);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedEntryId, setSelectedEntryId] = useState<string | null>(null);
  const weekCache = useRef(new Map<string, TimelineWeekView>());

  useEffect(() => {
    weekCache.current.clear();
    setTimeline(null);
    setSelectedEntryId(null);
    setFilters(INITIAL_FILTERS);
  }, [runId]);

  useEffect(() => {
    if (timelinePending) {
      setTimeline(null);
      setLoading(false);
      setError(null);
      return;
    }

    const cacheKey = `${revision}:${week}`;
    const cached = weekCache.current.get(cacheKey);
    if (cached) {
      setTimeline(cached);
      setLoading(false);
      setError(null);
      return;
    }

    const controller = new AbortController();
    setLoading(true);
    setError(null);
    void loadTimeline(runId, week, controller.signal)
      .then((nextTimeline) => {
        weekCache.current.set(cacheKey, nextTimeline);
        setTimeline(nextTimeline);
      })
      .catch((reason: unknown) => {
        if (!isAbortError(reason)) {
          setError(
            requestErrorMessage(reason, {
              fallback:
                "This run has not produced a readable operations timeline yet.",
            }),
          );
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [loadTimeline, revision, runId, timelinePending, week]);

  const companies = useMemo(() => timelineCompanies(timeline), [timeline]);
  const decisionKinds = useMemo<readonly DecisionKind[]>(
    () =>
      [
        ...new Set(
          timeline?.days.flatMap((day) =>
            day.turns.map((turn) => decisionKind(turn.decision)),
          ) ?? [],
        ),
      ].sort(),
    [timeline],
  );
  const visibleDays = useMemo(
    () => filterDays(timeline?.days ?? [], filters),
    [filters, timeline],
  );

  return (
    <section aria-labelledby="operations-title" className="panel operations-panel">
      <OperationsHeader
        context={timeline?.context ?? null}
        week={week}
        weeks={weeks}
      />

      {timeline && (
        <>
          <RunProvenanceBanner context={timeline.context} />
          <RunDiagnosticsPanel context={timeline.context} />
          <WeekNavigator
            onSelect={onWeekChange}
            summaries={timeline.weekSummaries}
            week={week}
          />
          <TimelineFilterBar
            companies={companies}
            decisionKinds={decisionKinds}
            filters={filters}
            onChange={setFilters}
          />
        </>
      )}

      {timelinePending ? (
        <TimelineNotice label="This run is queued and has not produced timeline data yet." />
      ) : loading ? (
        <TimelineNotice label="Loading this simulation week…" />
      ) : error ? (
        <TimelineError message={error} />
      ) : (
        <div className="operations-stream">
          {visibleDays.map((day) => (
            <DayBucket
              day={day}
              key={day.simDay.absoluteDay}
              onSelectEntry={setSelectedEntryId}
            />
          ))}
        </div>
      )}

      {selectedEntryId && (
        <DecisionDrawer
          entryId={selectedEntryId}
          loadDetail={loadDetail}
          onClose={() => setSelectedEntryId(null)}
          onSelectEntry={setSelectedEntryId}
          runId={runId}
        />
      )}
    </section>
  );
}

function OperationsHeader({
  context,
  week,
  weeks,
}: {
  readonly context: TimelineContextView | null;
  readonly week: number;
  readonly weeks: number;
}) {
  return (
    <div className="section-heading operations-heading">
      <div>
        <span className="eyebrow">OPERATIONS TIMELINE</span>
        <h2 id="operations-title">One trading week, Monday through Sunday</h2>
      </div>
      <p>
        Week {week} of {context?.totalWeeks ?? weeks}
        {context ? checkpointSummary(context) : ""}
      </p>
    </div>
  );
}

function checkpointSummary(context: TimelineContextView): string {
  const day = context.checkpointDay;
  return day === null
    ? ""
    : ` · last checkpoint ${simulationDayLabel(day)} · state v${context.checkpointStateVersion}`;
}

function RunProvenanceBanner({ context }: { readonly context: TimelineContextView }) {
  if (!context.isReplay) {
    const active = context.status === "queued" || context.status === "running";
    return (
      <div className="trace-provenance live">
        <span className="provenance-icon" aria-hidden="true">
          {active ? "LIVE" : context.status.toUpperCase()}
        </span>
        <div>
          <strong>{context.currentModelCallCount} model calls in this run</strong>
          <p>
            Trace material and {formatValue(context.currentUsage.totalTokens)} tokens
            belong to run <code>{context.currentRunId}</code>.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="trace-provenance replay">
      <span className="provenance-icon" aria-hidden="true">REPLAY</span>
      <div>
        <strong>
          Zero-call replay · {context.currentModelCallCount} current model calls ·{" "}
          {formatValue(context.currentUsage.totalTokens)} current tokens
        </strong>
        <p>
          Direct replay parent: <code>{context.sourceRunId ?? "not recorded"}</code>.
          Ultimate trace source: <code>{context.traceRunId}</code>: {context.sourceModelCallCount}{" "}
          calls and {formatValue(context.sourceUsage.totalTokens)} tokens. Source usage
          is never charged to this replay.
        </p>
      </div>
    </div>
  );
}

function RunDiagnosticsPanel({ context }: { readonly context: TimelineContextView }) {
  const diagnostics = context.diagnostics;
  const lastTrade =
    diagnostics.lastTradeWeek === null
      ? "none"
      : `Week ${diagnostics.lastTradeWeek}`;
  return (
    <section aria-label="Run diagnostics" className="run-diagnostics">
      <header>
        <span>RUN HEALTH</span>
        <strong>
          {diagnostics.benchmarkEligible === false
            ? "Diagnostic only"
            : diagnostics.benchmarkEligible === true
              ? "Benchmark eligible"
              : "In progress"}
        </strong>
      </header>
      <dl>
        <div><dt>Protocol-invalid</dt><dd>{formatValue(diagnostics.protocolInvalidTurns)} turns</dd></div>
        <div><dt>Trades</dt><dd>{formatValue(diagnostics.tradeCount)} · last {lastTrade}</dd></div>
        <div>
          <dt>Consumer fill</dt>
          <dd>
            {formatPercent(Number(diagnostics.consumerFillRate))} ·{" "}
            {formatExactDecimal(diagnostics.consumerSales)} /{" "}
            {formatExactDecimal(diagnostics.consumerDemand)}
          </dd>
        </div>
        <div><dt>Expired</dt><dd>{formatExactDecimal(diagnostics.expiredQuantity)}</dd></div>
        <div>
          <dt>Rejected decisions</dt>
          <dd>
            {formatValue(diagnostics.economicRejections)} economic ·{" "}
            {formatValue(diagnostics.attentionRejections)} attention
          </dd>
        </div>
        <div>
          <dt>Solvency warning</dt>
          <dd>
            {diagnostics.nearInsolventCompanyIds.length === 0
              ? "none"
              : diagnostics.nearInsolventCompanyIds
                  .map((companyId) => companyLabel(companyId))
                  .join(", ")}
          </dd>
        </div>
      </dl>
      {diagnostics.zeroTradeWeekStreak > 0 && (
        <p>
          No trades for the latest {formatValue(diagnostics.zeroTradeWeekStreak)} completed{" "}
          {plural(diagnostics.zeroTradeWeekStreak, "week")}.
        </p>
      )}
    </section>
  );
}

function WeekNavigator({
  onSelect,
  summaries,
  week,
}: {
  readonly onSelect: (week: number) => void;
  readonly summaries: TimelineWeekView["weekSummaries"];
  readonly week: number;
}) {
  const stripRef = useRef<HTMLDivElement>(null);
  const [scrollBounds, setScrollBounds] = useState({ backward: false, forward: false });

  const updateScrollBounds = useCallback(() => {
    const strip = stripRef.current;
    if (!strip) {
      return;
    }
    const maximum = Math.max(0, strip.scrollWidth - strip.clientWidth);
    const nextBounds = {
      backward: strip.scrollLeft > 1,
      forward: strip.scrollLeft < maximum - 1,
    };
    setScrollBounds((current) =>
      current.backward === nextBounds.backward && current.forward === nextBounds.forward
        ? current
        : nextBounds,
    );
  }, []);

  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) {
      return;
    }
    const selected = strip.querySelector<HTMLElement>(`[data-week="${week}"]`);
    if (selected) {
      strip.scrollTo({
        behavior: "smooth",
        left: selected.offsetLeft - (strip.clientWidth - selected.offsetWidth) / 2,
      });
    }
  }, [summaries, week]);

  useEffect(() => {
    const strip = stripRef.current;
    if (!strip) {
      return;
    }
    const observer = new ResizeObserver(updateScrollBounds);
    observer.observe(strip);
    updateScrollBounds();
    return () => observer.disconnect();
  }, [updateScrollBounds]);

  const slide = (direction: -1 | 1) => {
    const strip = stripRef.current;
    strip?.scrollBy({
      behavior: "smooth",
      left: direction * strip.clientWidth * 0.85,
    });
  };

  return (
    <nav aria-label="Simulation week" className="week-navigator">
      <button
        aria-label="Show earlier weeks"
        disabled={!scrollBounds.backward}
        onClick={() => slide(-1)}
        type="button"
      >
        <span aria-hidden="true">←</span>
      </button>
      <div className="week-density-strip" onScroll={updateScrollBounds} ref={stripRef}>
        {summaries.map((summary) => (
          <button
            aria-current={summary.week === week ? "page" : undefined}
            className={summary.week === week ? "selected" : ""}
            data-week={summary.week}
            key={summary.week}
            onClick={() => onSelect(summary.week)}
            style={{
              "--activity": activityOpacity(summary.turnCount, summaries),
            } as CSSProperties}
            title={
              `Week ${summary.week}: ${summary.turnCount} turns, ` +
              `${summary.rejectedCount} rejected, ` +
              `${formatExactDecimal(summary.tradeQuantity)} traded`
            }
            type="button"
          >
            <span>{summary.week}</span>
          </button>
        ))}
      </div>
      <button
        aria-label="Show later weeks"
        disabled={!scrollBounds.forward}
        onClick={() => slide(1)}
        type="button"
      >
        <span aria-hidden="true">→</span>
      </button>
    </nav>
  );
}

function TimelineFilterBar({
  companies,
  decisionKinds,
  filters,
  onChange,
}: {
  readonly companies: readonly CompanyOption[];
  readonly decisionKinds: readonly DecisionKind[];
  readonly filters: TimelineFilters;
  readonly onChange: (filters: TimelineFilters) => void;
}) {
  return (
    <div className="timeline-filters">
      <label>
        <span>Company</span>
        <select
          onChange={(event) => onChange({ ...filters, company: event.target.value })}
          value={filters.company}
        >
          <option value={ALL}>All companies</option>
          {companies.map((company) => (
            <option key={company.companyId} value={company.companyId}>
              {company.companyName}
            </option>
          ))}
        </select>
      </label>
      <label>
        <span>Status</span>
        <select
          onChange={(event) =>
            onChange({ ...filters, status: event.target.value as StatusFilter })
          }
          value={filters.status}
        >
          <option value="all">All Results</option>
          <option value="accepted">{ACCEPTED_BY_ENGINE_LABEL}</option>
          <option value="rejected">Rejected Decisions</option>
        </select>
      </label>
      <label>
        <span>Decision</span>
        <select
          onChange={(event) =>
            onChange({
              ...filters,
              decision: event.target.value as TimelineFilters["decision"],
            })
          }
          value={filters.decision}
        >
          <option value={ALL}>All decisions</option>
          {decisionKinds.map((kind) => (
            <option key={kind} value={kind}>{decisionLabel(kind)}</option>
          ))}
        </select>
      </label>
    </div>
  );
}

function DayBucket({
  day,
  onSelectEntry,
}: {
  readonly day: DisplayDay;
  readonly onSelectEntry: (entryId: string) => void;
}) {
  const absoluteDay = day.simDay.absoluteDay;
  const sunday = weekdayName(day.simDay) === "Sunday";
  return (
    <article className="day-bucket">
      <header className="day-marker">
        <time>{weekdayName(day.simDay)}</time>
        <span>
          {sunday
            ? "Weekly trading and consumer settlement"
            : day.totalTurnCount > 1
              ? concurrencyLabel(day)
              : day.totalTurnCount === 1
                ? "1 company turn"
                : "No company turn"}
        </span>
        <small>Simulation day {absoluteDay}</small>
      </header>
      <div className="day-content">
        <div className="day-lanes">
          <section className="company-activity-lane" aria-label="Company activity">
            {day.systemSteps.map((step) => (
              <SystemStepCard
                key={step.entryId}
                onSelect={() => onSelectEntry(step.entryId)}
                step={step}
              />
            ))}
            {day.totalTurnCount > 1 && day.turns.length > 0 && (
              <div className="concurrency-note">
                <span>CONCURRENT OBSERVATION</span>
                Agents saw the same pre-apply state. Cards follow the persisted,
                seed-derived application order.
              </div>
            )}
            {day.turns.length > 0 ? (
              <div className="turn-card-grid">
                {day.turns.map((turn) => (
                  <TurnCard
                    key={turn.entryId}
                    onSelect={() => onSelectEntry(turn.entryId)}
                    turn={turn}
                  />
                ))}
              </div>
            ) : day.systemSteps.length === 0 ? (
              <TimelineNotice label="No activity matches the selected filters for this day." />
            ) : null}
          </section>
          <MarketDisplay absoluteDay={absoluteDay} frame={day.market} />
        </div>
      </div>
    </article>
  );
}

function SystemStepCard({
  onSelect,
  step,
}: {
  readonly onSelect: () => void;
  readonly step: SystemTimelineItemView;
}) {
  return (
    <button className="system-step-card" onClick={onSelect} type="button">
      <span className="system-step-icon" aria-hidden="true">SYSTEM</span>
      <span>
        <small>{systemLabel(step.kind)}</small>
        <strong>{step.title}</strong>
        <span>{step.summary}</span>
      </span>
      <span className="system-step-meta">
        {step.effects.length} {plural(step.effects.length, "effect")} ·{" "}
        {step.affectedCompanyIds.length > 0
          ? step.affectedCompanyIds.map((id) => companyLabel(id)).join(", ")
          : "No direct economic impact"}
      </span>
    </button>
  );
}

function TurnCard({
  onSelect,
  turn,
}: {
  readonly onSelect: () => void;
  readonly turn: TurnTimelineItemView;
}) {
  const dayLabel = simulationDayLabel(turn.simDay);
  return (
    <article
      aria-label={`${turn.companyName} turn on ${dayLabel}`}
      className={`turn-card ${turn.accepted ? "accepted" : "rejected"}`}
    >
      <header>
        <span className={`role-dot ${turn.role}`} />
        <span>
          <strong>{turn.companyName}</strong>
          <small>{decisionContextSummary(turn)}</small>
        </span>
        <span className={`decision-status ${turn.accepted ? "accepted" : "rejected"}`}>
          {decisionDispositionLabel(turn)}
        </span>
      </header>
      <ol className="decision-chain">
        <DecisionStage
          label="Decision Trigger"
          value={turn.wakeSignals.map((signal) => wakeLabel(signal.reason)).join(" · ")}
        />
        <DecisionStage label="Company Decision" value={decisionSummary(turn.decision)} />
        <DecisionStage
          label="Decision Processing Result"
          tone={turn.accepted ? "positive" : "negative"}
          value={decisionProcessingResult(turn)}
        />
        <DecisionStage label="Next Decision Timing" value={nextDecisionTiming(turn)} />
      </ol>
      <footer>
        <span>{turn.title}</span>
        <span>Turn {turn.turnNumberThisWeek}/{turn.turnLimitThisWeek} this week</span>
        {turn.sourceTurnId && <span>Source turn linked</span>}
        <button
          aria-label={`Open ${turn.companyName} turn detail on ${dayLabel}`}
          className="turn-card-action"
          onClick={onSelect}
          type="button"
        >
          Open detail →
        </button>
      </footer>
    </article>
  );
}

function DecisionStage({
  label,
  tone,
  value,
}: {
  readonly label: string;
  readonly tone?: "negative" | "positive";
  readonly value: string;
}) {
  return (
    <li className={tone ? `stage-${tone}` : undefined}>
      <span>{label}</span>
      <strong>{value}</strong>
    </li>
  );
}

function filterDays(
  days: readonly TimelineDayFrameView[],
  filters: TimelineFilters,
): readonly DisplayDay[] {
  return days.map((day) => ({
    ...day,
    totalTurnCount: day.turns.length,
    systemSteps: day.systemSteps.filter(
      (step) => step.kind !== "agent_wake_suppressed",
    ),
    turns: day.turns.filter(
      (turn) =>
        (filters.company === ALL || turn.companyId === filters.company) &&
        (filters.decision === ALL ||
          decisionKind(turn.decision) === filters.decision) &&
        (filters.status === "all" ||
          (filters.status === "accepted" && turn.accepted) ||
          (filters.status === "rejected" && !turn.accepted)),
    ),
  }));
}

function timelineCompanies(
  timeline: TimelineWeekView | null,
): readonly CompanyOption[] {
  const companies = new Map<string, CompanyOption>();
  timeline?.days.forEach((day) =>
    day.turns.forEach((turn) =>
      companies.set(turn.companyId, {
        companyId: turn.companyId,
        companyName: turn.companyName,
      }),
    ),
  );
  return [...companies.values()].sort((left, right) =>
    left.companyName.localeCompare(right.companyName),
  );
}

function activityOpacity(
  count: number,
  summaries: TimelineWeekView["weekSummaries"],
): string {
  const maximum = Math.max(1, ...summaries.map((summary) => summary.turnCount));
  return `${(18 + (count / maximum) * 82).toFixed(1)}%`;
}

function concurrencyLabel(day: DisplayDay): string {
  const versions = [...new Set(day.turns.map((turn) => turn.stateVersion))];
  const state = `state v${versions.length === 1 ? versions[0] : versions.join("/")}`;
  return day.turns.length === day.totalTurnCount
    ? `${day.totalTurnCount} companies observed ${state} concurrently`
    : `${day.turns.length} of ${day.totalTurnCount} concurrent turns shown · observed ${state}`;
}
